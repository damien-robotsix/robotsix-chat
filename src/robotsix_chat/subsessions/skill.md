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
`instructions`. Optional: `model_level`, `interval_seconds`, `anchor_time`, `max_runs`,
`auto_stop_no_change_runs`, `include_previous_result`, `inherit_context`, `dedup_key`,
`run_timeout_seconds`.

- `model_level` picks capability 1 (cheap/frequent) to 3 (frontier); 1 covers monitors and routine
  checks, 2 is the workhorse for general work, 3 is frontier-only. Which provider serves a level is
  automatic (flat-rate default with paid failover). For `periodic` and `wait_for_event` monitors,
  levels above the server's `monitor_max_model_level` (default 1) are silently clamped down — use
  level 1 for all routine monitoring. Hard tasks should use `kind="task"` (uncapped) instead.
- `interval_seconds` (minimum enforced), `max_runs`, and `auto_stop_no_change_runs` are for
  `periodic` only.
- `anchor_time` (`periodic` only, optional) pins recurrences to an absolute wall-clock time instead
  of "spawn time + interval". Accepts `"HH:MM"` or `"HH:MM:SS"`, optionally followed by an IANA
  timezone — e.g. `"09:00"`, `"09:00:00 UTC"`, or `"09:00 Europe/Paris"` (default timezone UTC).
  With `interval_seconds=86400` and `anchor_time="09:00"` the monitor fires every day at 09:00 UTC;
  each next fire is the next occurrence of that time-of-day phase-aligned to `interval_seconds`, so
  the schedule does not drift. The first run still fires at spawn; the anchor governs every
  subsequent run. Omit it for the legacy relative-interval behaviour. DST: for whole-day intervals
  the local time-of-day is held constant across daylight-saving transitions.
- `auto_stop_no_change_runs` (must be an integer ≥ 1) overrides the global auto-stop threshold for
  this monitor only. For a long-lived ticket monitor that progresses over days (waiting on human
  review or CI), pass a higher value (e.g. 50) so it is not auto-stopped after the default 3
  consecutive `NO_CHANGE` runs.
- `dedup_key` prevents duplicate `user_chat`, `periodic`, and `wait_for_event` subsessions — use the
  ticket id as the dedup key for monitors. Always check `list_subsessions` first.
- `run_timeout_seconds` (seconds, optional) overrides the global per-run timeout (default 600 s) for
  this subsession — declare a longer budget (e.g. 1800 for a 20-minute batch) for long search /
  tournament / batch work. Bounded by `subsessions.max_run_timeout_seconds` (default 3600 s);
  requests above the cap are clamped to it. A turn that exceeds its budget is failed fast and NOT
  retried, so size it to the longest single step of the job.

### `message_subsession`

Send a steering message to a running subsession — it sees the message at its next turn boundary.

### `close_subsession`

Close a subsession from the outside. Prefer letting subsessions finish on their own.

### `list_subsessions`

List this conversation's subsessions: id, kind, status, model level, title, and scheduling info.

### `check_monitor`

Check whether an active monitor (periodic or wait_for_event subsession) exists for a given ticket.
Returns a JSON object with `active` (bool) and, when a monitor is found, its `subsession_id`,
`kind`, `status`, and `title`. **Always call this tool before claiming a tracker is running** — do
NOT assert "tracking is active" without verifying via `check_monitor`. Searches both
checkpoint-based matches and dedup-key matches.

### Surface stop-and-escalate conditions in the user-facing summary

When you spawn or arm a monitor that carries a **stop-and-escalate condition** — a rule that will
halt the monitor and alert the operator rather than keep looping (e.g. "if the build fails again on
the same model-config error, stop and escalate as an infrastructure blocker") — you MUST make that
condition visible in the user-facing summary. State plainly what triggers the stop and that the
monitor will alert the user, for example: "If the build fails again, the monitor will stop and alert
you as an infrastructure blocker." Do not leave the stop rule implicit in the monitor's
instructions: the operator otherwise assumes the monitor will loop indefinitely, and is caught off
guard when it halts and escalates. Surfacing the condition aligns the operator's expectations with
the monitor's actual escalation behaviour.

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

When the verified run is a `startup_failure` (zero jobs, no logs), report the tool's deterministic
classification verbatim: a sibling workflow on the **same commit** that reached job execution means
**per-workflow config issue** (not an account-level problem); zero siblings reaching job execution
means **account/runner issue** (operator action, not a workflow-file edit). Never speculate a
billing diagnosis that contradicts the tool's classification — two monitors watching the same run
must reach the same conclusion.

### `set_checkpoint`

Persist arbitrary key/value data across restarts. Each call **replaces** the entire checkpoint, so
include all fields you want to keep. Use it for: monitored ticket id, last-known ticket state,
completion criteria, consecutive-failure counters. Two keys are system-owned and preserved
automatically even if omitted: `ticket_id` for `wait_for_event` monitors (so the monitor keeps its
target ticket across restarts), and `auto_stop_no_change_runs` for `periodic` monitors (so a
per-spawn no-change threshold override is not lost).

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

## Operating rules (moved from the system prompt, 2026-09-08)

These rules complement the tool reference above. The spawning conversation keeps the short version
(one subsession per subject, self-contained instructions, reporting contract, pre-spawn guard);
everything about running INSIDE a subsession, monitor hygiene and user_chat decision etiquette is
here.

### Monitors and periodic subsessions

– Monitor existence check: NEVER claim a monitor is active, or that no monitor is needed because the
work already finished, without first checking live state. Before making any claim about a monitor's
existence or status, call check_monitor and list_subsessions (and, when a specific ticket is named,
component_request GET /tickets/{id}) to verify what is actually spawned and what state it is in. If
check_monitor returns terminal_report=true, a previous monitor already tracked the ticket to an end
state — spawn a new monitor only if GET /tickets/{id} shows it active again (not DONE/CLOSED), and
do NOT claim the work is unfinished. If no monitor was spawned and no terminal report exists, say so
directly and offer to start one — do NOT invent a reason for why no monitor exists. Treat the work
as unfinished (and the monitor as still needed) until the ticket is merged AND its endpoints are
confirmed live. – Inside a subsession, call complete_subsession(summary) as soon as your goal is
reached — for periodic work, that means as soon as the monitored condition reaches a verified
terminal state. Also call complete_subsession when user intervention is required (ticket blocked on
a decision, escalation needed). Do NOT call complete_subsession for intermediate progress — only the
final summary reaches the parent. Reply exactly NO_CHANGE on a periodic run where nothing changed. –
Periodic subsessions poll directly on every cycle and cannot spawn child subsessions — they perform
all monitoring, polling, and checking inline in their own replies. Being spawned as a periodic
monitor directly from a conversation (without going through a task subsession) is fully supported —
it is the preferred way to launch a ticket monitor. – When monitoring a ticket that involves a code
change deployed to a component, periodic subsessions must track deploy status alongside board
status. After the PR merges, the fix is not yet live — the monitor must verify the component is
running the new image. Use get_lifecycle_service_status to confirm rollout completed, and
component_request GET /health to verify the component is healthy. A merged PR whose image is not yet
deployed is not a terminal state — keep the monitor open until deploy is confirmed. This prevents
redundant fix proposals for issues already resolved in the running image. – If a periodic subsession
attempts spawn_subsession and receives a 'periodic subsessions cannot spawn' error, do NOT present
options to the user or ask how to proceed. This is a hard code-level restriction, not a transient
failure — retry will hit the same gate. Fall back immediately: perform the monitoring, polling, or
checking inline in the current reply. If the work is too large for one cycle, spread it across
multiple cycles using NO_CHANGE replies to hold intermediate state — the periodic monitor's own
reply loop is the correct vehicle for ongoing inline work. The user should never see the error or be
asked to choose a recovery path. – Subsessions can spawn their own subsessions (nesting is
depth-limited) — split genuinely independent subtasks, do not chain for its own sake. – Spawn
periodic monitors directly — do NOT create a child task subsession whose only job is to call
spawn_subsession(kind='periodic', ...). A task that exists solely to launch a monitor wastes a model
round-trip and duplicates the spawning logic you already own. If you need a periodic monitor, spawn
it from your own context. – When spawning a subsession to report a known global process error (e.g.
'asyncio.run() cannot be called from a running event loop', or any error that affects multiple
tickets/subsessions at once), set dedup_key to the exact error message prefix (first 80 chars). When
spawning a periodic monitor for a ticket, set dedup_key to the ticket id (e.g. '5f1c') — this
prevents duplicate monitors for the same ticket. The system will suppress duplicate spawns for the
same key — only the first spawn creates a new subsession; subsequent spawns return the existing id.
Always pair this with list_subsessions to check what is already running.

### Subsession pool budget

– Subsession pool budget (check before spawning any monitor): the global subsession pool is finite —
all active subsessions (monitors, tasks, side-chats) share one process-wide capacity cap
(`subsessions.max_concurrent` in the server config). Spawning past the cap forces eviction of an
existing paused subsession, so an unplanned spawn can silently kill a monitor you still need. Treat
every spawn as consuming a scarce slot, and plan monitor count against the cap: • Count before
spawning: call list_subsessions and count current active AND paused subsessions, then check headroom
against the cap. If the pool is full, do not spawn — reuse an existing monitor (see below) or ask
the operator which monitor to pause. • Reuse slots: if an existing monitor already covers the same
or a related ticket, resume or reuse it instead of spawning a duplicate. One monitor can watch a
related set of tickets; duplicates waste a slot. • Skip draft tickets: do not spawn monitors for
tickets still in `draft` status — they are unlikely to change state and the board-drain periodic
picks those up instead. Monitor only tickets that have entered the active pipeline. • No
evict-and-respawn thrash: do not pause/evict a low-priority monitor to free a slot and then forget
to respawn it. If you must evict to make room, note the evicted monitor's ticket id and respawn it
explicitly once a slot frees — never leave it evicted silently.

### user_chat decision subsessions

– In a user_chat subsession, ask a pending question ONCE and wait for the user's reply; close with a
summary once the discussion reaches a conclusion. The user can also close it at any time. – CRITICAL
for user_chat decision subsessions: the operator sees ONLY the messages you write in the panel —
they do NOT see your instructions. Every time you reference an option label (Option A, Option B, …)
you MUST restate its full definition inline. For example, write "Option B (phased: cleanup now,
warning-first gate, fail-closed only after auto-mail migrates)" — never just "Option B." This
applies to every turn: the initial recommendation and any follow-up confirmation. When presenting a
decision, show ALL options with definitions so the operator can compare. – CRITICAL for user_chat
decision subsessions: present at most ONE decision per message. When multiple independent decisions
are pending, present them SEQUENTIALLY — state the first decision with its options, wait for the
operator's answer, confirm the choice (echo the selected option back and ask for explicit
acknowledgement), then and only then present the next decision. Never batch multiple unrelated
decisions into a single message; a human operator cannot process a list of choices reliably and will
miss or misread options. If the operator raises a new question mid-sequence, answer it but return to
the pending decision queue afterward.
