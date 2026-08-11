# Epic Decomposition — analyze epics and plan child tickets

You have a `decompose_epic` tool that fetches epic ticket data, history, and existing
children from the mill board API so you can create a concrete, dependency-ordered child
ticket plan.

## When to use it

- **Spawn exhaustion detected** — when a monitor reports that the implement agent exhausted
  all 3 spawn attempts on an epic, call `decompose_epic` to get a structured analysis before
  creating child tickets.
- **Scope-too-broad diagnosis** — when an epic has been sitting in `IN_PROGRESS` or
  `BLOCKED` across multiple implement cycles without producing a mergeable change.
- **Pre-ingest triage** — when the operator asks you to ingest a new epic and you want to
  proactively plan its decomposition before the first implement attempt.

## Allowed operations

| Tool              | Description                                                             |
| ----------------- | ----------------------------------------------------------------------- |
| `decompose_epic`  | Fetch epic data + history; return a structured decomposition plan.      |

The tool signature is:

```python
decompose_epic(epic_ticket_id: str) -> str
```

## Return value

A JSON string with these fields:

- `epic_ticket_id` — the epic identifier you supplied.
- `epic_title` — the epic's title from the board.
- `epic_state` — the epic's current state.
- `epic_kind` — the ticket kind (should be `"epic"`).
- `epic_description` — the epic description text (truncated to 2000 characters).
- `epic_events_summary` — a list of the last 20 events, each with `type`, `timestamp`, and
  `detail`.
- `implement_cycles` — how many implement cycles have run against this epic.
- `spawn_exhausted` — `true` when 3 or more implement cycles have run (the spawn limit).
- `existing_children` — a list of existing child tickets, each with `id`, `title`, and
  `state`.  May be empty when no children exist.
- `existing_children_error` — empty on success, or a diagnostic string when the child-list
  fetch failed.
- `decomposition_plan` — a list of suggested child tickets (see below).  Empty when the epic
  already has children covering its scope.
- `error` — empty on success, or a diagnostic message on failure (e.g. epic not found).

## Decomposition plan

The `decomposition_plan` is a list of suggested child ticket objects, each with:

- `title` — a placeholder title; replace it with a concrete, scoped title derived from the
  epic description.
- `scope` — guidance on how to scope this child (one acceptance criterion, one subsystem, etc.).
- `kind` — suggested ticket kind (usually `"task"`).
- `depends_on` — which preceding child this one depends on, or `"(none — first child)"` for
  the first ticket.

**The plan is a skeleton — you MUST populate it with concrete titles and scopes derived from
the epic description before creating child tickets.**  The tool provides the structural
analysis (spawn exhaustion, event history, existing children); you provide the domain
judgment.

## Creating child tickets

After calling `decompose_epic`, create each child ticket in dependency order via:

```
component_request("mill", "POST", "/tickets/ingest", json_body={
    "title": "...",
    "description": "...",
    "kind": "task",
    "source": "agent",
    "epic_id": "<epic_ticket_id>"
})
```

The board assigns a ticket ID automatically.  Use the epic's `ticket_id` as the `epic_id`
field so the children are linked to the parent epic.
