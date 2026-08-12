# Ticket Poll — ticket-state lookup and PR merging via the component roster

You have `ticket_poll`, `ticket_poll_batch`, `merge_pull_request`, and `find_ticket_by_pr` tools
that interact with the mill board API. These tools route through `component_request` (roster-based connectivity) when
available, falling back to the direct board API when the roster is unavailable — they are reliable
as the primary path for checking ticket state and merging approved PRs.

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
- **Merge approved PRs** — use `merge_pull_request` when a ticket is in `waiting_auto_merge` or
  `human_mr_approval` state and its associated PR has been approved. This directly calls the mill
  board's merge-now endpoint, bypassing the need for auto-merge to be enabled on the target
  repository.
- **Find ticket by PR** — use `find_ticket_by_pr` when you know a PR URL (e.g.
  `https://github.com/owner/repo/pull/656`) but not the associated ticket ID. This is a direct
  lookup — one API round-trip instead of enumerating all tickets and filtering client-side.

## Allowed operations

| Tool                          | Description                                                                  |
| ----------------------------- | ---------------------------------------------------------------------------- |
| `ticket_poll`                 | HTTP GET to the board API; returns the ticket's current state.               |
| `ticket_poll_batch`           | Concurrent HTTP GETs for multiple tickets; returns full details for triage.  |
| `merge_pull_request`          | HTTP POST to merge the approved PR associated with a ticket.                 |
| `find_ticket_by_pr`           | HTTP GET to find the ticket linked to a given PR URL.                        |
| `prioritize_all_open_tickets` | Lists all open, unflagged tickets and sets priority on every one in a batch. |

The tool signatures are:

```python
ticket_poll(ticket_id: str) -> str
ticket_poll_batch(ticket_ids: list[str]) -> str
merge_pull_request(ticket_id: str) -> str
find_ticket_by_pr(pr_url: str) -> str
prioritize_all_open_tickets() -> str
```

## Lifecycle and priority mutation endpoints (via `component_request`)

When you need to change a ticket's state or priority, use `component_request` with the correct HTTP
method and path below. **Never guess or fabricate paths** — every mutating operation on a ticket has
a documented endpoint. Guessing a wrong method (e.g. `PATCH`) or a wrong path (e.g. `/prioritize`)
causes 4xx errors and wastes turns.

| Operation         | Method | Path                           | Notes                                                               |
| ----------------- | ------ | ------------------------------ | ------------------------------------------------------------------- |
| Toggle priority   | POST   | `/tickets/{id}/priority`       | Set or clear a ticket's priority flag.                              |
| Resume blocked    | POST   | `/tickets/{id}/resume-blocked` | Body: `{"justification": "why this ticket can safely resume now"}`. |
| Mark done         | POST   | `/tickets/{id}/mark-done`      | Transition a ticket to the terminal `done` state.                   |
| Merge PR          | POST   | `/tickets/{id}/merge-now`      | Prefer the dedicated `merge_pull_request` tool.                     |
| File a new ticket | POST   | `/tickets/ingest`              | Submit a ticket spec for ingestion into the board.                  |
| Read ticket state | GET    | `/tickets/{id}`                | Prefer the dedicated `ticket_poll` / `ticket_poll_batch` tools.     |
| List tickets      | GET    | `/tickets`                     | Query parameters: `state`, `limit`, etc.                            |

All mutation endpoints require authorization — the `component_request` tool applies the correct auth
headers from the roster automatically. However, **mutations must still be authorized by the
operator** before you call them (see the main prompt's MUTATION AUTHORIZATION section).

**Anti-patterns — do NOT do these:**

- `PATCH /tickets/{id}` — there is no PATCH endpoint for tickets; use POST with the specific path.
- `POST /prioritize` or `POST /tickets/prioritize` — the correct path is `/tickets/{id}/priority`.
- Guessing a path from an operation name — always consult this table first.

## Return values

### `ticket_poll`

A JSON string with these fields:

- `ticket_id` — the ticket identifier you supplied
- `state` — the ticket's current state string (e.g. `"BLOCKED"`, `"IN_PROGRESS"`, `"DONE"`), or
  `null` when the state could not be determined
- `error` — empty string on success, or a diagnostic message on failure
- `unexpected_terminal` — a human-readable diagnostic string when the ticket reached a terminal
  state (`CLOSED` or `DONE`) without ever passing through an active-work state (`APPROVED`,
  `IN_PROGRESS`, or `BLOCKED`); `null` when the transition looks normal or when the data carries
  insufficient history to decide. Use this to detect tickets that were closed prematurely — e.g. a
  `DRAFT → CLOSED` transition without approval — and alert the operator.
- `cache_caveat` — *(present only when the board API was unreachable and the response was served
  from the ticket-state cache)* a human-readable staleness note, e.g.
  `[last-known state — board API unreachable; showing cached state from 120s ago]`. Entries older
  than 1 hour are flagged as `stale`.

### `ticket_poll_batch`

A JSON string with a `tickets` array. Each element has:

- `ticket_id` — the ticket identifier
- `state` — the ticket's current state string (or `null`)
- `data` — the full JSON response from `GET /tickets/{id}` (includes events, history, comments,
  cycle metadata)
- `error` — empty string on success, or a diagnostic message on failure
- `unexpected_terminal` — same semantics as in `ticket_poll` above

### `merge_pull_request`

A status message string from the mill API — either a success confirmation or an error describing why
the merge failed (e.g. the PR is not approved, conflicts exist, or required status checks have not
passed). The tool routes through the component roster when available, falling back to the direct
board API on any failure.

### `find_ticket_by_pr`

A JSON string with these fields:

- `ticket_id` — the full ticket ID of the matching ticket, or `null` when no match was found
- `state` — the ticket's current state string, or `null`
- `pr_url` — the PR URL that was looked up (echoed back)
- `error` — empty string on success, or a diagnostic message when no matching ticket was found or
  the board API was unreachable

### `prioritize_all_open_tickets`

A JSON string with these fields:

- `prioritized` — number of tickets whose priority was successfully set
- `skipped` — number of tickets already prioritized (no action taken)
- `errors` — number of tickets where the priority toggle failed
- `total_open` — total number of open tickets considered (prioritized + skipped)
- `results` — an array of per-ticket outcome objects, each with `ticket_id`, `state`, `ok`
  (boolean), and `error` (empty on success)
- `note` — *(present when there were no tickets to prioritize)* a human-readable message (e.g. "No
  open, unflagged tickets to prioritize.")
- `error` — *(present only on a listing failure)* a diagnostic message when the board ticket list
  could not be fetched

This tool replaces the manual sequence of listing tickets, identifying unflagged ones, and toggling
priority on each individually. Call it when the user asks to "prioritize tickets" or "prioritize all
open tickets."

## ID resolution

Both `ticket_poll` and `ticket_poll_batch` resolve paraphrased / abbreviated ticket IDs against the
live board before making any per-ticket request. This means you can pass an ID derived from
narrative text (e.g. `...-my-ticket-a3f2`) and it will be mapped to the full ticket ID on the board.
Resolution tries, in order:

1. **Exact match** — the candidate ID appears verbatim on the board.
2. **Hash-suffix match** — the last 4 hex chars (e.g. `a3f2`) uniquely match one ticket.
3. **Slug-substring match** — the non-timestamp portion appears as a substring of exactly one
   ticket's full ID.

When resolution succeeds, the resolved full ID is used for the request and returned in the response.
When it fails — the candidate is ambiguous or the board is unreachable — the original ID is still
attempted (which may surface a 404).

## Safety

- **`ticket_poll` / `ticket_poll_batch` are read-only** — GET only; no state mutation is possible
  through these two tools.
- **`merge_pull_request` and `prioritize_all_open_tickets` are mutating** — they issue POST requests
  that alter ticket state. Only call them when the preconditions are met: `merge_pull_request`
  requires an approved PR in `waiting_auto_merge` or `human_mr_approval` state;
  `prioritize_all_open_tickets` should be called when the operator asks to prioritize tickets. Do
  not call either speculatively.
- **Roster-first routing** — prefers the component roster (`component_request`) when available;
  falls back to the direct board API when the roster is absent. This means the tools share the same
  connectivity path as `component_request` and are equally reliable.
- **Timeout** — each request has a short timeout; failures are per-ticket (one down ticket won't
  break the batch).
- **Connectivity vs. state.** A tool call failure (timeout, unreachable, HTTP 5xx, DNS error) is a
  transient I/O error — it tells you nothing about the ticket's actual state. Do **not** conflate
  "the API returned an error" with "the ticket is closed," "the ticket is being tracked by a
  monitor," or any other conclusion about the ticket. When a call fails, report the failure honestly
  (e.g. "could not reach the board API — will retry") and do not fabricate state from the absence of
  data. Only state facts about a ticket when you have a successful response containing those facts.
- **Graceful degradation via ticket-state cache.** When the board API is unreachable, `ticket_poll`
  falls back to the last-known state from an in-memory cache (populated by mill push events and
  prior successful polls). The cached response includes a `cache_caveat` field with a staleness note
  (e.g. `[last-known state — board API unreachable; showing cached state from 120s ago]`). Use this
  to surface last-known state with an honest caveat rather than saying "I can't confirm." Entries
  older than 1 hour are flagged as `stale`. Only state facts about a ticket when you have a
  successful response containing those facts.

## Example calls

```python
# Basic state check
ticket_poll("20250101T120000Z-my-ticket-a1b2")

# Bulk triage of blocked tickets — fetch all at once, then classify
ticket_poll_batch(
    [
        "20250101T120000Z-ticket-a-a1b2",
        "20250102T090000Z-ticket-b-c3d4",
        "20250102T150000Z-ticket-c-e5f6",
    ]
)
# → inspect each ticket's data.events history to identify failure signatures:
#   "implement-loop/3of3", "git-failure", "capability-gap", ...

# Combined with component_request for double-check (terminal-state verification)
# 1. ticket_poll("20250101T120000Z-my-ticket-a1b2")     → state: "DONE"
# 2. component_request("mill", "GET", "/tickets/...")    → confirm: "DONE"

# Merge an approved PR when a ticket is in waiting_auto_merge or human_mr_approval
merge_pull_request("20250101T120000Z-my-ticket-a1b2")
# → "HTTP 200\n{\"status\": \"merged\", ...}"

# Prioritize all open, unflagged tickets in a single call
prioritize_all_open_tickets()
# → {"prioritized": 5, "skipped": 2, "errors": 0, "total_open": 7, "results": [...]}
```
