# Ticket Poll — ticket-state lookup via the component roster

You have a `ticket_poll` tool that queries the mill board API. This tool routes through
`component_request` (roster-based connectivity) when available, falling back to the direct board API
when the roster is unavailable — it is reliable as the primary path for checking ticket state.

## When to use it

- **Primary ticket-state check** — `ticket_poll` is your go-to tool for checking a ticket's current
  state. It uses the same roster-based connectivity as `component_request` and is always preferred
  for single-ticket lookups.
- **Independent verification** — use `ticket_poll` as a second source (alongside
  `component_request`) to satisfy the terminal-state double-check requirement.
- **Periodic monitoring** — every monitor tick, call `ticket_poll` (or `component_request`) to
  live-GET the ticket state before reporting any change.

## Allowed operation

| Tool          | Description                                                    |
| ------------- | -------------------------------------------------------------- |
| `ticket_poll` | HTTP GET to the board API; returns the ticket's current state. |

The tool signature is:

```python
ticket_poll(ticket_id: str) -> str
```

## Return value

A JSON string with these fields:

- `ticket_id` — the ticket identifier you supplied
- `state` — the ticket's current state string (e.g. `"BLOCKED"`, `"IN_PROGRESS"`, `"DONE"`), or
  `null` when the state could not be determined
- `error` — empty string on success, or a diagnostic message on failure

## Safety

- **Read-only** — GET only; no state mutation is possible through this tool.
- **Roster-first routing** — prefers the component roster (`component_request`) when available;
  falls back to the direct board API when the roster is absent. This means the tool shares the same
  connectivity path as `component_request` and is equally reliable.
- **Timeout** — the request has a short timeout; one request per call.

## Example calls

```python
# Basic state check
ticket_poll("20250101T120000Z-my-ticket-a1b2")

# Combined with component_request for double-check (terminal-state verification)
# 1. ticket_poll("20250101T120000Z-my-ticket-a1b2")     → state: "DONE"
# 2. component_request("mill", "GET", "/tickets/...")    → confirm: "DONE"
```
