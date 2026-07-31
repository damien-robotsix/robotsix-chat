# Ticket Poll — ticket-state lookup via the component roster

You have `ticket_poll` and `ticket_poll_batch` tools that query the mill board API. These tools
route through `component_request` (roster-based connectivity) when available, falling back to the
direct board API when the roster is unavailable — they are reliable as the primary path for checking
ticket state.

## When to use it

- **Primary ticket-state check** — `ticket_poll` is your go-to tool for checking a ticket's current
  state. It uses the same roster-based connectivity as `component_request` and is always preferred
  for single-ticket lookups.
- **Independent verification** — use `ticket_poll` as a second source (alongside
  `component_request`) to satisfy the terminal-state double-check requirement.
- **Periodic monitoring** — every monitor tick, call `ticket_poll` (or `component_request`) to
  live-GET the ticket state before reporting any change.
- **Bulk triage** — use `ticket_poll_batch` to fetch full details (state, history, events, comments)
  for many tickets in one call, then classify them by failure signature.

## Allowed operations

| Tool                | Description                                                                 |
| ------------------- | --------------------------------------------------------------------------- |
| `ticket_poll`       | HTTP GET to the board API; returns the ticket's current state.              |
| `ticket_poll_batch` | Concurrent HTTP GETs for multiple tickets; returns full details for triage. |

The tool signatures are:

```python
ticket_poll(ticket_id: str) -> str
ticket_poll_batch(ticket_ids: list[str]) -> str
```

## Return values

### `ticket_poll`

A JSON string with these fields:

- `ticket_id` — the ticket identifier you supplied
- `state` — the ticket's current state string (e.g. `"BLOCKED"`, `"IN_PROGRESS"`, `"DONE"`), or
  `null` when the state could not be determined
- `error` — empty string on success, or a diagnostic message on failure

### `ticket_poll_batch`

A JSON string with a `tickets` array. Each element has:

- `ticket_id` — the ticket identifier
- `state` — the ticket's current state string (or `null`)
- `data` — the full JSON response from `GET /tickets/{id}` (includes events, history, comments,
  cycle metadata)
- `error` — empty string on success, or a diagnostic message on failure

## Safety

- **Read-only** — GET only; no state mutation is possible through these tools.
- **Roster-first routing** — prefers the component roster (`component_request`) when available;
  falls back to the direct board API when the roster is absent. This means the tools share the same
  connectivity path as `component_request` and are equally reliable.
- **Timeout** — each request has a short timeout; failures are per-ticket (one down ticket won't
  break the batch).

## Example calls

```python
# Basic state check
ticket_poll("20250101T120000Z-my-ticket-a1b2")

# Bulk triage of blocked tickets — fetch all at once, then classify
ticket_poll_batch([
    "20250101T120000Z-ticket-a-a1b2",
    "20250102T090000Z-ticket-b-c3d4",
    "20250102T150000Z-ticket-c-e5f6",
])
# → inspect each ticket's data.events history to identify failure signatures:
#   "implement-loop/3of3", "git-failure", "capability-gap", ...

# Combined with component_request for double-check (terminal-state verification)
# 1. ticket_poll("20250101T120000Z-my-ticket-a1b2")     → state: "DONE"
# 2. component_request("mill", "GET", "/tickets/...")    → confirm: "DONE"
```
