# Periodic Checks (periodic subsessions)

The assistant can arm **periodic subsessions** that re-run on a regular cadence — e.g. every 30
minutes — to monitor the mill board for ticket status changes, poll an endpoint, or watch for any
recurring condition. Each run executes in a sub-agent with the same tool suite as the main agent
(mill, board reader, calendar, refdocs, …), so it can query the board, check ticket status, and
report back.

## Setting up a periodic board check

Tell the assistant something like:

> "Watch my board tickets and let me know if any of them change status — check every 30 minutes."

The assistant calls `spawn_subsession` with:

| Parameter                 | Value                                                                                                            |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------- |
| `kind`                    | `"periodic"`                                                                                                     |
| `title`                   | Short human-readable label shown in the UI panel, e.g. "Monitor ticket T-42 status"                              |
| `instructions`            | A self-contained prompt for the sub-agent: what to check, which tickets to watch, what constitutes a change      |
| `model_level`             | Capability level 1–4 picked by difficulty (cheap tiers for simple polling)                                       |
| `interval_seconds`        | How often to re-run, in seconds. Minimum is 60 seconds; 1800 (30 minutes) is a common choice                     |
| `max_runs`                | (optional) Cap on the number of runs; omitted means run until closed                                             |
| `include_previous_result` | Set to `true` so each run can compare against the prior state                                                    |
| `dedup_key`               | When monitoring a ticket, set to the ticket id (e.g. `"5f1c"`) — prevents duplicate monitors for the same ticket |

### Change-detection convention

Each periodic run is instructed to reply with exactly `NO_CHANGE` when nothing changed since the
previous run, **or when only minor, low-value state transitions occurred** (e.g. draft→ready,
waiting_for_ci→in_progress, label changes, routine CI runs). The sentinel triggers automatic
suppression: no result is delivered to the parent conversation and no notification bubble is shown —
you are only bothered when something substantive happened. For minor but notable changes the agent
replies with a single concise line; full reports are reserved for substantive changes (first-time
blocking, completion, failure, or transitions requiring user action). After a configurable number of
consecutive `NO_CHANGE` runs (`subsessions.auto_stop_no_change_runs`, default 5) the subsession
closes itself.

## Listing active checks

Ask:

> "What periodic checks are currently running?"

The assistant calls `list_subsessions`. The Subsessions panel in the UI also shows every periodic
subsession with its run count and a live countdown to the next run.

You can also call the REST API directly:

```http
GET /subsessions?session_id=<your-session-id>
```

## Steering or cancelling a check

Ask:

> "Stop the check for ticket T-42." — the assistant calls `close_subsession(subsession_id)`.

While a check runs you can also refine it without restarting ("also watch ticket T-43") — the
assistant calls `message_subsession` and the instruction is picked up on the next run.

The UI's Subsessions panel has a **Close** button on every live subsession, or call the REST API:

```http
POST /subsessions/{subsession_id}/close
```

## Retry behaviour for user_chat and task subsessions

When a **user_chat** (user-facing decision prompt) or **task** (one-shot background job) subsession
fails with an exception — e.g. a transient Claude SDK error, a timeout, or context loss after a
server restart — it is automatically retried instead of immediately marked `FAILED`.

| Setting                             | Default | Description                                     |
| ----------------------------------- | ------- | ----------------------------------------------- |
| `subsessions.user_chat_max_retries` | `3`     | Max retries for user_chat and task subsessions. |

### How retries work

1. The worker catches the exception and calls `_format_worker_error` to produce a readable error
   message (see [Error formatting](#error-formatting) below).
2. If the subsession kind is `user_chat` or `task` and `retry_count < user_chat_max_retries`:
   - The retry counter is incremented and persisted immediately to the JSON store so it survives a
     process crash or restart.
   - A `[System note]` is prepended to the subsession's prompt explaining what went wrong on the
     prior attempt and advising the agent to re-fetch any lost external state.
   - The worker re-launches the subsession recursively with the updated prompt.
3. If retries are exhausted (or the kind is not retryable), the subsession is marked `FAILED` as
   before.

### Fallback for exhausted user_chat retries

When a **user_chat** subsession exhausts its retry budget, the summary delivered to the parent
conversation includes the original decision prompt so the operator can answer the question directly
in the main chat — the side-chat panel is no longer available:

```
The side-chat could not be delivered after 3 retries.
You can answer the original decision here:

[original prompt with options]
```

This ensures the operator is never left wondering what the question was.

### Error formatting

`_format_worker_error` transforms raw exceptions into actionable messages:

| Exception pattern                              | Formatted output                                                            |
| ---------------------------------------------- | --------------------------------------------------------------------------- |
| Degenerate success frame (SDK bug)             | Explains the self-contradictory `is_error=True` / `subtype="success"` frame |
| Usage credits exhausted                        | Clear message about the tier being out of credits                           |
| Process error (exit code + stderr)             | `Claude CLI process exited with code N` plus truncated stderr               |
| Any other exception where type name is absent  | `[TypeName] original message` — e.g. `[TimeoutError] ...`                   |
| Any other exception where type name is present | Original message unchanged (type name already included)                     |

Previously, a generic SDK error like `Claude Code returned an error result: success` was passed
through verbatim, giving the operator no actionable information. Now every error message includes
the exception type name when the SDK's own message omits it, so you can distinguish a `TimeoutError`
from a `RuntimeError` at a glance.

## Mill-recovery behaviour

When the mill (board API) is unreachable, ticket monitors enter a **recovery mode** instead of
self-closing after a fixed number of failures.

1. After **2 consecutive** mill-unreachable failures (`_MAX_MILL_FAILURES`), the subsession stops
   normal periodic ticking and enters recovery mode.
2. A health probe runs with **exponential backoff** — the first retry sleeps
   `subsessions.mill_recovery_initial_backoff_seconds` (default 60 s), then doubles on each
   subsequent retry up to `subsessions.mill_recovery_max_backoff_seconds` (default 3600 s / 1 hour).
3. On each retry cycle, the subsession probes the mill's health endpoint. If mill is reachable
   again, the failure counter resets and normal periodic operation resumes automatically — no manual
   intervention needed.
4. If the mill remains unreachable after `subsessions.mill_recovery_max_retries` (default 10)
   recovery retries, the subsession is permanently closed and a summary is delivered to the parent
   conversation.

| Config key                                          | Default  | Description                                                                     |
| --------------------------------------------------- | -------- | ------------------------------------------------------------------------------- |
| `subsessions.mill_recovery_initial_backoff_seconds` | `60.0`   | Initial backoff (seconds) before the first health probe. Doubles on each retry. |
| `subsessions.mill_recovery_max_backoff_seconds`     | `3600.0` | Maximum backoff cap (seconds) — backoff never exceeds this.                     |
| `subsessions.mill_recovery_max_retries`             | `10`     | Max retries before the subsession is permanently closed.                        |

## How it works under the hood

01. `spawn_subsession(kind="periodic", ...)` launches an asyncio worker that runs one agent turn per
    tick on the configured interval (minimum `subsessions.min_interval_seconds`, default 60s).

02. Each turn runs the subsession's own agent (built at the chosen `model_level` via
    `create_agent_from_settings`) with the full standard tool suite plus the subsession tools. Every
    turn is guarded by a hard timeout (`subsessions.run_timeout_seconds`, default 600 s): if the
    agent turn (recall + LLM call + delivery) exceeds the deadline, the run is marked failed, a
    warning is logged, and the schedule continues with the next tick — preventing a hung cognee
    adapter lock or stalled LLM call from freezing the subsession forever.

03. When `include_previous_result` is `true`, the previous run's result is prepended to the prompt
    so the agent can compare state across runs.

04. A `NO_CHANGE` reply suppresses parent delivery and the `subsession_result` SSE frame for that
    run; N consecutive suppressed runs auto-close the subsession.

05. A non-suppressed result is delivered to the parent conversation (a synthetic turn in the owning
    chat session, or the parent subsession's inbox when nested) and published as a
    `subsession_result` frame to the browser.

    - **Decision chats (user_chat) spawned by periodic parents get dual delivery:** the outcome is
      enqueued into the periodic parent's inbox (so the periodic sees completed children on its next
      wake and suppresses duplicate user_chat spawns for the same ticket) AND scheduled as a
      reaction in the main chat (so the operator sees decisions immediately even while the periodic
      is sleeping). Previously, outcomes from periodic-spawned decision chats reached only the
      sleeping periodic parent and were silently stranded.
    - **Nested user_chat prohibition:** a `user_chat` subsession cannot spawn another `user_chat`
      subsession — preventing stacked orphaned decision chats. If a spawned decision chat tries to
      open a second decision chat for the same ticket, the spawn is refused with a
      `SubsessionUserChatSpawnError`. Non-`user_chat` children (e.g. `task`) from a `user_chat`
      parent are still allowed.

06. **Terminal-state discipline.** The sub-agent calls its `complete_subsession(summary)` tool as
    soon as the monitored condition reaches a verified terminal state — the summary is delivered to
    the parent and the subsession closes.

07. Subsessions persist to `/data/subsessions.json`; periodic ones are automatically resumed after a
    process restart (e.g. Watchtower redeploy) with their remaining run budget.

08. **Blocked-resume threshold detection.** When a periodic monitor resumes and finds its ticket
    still BLOCKED, the subsession's checkpoint tracks a `blocked_resume_count`. If the ticket stays
    blocked across **3 consecutive resume attempts** (controlled by `_MAX_BLOCKED_RESUMES` in
    `worker_mill.py`), the subsession is automatically closed with `close_reason="repeated_blocked"`
    and a diagnostic summary is delivered to the parent conversation. This prevents the agent from
    cycling through a dead-end implement→blocked→resume loop — e.g. config-standard footprint
    violations that the assistant cannot fix on its own (the implement step fails to revert
    base-branch files, re-blocking the ticket on every attempt).

    - The counter **resets to 0** any time the ticket transitions to a non-blocked state between
      resumes, meaning the agent made progress.
    - The stale-worker cap (`_MAX_STALE_WORKER_RESUMES = 2`, which closes with
      `close_reason="stale_worker"`) is checked independently; whichever cap fires first closes the
      subsession.
    - When the counter is between 1 and 2 (below the threshold), the agent receives an additional
      context note:
      `"Repeated block: this is blocked-resume attempt X/3 (N remaining before auto-close). If the same failure keeps recurring, stop auto-retrying and escalate to the operator."`

09. **Decision-blocked guidance.** When a periodic monitor finds its ticket awaiting an operator
    decision — stuck in `human_issue_approval`, waiting on an `"Option A or B?"` choice, or
    otherwise blocked on human direction — the sub-agent is instructed to **not** silently reply
    `NO_CHANGE` run after run. Instead, it reports the blocked state with a recommendation to pause
    the monitor, e.g.:

    > "Ticket is awaiting operator decision. Consider pausing this monitor until the operator
    > provides direction."

    This surfaces the pause recommendation immediately so the operator can act on it, rather than
    waiting for the `auto_stop_no_change_runs` timeout to close the subsession. The guidance is
    embedded in the prompt built by `_build_periodic_input` in `worker.py`.

10. **Mill-recovery mode.** If the mill is unreachable, the monitor enters a recovery loop with
    exponential backoff (see [Mill-recovery behaviour](#mill-recovery-behaviour) above), probing the
    mill health endpoint and resuming automatically when it recovers.

11. Concurrency is bounded by `subsessions.max_concurrent` (default 8, across all subsession kinds);
    exceeding it returns a friendly refusal rather than raising.
