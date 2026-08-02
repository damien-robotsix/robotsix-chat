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
consecutive `NO_CHANGE` runs (`subsessions.auto_stop_no_change_runs`, default 3) the subsession
closes itself.

### Auto-stop and failure notifications

When a periodic monitor auto-stops (e.g. after 3 consecutive no-change runs, or after hitting its
`max_runs` limit) or fails with an API error, the assistant **immediately notifies you** in the
conversation — you do not need to send a message to learn what happened. The notification is
delivered via a synthetic reaction turn:

1. **Normal case** — the main agent runs a real LLM turn that processes the outcome and replies with
   a substantive message explaining what happened, what it means, and what you can do next (e.g.
   restart the monitor, check the ticket). The prompt instructs the agent to **not** just
   acknowledge the outcome briefly — it must provide actionable context.
1. **LLM API failure** — if the reaction turn itself fails (e.g. OpenRouter is unreachable), the
   system falls back to publishing a plain `agent_message` frame directly into the chat. You see a
   message like
   `"[System] Background task 'Monitor ticket T-42' (periodic) auto-stopped after consecutive no-change runs."`
   with the full outcome included.
1. **Depth bounding** — if reaction turns chain-react (a close spawns a new subsession that closes
   and triggers another reaction, etc.), the recursion is capped at 3 nested turns. Beyond that,
   outcomes are recorded passively without further LLM calls.
1. **Plan-aware prompts** — if the main conversation has an active autonomous plan (awaiting
   approval or mid-execution), the reaction prompt includes the current plan and instructs the agent
   to acknowledge the subsession outcome as a note and continue without re-requesting approval or
   restarting planning. This prevents subsession notifications from derailing approved work.

Internal reason codes (e.g. `"no_change_auto_stop"`, `"failed"`, `"ticket_terminal"`) are
automatically translated to human-readable phrases in both the prompt and fallback messages.

**Stale-monitor suppression.** If the monitored ticket is already in a terminal state — that is, its
`last_known_state` is `"closed"` or `"done"` — the auto-stop/auto-pause notification is silently
suppressed instead of being surfaced to you. This prevents a monitor tracking a long-closed ticket
from generating repeated "no change" chatter; only meaningful state changes or blockers for active
tickets are surfaced.

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

### Paused-monitor resume behaviour (watcher)

When a periodic monitor accumulates `subsessions.max_idle_runs` consecutive `NO_CHANGE` replies, the
subsession enters a real `PAUSED` status (distinct from `CLOSED`) by calling `registry.mark_paused()`
with reason `"paused"`. Unlike a closed subsession, a paused monitor's **worker stays alive**: it
stops running agent turns and blocks on an event-driven inbox signal instead of terminating. The
monitor resumes **when the monitored ticket's state changes** (or when it is explicitly reopened).

A background **watcher** task (`watch_paused_monitors` in `subsessions/watcher.py`) runs for the
lifetime of the server process. On every poll tick it:

1. Queries the registry for all paused periodic subsessions (`find_paused_periodic()`).
1. For each paused monitor, fetches the current ticket state from the mill API via the
   `board_api_base_url` configured in `direct_repo`.
1. Compares the fetched state against the checkpoint's `last_known_state`.
1. If the state differs, the watcher sends an immediate inbox **wake message** to the live `PAUSED`
   worker (`_wake_paused_monitor`), which unblocks it and resumes polling right away. If the worker
   is not reachable (e.g. it died after a server restart), the watcher falls back to `reopen()` +
   spawning a fresh worker.
1. A second pass also polls GitHub for a tracked PR's merge status, resuming the monitor via the
   same wake/reopen path when the PR is merged.

The poll interval is controlled by:

| Config key                                         | Default | Description                                                                                                                         |
| -------------------------------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `subsessions.paused_monitor_poll_interval_seconds` | `60.0`  | Seconds between watcher polls. Set to `0` to disable runtime polling; paused monitors then only resume on the next service restart. |

When no paused monitors exist, the watcher sleeps for 30 seconds before checking again (avoiding
busy-wait). If the mill endpoint is unreachable during a poll tick, the watcher logs a debug message
and tries again on the next tick — no paused monitor is resumed until the mill responds.

The watcher is started automatically during the server's `startup` phase (in `cli.py`'s
`_resume_autonomous` lifespan hook). If `board_api_base_url` is not configured, the watcher returns
immediately with a debug log line and does not loop.

The `reopen()` method on `SubsessionRegistry` transitions records that are `PAUSED`, or `CLOSED`
with `close_reason == "paused"`, `"human_approval_timeout"`, or `"pre_authorized_approval"`, and
`kind == PERIODIC`. All other terminal records (completed, max_runs, explicit close, etc.) are left
untouched — the watcher will never accidentally revive a deliberately closed subsession.

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
1. If the subsession kind is `user_chat` or `task` and `retry_count < user_chat_max_retries`:
   - The retry counter is incremented and persisted immediately to the JSON store so it survives a
     process crash or restart.
   - A `[System note]` is prepended to the subsession's prompt explaining what went wrong on the
     prior attempt and advising the agent to re-fetch any lost external state.
   - The worker re-launches the subsession recursively with the updated prompt.
1. If retries are exhausted (or the kind is not retryable), the subsession is marked `FAILED` as
   before.

### Fallback for exhausted user_chat retries

When a **user_chat** subsession exhausts its retry budget, the summary delivered to the parent
conversation includes the original decision prompt so the operator can answer the question directly
in the main chat — the side-chat panel is no longer available:

```text
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
from a `RuntimeError` at a glance. (mill(docs): Side-chat subsessions for user decisions fail with
uninformative error on restart (20260723T235114Z-side-chat-subsessions-for-user-decisions-903d))

## Mill-recovery behaviour

When the mill (board API) is unreachable, ticket monitors enter a **recovery mode** instead of
self-closing after a fixed number of failures.

1. After **2 consecutive** mill-unreachable failures (`_MAX_MILL_FAILURES`), the subsession stops
   normal periodic ticking and enters recovery mode.
1. A health probe runs with **exponential backoff** — the first retry sleeps
   `subsessions.mill_recovery_initial_backoff_seconds` (default 60 s), then doubles on each
   subsequent retry up to `subsessions.mill_recovery_max_backoff_seconds` (default 3600 s / 1 hour).
1. On each retry cycle, the subsession probes the mill's health endpoint. If mill is reachable
   again, the failure counter resets and normal periodic operation resumes automatically — no manual
   intervention needed.
1. If the mill remains unreachable after `subsessions.mill_recovery_max_retries` (default 10)
   recovery retries, the subsession is permanently closed and a summary is delivered to the parent
   conversation.

| Config key                                          | Default  | Description                                                                     |
| --------------------------------------------------- | -------- | ------------------------------------------------------------------------------- |
| `subsessions.mill_recovery_initial_backoff_seconds` | `60.0`   | Initial backoff (seconds) before the first health probe. Doubles on each retry. |
| `subsessions.mill_recovery_max_backoff_seconds`     | `3600.0` | Maximum backoff cap (seconds) — backoff never exceeds this.                     |
| `subsessions.mill_recovery_max_retries`             | `10`     | Max retries before the subsession is permanently closed.                        |

## Transient-error retry behaviour

Periodic subsessions that talk to an external LLM API (e.g. OpenRouter) may encounter **transient**
upstream errors — a Pydantic validation error from the chat-completions endpoint, a 503, or a
dropped connection. These are distinct from mill-recovery errors (see above) because the mill is
reachable but the LLM provider itself hiccups.

When a periodic subsession's agent turn fails with a recognised transient error:

1. The turn is retried with **exponential backoff** — the first retry sleeps
   `subsessions.transient_error_backoff_base` (default 1.0 s), then doubles on each subsequent retry
   up to `subsessions.transient_error_backoff_cap` (default 30.0 s).
1. A warning is logged with the error details for debugging.
1. If the turn succeeds on a retry, the periodic cycle continues normally — no result is lost.
1. If all retries are exhausted (`subsessions.transient_error_max_retries + 1` total attempts), the
   run is **skipped gracefully**: the subsession stays alive (status `SLEEPING`), a
   `"TRANSIENT_ERROR"` result is recorded, and the schedule continues with the next interval. The
   subsession is **not** permanently failed — it will retry on its next scheduled tick.

This behaviour applies **only** to `PERIODIC` subsessions. `TASK` and `USER_CHAT` subsessions
propagate transient errors immediately and fail — they are not retried, because those subsessions
run once and a transient failure would silently lose the work.

| Config key                                 | Default | Description                                                               |
| ------------------------------------------ | ------- | ------------------------------------------------------------------------- |
| `subsessions.transient_error_max_retries`  | `3`     | Max retry attempts (besides the initial try) before the cycle is skipped. |
| `subsessions.transient_error_backoff_base` | `1.0`   | Initial backoff in seconds — doubles each retry.                          |
| `subsessions.transient_error_backoff_cap`  | `30.0`  | Maximum backoff cap in seconds — backoff never exceeds this.              |

## How it works under the hood

1. `spawn_subsession(kind="periodic", ...)` launches an asyncio worker that runs one agent turn per
   tick on the configured interval (minimum `subsessions.min_interval_seconds`, default 60s).

1. Each turn runs the subsession's own agent (built at the chosen `model_level` via
   `create_agent_from_settings`) with the full standard tool suite plus the subsession tools. Every
   turn is guarded by a hard timeout (`subsessions.run_timeout_seconds`, default 600 s): if the
   agent turn (recall + LLM call + delivery) exceeds the deadline, the run is marked failed, a
   warning is logged, and the schedule continues with the next tick — preventing a hung cognee
   adapter lock or stalled LLM call from freezing the subsession forever.

1. When `include_previous_result` is `true`, the previous run's result is prepended to the prompt
   so the agent can compare state across runs.

1. A `NO_CHANGE` reply suppresses parent delivery and the `subsession_result` SSE frame for that
   run; N consecutive suppressed runs auto-close the subsession.

1. A non-suppressed result is delivered to the parent conversation (a synthetic turn in the owning
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
   - **Periodic nesting restriction:** a **periodic** subsession cannot spawn another periodic
     child. If the periodic agent attempts to spawn a periodic monitor, the error message suggests
     actionable alternatives: use a one-shot task subsession instead, modify the existing monitor's
     prompt to cover the additional scope, or ask the operator to spawn a top-level periodic
     monitor.

1. **Terminal-state discipline (three-source verification + CI loop guard).** The sub-agent calls
   its `complete_subsession(summary)` tool as soon as the monitored condition reaches a verified
   terminal state — the summary is delivered to the parent and the subsession closes. For periodic
   monitors with a `ticket_id` in their checkpoint, the agent must verify from **three independent
   sources** before calling `complete_subsession`:

   - **(1)** A live GET of the ticket endpoint confirming the terminal state.
   - **(2)** A check of the PR/MR endpoint confirming merge status (or a statement that the ticket
     was closed without a PR).
   - **(3)** A check of the most recent CI workflow run for the affected pipeline (e.g. the
     "Publish Docker image" workflow or the repo's primary deploy workflow).

   **Programmatic gate.** `complete_subsession` **rejects** any summary that does not include CI
   workflow evidence — at least one of: `"CI workflow"`, `"workflow run"`, `"pipeline"`,
   `"GitHub Actions"`, `"publish"`, `"deploy workflow"`, or `"could not be verified"`. If the
   summary lacks these keywords, the tool returns a rejection message instructing the agent to
   fetch the CI workflow status first.

   **On CI failure.** If the workflow run failed or is still failing, the agent must NOT claim
   success. Instead it calls `complete_subsession` with a summary documenting the failure (run id,
   reason, log excerpt), then calls `spawn_subsession` to file a new diagnostic ticket so the
   operator sees the pipeline is still broken. If the workflow API is unreachable, the agent
   retries twice with a 5-second pause before acknowledging the status could not be verified.

1. Subsessions persist to `/data/subsessions.json`; periodic ones are automatically resumed after a
   process restart (e.g. Watchtower redeploy) with their remaining run budget. Unlike task and
   user_chat subsessions, periodic monitors resume silently — they are excluded from the restart
   notice injected into the parent conversation, preventing unnecessary parent-agent noise on every
   redeploy. Results continue to be delivered via their normal `subsession_result` frames.

   **Auto-paused monitors are also restored on restart.** If a periodic monitor was auto-paused
   with reason `paused` (max idle runs), its `PAUSED` state is persisted; the resume hook
   re-spawns the worker and immediately returns it to the paused wait loop (it does not run an
   agent turn), so the live watcher can wake it when the ticket state changes. Monitors
   auto-closed with `no_change_auto_stop` (consecutive no-change runs) or
   `human_approval_timeout` are re-spawned normally so they can re-verify the ticket state — the
   underlying condition (no change, idle, pending approval) may have resolved during the outage;
   the worker's `_check_resume_status` inspects the current ticket state on its first post-restart
   tick and closes immediately if conditions have not improved.

   Monitors closed explicitly (e.g. `completed` by the agent, `max_runs` by user cap, or any
   explicit close by the user) are **not** re-spawned — the shutdown was intentional.

1. **Blocked-resume threshold detection.** When a periodic monitor resumes and finds its ticket
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

1. **Decision-blocked guidance.** When a periodic monitor finds its ticket awaiting an operator
   decision — stuck in `human_issue_approval`, waiting on an `"Option A or B?"` choice, or
   otherwise blocked on human direction — the sub-agent is instructed to **not** silently reply
   `NO_CHANGE` run after run. Instead, it reports the blocked state with a recommendation to pause
   the monitor, e.g.:

   > "Ticket is awaiting operator decision. Consider pausing this monitor until the operator
   > provides direction."

   This surfaces the pause recommendation immediately so the operator can act on it, rather than
   waiting for the `auto_stop_no_change_runs` timeout to close the subsession. The guidance is
   embedded in the prompt built by `_build_periodic_input` in `worker.py`.

   **Wall-clock backstop.** In addition to the run-count gate (`human_approval_timeout_runs`), the
   system tracks how long the checkpoint has carried `last_known_state='human_issue_approval'`
   using a `human_approval_since` timestamp stored in the checkpoint. If the wall-clock time spent
   in the `human_issue_approval` state exceeds `human_approval_timeout_seconds` (default 300 s / 5
   minutes), the subsession automatically escalates (reason `human_approval_timeout`) — even if the
   agent never emitted a `NO_CHANGE` reply. This catches the case where the agent follows the
   system prompt (calling `complete_subsession` instead of replying `NO_CHANGE`) but the call
   fails, avoiding an indefinite stall.

   | Config key                                   | Default | Description                                                                          |
   | -------------------------------------------- | ------- | ------------------------------------------------------------------------------------ |
   | `subsessions.human_approval_timeout_runs`    | `5`     | Consecutive `NO_CHANGE` runs in `human_issue_approval` state before auto-escalate.   |
   | `subsessions.human_approval_timeout_seconds` | `300.0` | Wall-clock seconds in `human_issue_approval` state before auto-escalate (5 minutes). |

1. **Mill-recovery mode.** If the mill is unreachable, the monitor enters a recovery loop with
   exponential backoff (see [Mill-recovery behaviour](#mill-recovery-behaviour) above), probing the
   mill health endpoint and resuming automatically when it recovers.

1. Concurrency is bounded by `subsessions.max_concurrent` (default 8, across all subsession kinds);
   exceeding it returns a friendly refusal rather than raising.

## Autonomous-session interaction

In **autonomous sessions**, periodic monitors do not block session completion. The runner's
`_has_pending_subsessions` check **excludes** periodic subsessions; only `task` and `user_chat`
subsessions (which have finite lifetimes) block the loop. The agent is instructed to emit the
completion marker while periodic monitors are still running, under either of the following paths:

### Stale-monitor completion (automatic)

The agent may declare the session complete when all active periodic monitors have been reporting
`NO_CHANGE` for at least `autonomous.stale_monitor_runs_before_completion` (default 3) consecutive
cycles and no other pending actions remain (no in-flight task or user_chat subsessions, no
unaddressed operator decisions). Monitors continue running in the background after closure; their
terminal summaries are delivered to the next session.

### Operator-driven completion (explicit)

When the operator sends repeated continuation messages (e.g. "Continue" multiple times) without
providing new instructions or data, the agent treats this as explicit permission to close the
session immediately — bypassing the stale-threshold check. The same applies when the operator sends
short, non-substantive messages several times in a row. This prevents indefinite looping when the
operator is satisfied and all actionable work is done but periodic monitors have not yet accumulated
enough `NO_CHANGE` cycles to auto-close.

| Config key                                        | Default | Description                                                                   |
| ------------------------------------------------- | ------- | ----------------------------------------------------------------------------- |
| `autonomous.stale_monitor_runs_before_completion` | `3`     | Consecutive `NO_CHANGE` cycles before a periodic monitor is considered stale. |

See [Configuration](configuration.md#autonomous) for the full autonomous settings reference.
