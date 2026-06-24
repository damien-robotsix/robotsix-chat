# Periodic Checks

The assistant can arm **periodic background checks** that re-run on a
regular cadence — e.g. every 30 minutes — to monitor the mill board for
ticket status changes, poll an endpoint, or watch for any recurring
condition. Checks run in a fresh sub-agent with access to all the same
tools (mill, calendar, refdocs), so they can query the board, check
ticket status, and report back.

## Setting up a periodic board check

Tell the assistant something like:

> "Watch my board tickets and let me know if any of them change status —
> check every 30 minutes."

The assistant calls `start_check_loop` with:

| Parameter | Value |
|---|---|
| `check_description` | A self-contained prompt for the sub-agent: what to check, which tickets to watch, what constitutes a change |
| `interval_seconds` | How often to re-run, in seconds. Minimum is 60 seconds; 1800 (30 minutes) is a common choice |
| `reason` | (optional) Short human-readable label shown in the UI, e.g. "Monitor ticket T-42 status" |
| `max_iterations` | (optional) Cap on num of checks; `None` means run until explicitly stopped |
| `include_previous_result` | Set to `true` so each tick can compare against the prior state |

### Recommended prompt pattern for change-detection

When you want the assistant to **only notify on changes**, include these
instructions in your request:

> "On each check, query the board for the current status of tickets X, Y, Z.
> Compare against the previous check's result (which you'll see in the prompt).
> If nothing changed, respond with exactly: NO_CHANGE
> If something changed — a ticket reached a gate, got blocked, failed, or
> completed — describe what changed and which ticket."

The `NO_CHANGE` sentinel triggers automatic suppression: no SSE notification
is sent to the browser and no conversation turn is recorded, so you are only
bothered when something actually happened.

## Listing active checks

Ask:

> "What periodic checks are currently running?"

The assistant calls `list_check_loops` and returns each loop's id, status,
interval, iteration count, and a prompt snippet.

You can also call the REST API directly:

```
GET /loops?client_id=<your-client-id>
```

## Cancelling a check

Ask:

> "Stop the check loop for ticket T-42."

The assistant calls `stop_check_loop(loop_id)` with the loop id obtained
from `list_check_loops` (or from the `start_check_loop` return message).

You can also call the REST API directly:

```
POST /loops/{loop_id}/stop
```

After cancellation the check no longer fires. The loop's status flips to
`stopped` and it disappears from the UI's active-loops panel.

## How it works under the hood

1. `start_check_loop` spawns an asyncio worker that runs on a configurable
   interval (minimum 60 seconds, no upper bound).
2. On each tick, a fresh sub-agent is built via `create_agent_from_settings`
   — it has access to mill, calendar, and refdocs tools (same as the
   foreground agent).
3. When `include_previous_result` is `true`, the previous tick's result is
   prepended to the prompt so the sub-agent can compare state across
   iterations.
4. The `NO_CHANGE` sentinel (or empty result) suppresses the SSE notification
   and conversation-store turn for that tick — you see nothing when nothing
   changed.
5. When a tick result is NOT suppressed, a `loop_tick` frame is published via
   SSE to the browser and written to the conversation store for the next
   foreground turn.
6. Loops persist to `.data/check_loops.json` and are automatically resumed
   after a process restart (e.g. Watchtower redeploy).
7. Concurrency is bounded by `max_check_loops` (default 5); exceeding it
   returns a friendly "too many" message rather than raising.
