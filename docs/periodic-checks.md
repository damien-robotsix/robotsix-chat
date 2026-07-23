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
closes itself. When this happens, the assistant does **not** silently drop the stall — it
proactively offers escalation paths: re-trigger the check with narrower debugging instructions,
escalate for human review with a clear diagnosis, or suggest inspecting implementation history and
fetch-spec logs to understand what is blocking progress.

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

## How it works under the hood

1. `spawn_subsession(kind="periodic", ...)` launches an asyncio worker that runs one agent turn per
   tick on the configured interval (minimum `subsessions.min_interval_seconds`, default 60s).
2. Each turn runs the subsession's own agent (built at the chosen `model_level` via
   `create_agent_from_settings`) with the full standard tool suite plus the subsession tools. Every
   turn is guarded by a hard timeout (`subsessions.run_timeout_seconds`, default 600 s): if the
   agent turn (recall + LLM call + delivery) exceeds the deadline, the run is marked failed, a
   warning is logged, and the schedule continues with the next tick — preventing a hung cognee
   adapter lock or stalled LLM call from freezing the subsession forever.
3. When `include_previous_result` is `true`, the previous run's result is prepended to the prompt so
   the agent can compare state across runs.
4. A `NO_CHANGE` reply suppresses parent delivery and the `subsession_result` SSE frame for that
   run; N consecutive suppressed runs auto-close the subsession.
5. A non-suppressed result is delivered to the parent conversation (a synthetic turn in the owning
   chat session, or the parent subsession's inbox when nested) and published as a
   `subsession_result` frame to the browser.
   - **Decision chats (user_chat) spawned by periodic parents get dual delivery:** the outcome is
     enqueued into the periodic parent's inbox (so the periodic sees completed children on its next
     wake and suppresses duplicate user_chat spawns for the same ticket) AND scheduled as a reaction
     in the main chat (so the operator sees decisions immediately even while the periodic is
     sleeping). Previously, outcomes from periodic-spawned decision chats reached only the sleeping
     periodic parent and were silently stranded.
   - **Nested user_chat prohibition:** a `user_chat` subsession cannot spawn another `user_chat`
     subsession — preventing stacked orphaned decision chats. If a spawned decision chat tries to
     open a second decision chat for the same ticket, the spawn is refused with a
     `SubsessionUserChatSpawnError`. Non-`user_chat` children (e.g. `task`) from a `user_chat`
     parent are still allowed.
6. **Terminal-state discipline.** The sub-agent calls its `complete_subsession(summary)` tool as
   soon as the monitored condition reaches a verified terminal state — the summary is delivered to
   the parent and the subsession closes.
7. Subsessions persist to `/data/subsessions.json`; periodic ones are automatically resumed after a
   process restart (e.g. Watchtower redeploy) with their remaining run budget.
8. Concurrency is bounded by `subsessions.max_concurrent` (default 8, across all subsession kinds);
   exceeding it returns a friendly refusal rather than raising.
