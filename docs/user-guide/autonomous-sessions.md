# Autonomous Sessions

Autonomous sessions are self-directed agent loops: the agent independently picks a subject, drafts a
step-by-step plan, presents the plan to the operator for review, then — once the operator comments
on the plan — executes it through tool calls. Sessions stay open after completion; the operator
explicitly closes them.

**Named session definitions** let you configure multiple autonomous sessions with distinct prompts
and restart triggers. Out of the box, a single `"default"` session is synthesized that matches the
pre-existing single-session behavior exactly — no configuration is required to get started.

______________________________________________________________________

## Overview

When autonomous mode is enabled (`autonomous.enabled=true`), each configured session runs an
independent loop over its own pseudo-owner (`autonomous` for the `"default"` preset,
`autonomous:<name>` for named sessions). A session cannot overlap with itself: a new run does not
start while the previous run of the same session is active.

### Lifecycle

Each session run follows the same flow:

1. **Spawn** — the runner kicks off an initial agent turn with the session's kickoff prompt.
2. **Plan & propose** — the agent picks a subject and drafts a plan, then emits the proposal marker
   (`---PROPOSAL READY---` by default). The session enters the `proposal` state and waits for the
   operator.
3. **Execute** — when the operator comments on the plan, the session enters the `executing` state
   and auto-cycles through tool calls.
4. **Complete** — when the agent emits the completion marker (`---AUTONOMOUS COMPLETE---` by
   default), the session is marked `completed`, but stays open. The operator explicitly closes it.
5. **Re-trigger** — depending on the session's trigger, a fresh run is scheduled after completion
   (see [Triggers](#triggers)).

### The `[AUTONOMOUS]` badge

Autonomous sessions surface in the operator's session list under their pseudo-owner with a
`[AUTONOMOUS]` badge and (optionally) a session color accent, so they are easy to distinguish from
interactive chats.

______________________________________________________________________

## Getting started (default preset)

If you change nothing, the runner synthesizes a single session named `"default"`:

- **Prompt**: the standard "Pick a subject and draft a plan" prompt (or `autonomous.initial_task`
  when set).
- **Trigger**: `periodic` — it restarts `autonomous.continue_interval_seconds` (default 45 s) after
  completion.
- **Owner**: `autonomous`.

This preserves the pre-existing behavior exactly — the `GET /sessions?owner_id=autonomous` endpoint
and the `[AUTONOMOUS]` badge keep working.

______________________________________________________________________

## Defining multiple sessions

Add entries under `autonomous.sessions` in the config to define named sessions. Each entry has:

| Key                        | Default      | Description                                                                       |
| -------------------------- | ------------ | --------------------------------------------------------------------------------- |
| `name`                     | *(required)* | Unique identifier for the session definition.                                     |
| `prompt`                   | `""`         | Custom kickoff prompt. When empty, the standard subject-selection prompt is used. |
| `trigger_type`             | `"periodic"` | `"periodic"` (wait `trigger_interval_seconds`) or `"on_close"` (continuous).      |
| `trigger_interval_seconds` | `45.0`       | Delay between completion and restart for `"periodic"`. Ignored for `"on_close"`.  |
| `enabled`                  | `true`       | When `false`, the definition is skipped — no session is created for it.           |

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
