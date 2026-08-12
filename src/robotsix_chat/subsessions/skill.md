# Subsessions — Background Agents

The subsession system runs background agents spawned from chat sessions. Every subsession is an
independent agent with its own model, instructions, and lifecycle — it runs concurrently while the
parent conversation continues.

## Subsession kinds

| Kind             | Behaviour                                                                                            |
| ---------------- | ---------------------------------------------------------------------------------------------------- |
| `task`           | One-shot background job — runs to completion and reports a summary back.                             |
| `periodic`       | Re-runs its instructions on a fixed interval until closed — for monitoring, polling, and CI-watch.   |
| `wait_for_event` | Event-driven ticket monitor — wakes on mill push events (ticket state changes) instead of polling.   |
| `user_chat`      | Side-chat with the operator — for discussions or decisions without blocking the parent conversation. |
| `on_close`       | One-shot task that fires when the parent session is **closed** — for post-conversation work.         |

## Agent tools — spawn and control from any session

These tools are available to the main chat agent (depth 0) and to any subsession agent whose
children would still be within the configured `max_depth`.

### `spawn_subsession`

Start a background subsession and return its id immediately. Required: `kind`, `title`,
`instructions`. Optional: `model_level`, `interval_seconds`, `max_runs`, `include_previous_result`,
`inherit_context`, `dedup_key`.

- `model_level` picks capability 1 (cheapest) to 4 (frontier). Levels 1-2 need an OpenRouter API
  key; if a spawn errors with an API key message, retry at level 3 (keyless).
- `interval_seconds` (minimum enforced) and `max_runs` are for `periodic` only.
- `dedup_key` prevents duplicate `user_chat`, `periodic`, and `wait_for_event` subsessions — use the
  ticket id as the dedup key for monitors. Always check `list_subsessions` first.

### `message_subsession`

Send a steering message to a running subsession — it sees the message at its next turn boundary.

### `close_subsession`

Close a subsession from the outside. Prefer letting subsessions finish on their own.

### `list_subsessions`

List this conversation's subsessions: id, kind, status, model level, title, and scheduling info.

## Tools available only inside a subsession

### `complete_subsession`

Close **this** subsession and report a summary to the parent. Call when work is finished, discussion
concluded, or the monitored condition has reached a terminal state. The summary is the only thing
the parent conversation is guaranteed to see — make it concise, self-contained, and user-facing
(omit internal technical details: block IDs, event numbers, stack traces, raw API fragments).

**Guard: periodic ticket monitors must observe at least one full tick before self-closing** — a
`complete_subsession` call before the first run completes is rejected.

**Guard: periodic ticket monitors must verify the most recent CI workflow run** before calling
`complete_subsession`. Use `check_workflow_run` (or the GitHub Actions API) to fetch the latest run
status, and include the verification result in the summary. A summary without CI evidence is
rejected.

### `set_checkpoint`

Persist arbitrary key/value data across restarts. Each call **replaces** the entire checkpoint, so
include all fields you want to keep. Use it for: monitored ticket id, last-known ticket state,
completion criteria, consecutive-failure counters. For `wait_for_event` monitors the `ticket_id` key
is system-owned and is preserved automatically even if omitted, so the monitor keeps its target
ticket across restarts.

### `self_update_subsession`

Update THIS periodic subsession's own run configuration — the natural alternative to spawning a new
periodic child (which is not allowed from within a periodic context). Changes take effect on the
next scheduled tick.

Parameters (at least one required):

- `instructions` (string, ≤ 8000 chars) — rewrite or extend the instruction text this subsession
  executes each tick.
- `interval_seconds` (number, ≥ configured minimum) — change the polling interval.
- `max_runs` (integer, ≥ 0) — adjust the remaining max-run cap. Pass `null`/`None` to remove the cap
  entirely. The run counter is **never** reset — self-update cannot bypass max-run limits.

Only works from within a periodic subsession. Returns a confirmation string listing which fields
were changed.

## Lifecycle

| Status        | Meaning                                                    |
| ------------- | ---------------------------------------------------------- |
| `running`     | An agent turn is in flight.                                |
| `waiting`     | Idle, waiting for an inbox message (user_chat).            |
| `sleeping`    | Periodic, waiting for the next scheduled run.              |
| `paused`      | Periodic, auto-paused by the idle-guard — retains worker.  |
| `closed`      | Finished normally, summary delivered.                      |
| `failed`      | Terminated with an error.                                  |
| `interrupted` | Server restarted while work was live — resumes on restart. |
