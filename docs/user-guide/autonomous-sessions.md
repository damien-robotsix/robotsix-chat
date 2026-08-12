# Autonomous Sessions

Autonomous sessions are single-prompt self-directed runs: the agent starts a normal session
automatically and works the configured prompt through to completion, then closes. There is no
plan-drafting or proposal pause and no operator approval gate — the run executes until the
completion marker, then the session closes and restarts on its trigger.

**Session presets in `autonomous.sessions` are the sole enablement model.** A preset that exists and
has `"enabled": true` IS the enablement — there is no separate master switch. When the sessions list
is empty, no autonomous sessions run. Define at least one preset to get started.

______________________________________________________________________

## Overview

Each configured session preset in `autonomous.sessions` runs an independent single-prompt session
over its own pseudo-owner (`autonomous` for the `"default"` preset, `autonomous:<name>` for named
sessions). A session cannot overlap with itself: a new run does not start while the previous run of
the same session is active.

### Lifecycle

Each session run follows the same flow:

1. **Start** — the runner kicks off exactly one agent turn with the session's kickoff prompt (or the
   standard "begin a new autonomous session and work it to completion" prompt when empty).
2. **Execute** — the agent works autonomously in that single turn, using its tools, subsessions, and
   the continuation-scheduling mechanism as needed.
3. **Complete** — when the agent emits the completion marker (`---AUTONOMOUS COMPLETE---` by
   default) the session is marked `completed`.
4. **Re-trigger** — completion is automatic: the runner closes the session and schedules a fresh run
   per the session's trigger (see [Triggers](#triggers)).

### The `[AUTONOMOUS]` badge

Autonomous sessions surface in the operator's session list under their pseudo-owner with a
`[AUTONOMOUS]` badge and (optionally) a session color accent, so they are easy to distinguish from
interactive chats.

The UI lists all of them with a single query — `GET /sessions?owner_id=autonomous`.  Because named
presets are stored under `autonomous:<name>`, the backend expands the bootstrap `autonomous` owner
to include every `autonomous:*` sub-scope, so every preset appears in one merged list.  When adding
a new scoped owner id, make sure the session-list handler's prefix expansion covers it — otherwise
the session runs but never surfaces in the list.

______________________________________________________________________

## Getting started (default preset)

Add a session preset named `"default"` to `autonomous.sessions` to start the simplest autonomous
session:

```json
"autonomous": {
  "sessions": [
    {
      "name": "default",
      "prompt": "",
      "trigger_type": "periodic",
      "trigger_interval_seconds": 45.0,
      "max_auto_turns": 20,
      "enabled": true
    }
  ]
}
```

- **Prompt**: the standard "Begin a new autonomous session and work it to completion" prompt.
- **Trigger**: `periodic` — it restarts after the configured `trigger_interval_seconds` (default 45
  s) after completion.
- **Owner**: `autonomous`.

This gives you a single periodic autonomous session surfacing under the `[AUTONOMOUS]` badge at
`GET /sessions?owner_id=autonomous`.

______________________________________________________________________

## Defining multiple sessions

Add entries under `autonomous.sessions` in the config to define named sessions. Each entry has:

| Key                        | Default      | Description                                                                                                                |
| -------------------------- | ------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `name`                     | *(required)* | Unique identifier for the session definition.                                                                              |
| `prompt`                   | `""`         | Custom kickoff prompt. When empty, the standard "begin a new autonomous session and work it to completion" prompt is used. |
| `trigger_type`             | `"periodic"` | `"periodic"` (wait `trigger_interval_seconds`) or `"on_close"` (continuous).                                               |
| `trigger_interval_seconds` | `45.0`       | Delay between completion and restart for `"periodic"`. Ignored for `"on_close"`.                                           |
| `enabled`                  | `true`       | When `false`, the definition is skipped — no session is created for it.                                                    |

Once `autonomous.sessions` is non-empty, each enabled definition becomes its own session with its
own prompt and trigger. Each maps to a distinct pseudo-owner (`autonomous:<name>`), so sessions run
independently and cannot overlap with themselves.

### Example — one periodic + one continuous session

```json
"autonomous": {
  "enabled": true,
  "sessions": [
    {
      "name": "default",
      "prompt": "",
      "trigger_type": "periodic",
      "trigger_interval_seconds": 45.0,
      "enabled": true
    },
    {
      "name": "continuous-triage",
      "prompt": "Begin an autonomous triage session.  Scan open tickets and investigate the oldest unassigned item.",
      "trigger_type": "on_close",
      "enabled": true
    }
  ]
}
```

______________________________________________________________________

## Triggers

A session's `trigger_type` controls how it is re-triggered after a run completes:

- **`periodic`** — after completion the runner waits `trigger_interval_seconds`, then starts a fresh
  run. This is the default and matches the pre-existing single-session pacing.
- **`on_close`** — the runner restarts the session immediately (continuous mode) as soon as the
  previous run completes, rather than waiting for an interval.

### Manual one-shot trigger

Any enabled session can be run on demand:

- `POST /autonomous/definitions/{name}/run` — starts a one-shot run. Returns `200` with the new
  `session_id`, or `409` if that definition already has an active session (a run cannot overlap
  itself).

### Listing definitions

- `GET /autonomous/definitions` — lists all session definitions with their current `owner_id`,
  trigger details, `enabled` flag, prompt, and the `active_session_id` of any currently-open run
  (`null` when none is active).

______________________________________________________________________

## Safety & audit

- **Dedup / lock** — a session cannot overlap with itself. A manual trigger returns `409` while a
  run of the same definition is active.
- **Confirmation gating** — confirmation-gated mutations remain gated inside autonomous runs; the
  agent still drafts a plan and awaits operator approval before executing tool calls. The one
  exception: a **user-requested ticket** (one the operator explicitly asks the agent to file, e.g.
  "file a ticket for X") is treated as pre-authorized — the agent includes `kind: user-request` and
  `priority: high` markers in the ticket metadata and immediately approves it out of
  draft/`human_issue_approval` in the same turn, since the filing request constitutes consent for
  both filing and approval. Auto-filed chores and feedback tickets still flow through the normal
  approval gate.
- **Auditability** — each run records its definition name, trigger reason, start/end and summary, so
  autonomous activity is traceable.

______________________________________________________________________

## See also

- [Configuration reference](../configuration.md) — full `autonomous.*` settings documentation.
- API reference — `GET /autonomous/definitions` and `POST /autonomous/definitions/{name}/run`.
