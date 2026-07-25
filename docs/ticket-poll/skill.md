# Ticket Poll — direct board-API ticket-state lookup

You have a `ticket_poll` tool that queries the mill board API directly to
retrieve a ticket's current state.  This tool bypasses the component roster
and is always available when the board API URL is configured — use it as a
fallback when `component_request` is unavailable or as an independent
verification of ticket state.

## When to use it

- **Primary fallback** — when `component_request` is not among your tools,
  `ticket_poll` is your only way to check ticket state.  Always try it
  before reporting that you cannot check ticket state.
- **Independent verification** — even when `component_request` is available,
  you can use `ticket_poll` as a second source to satisfy the terminal-state
  double-check requirement.
- **Periodic monitoring** — every monitor tick, call `ticket_poll` (or
  `component_request`) to live-GET the ticket state before reporting any
  change.

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
- **No roster dependency** — queries the board API directly; does not require
  `component_request` or the component roster to be available.
- **Timeout** — the request has a short timeout; one request per call.

## Example calls

```python
# Basic state check
ticket_poll("20250101T120000Z-my-ticket-a1b2")

# Combined with component_request for double-check (terminal-state verification)
# 1. ticket_poll("20250101T120000Z-my-ticket-a1b2")     → state: "DONE"
# 2. component_request("mill", "GET", "/tickets/...")    → confirm: "DONE"
```
