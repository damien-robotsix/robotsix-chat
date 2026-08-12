# Langfuse trace inspection

Read-only tool that queries the Langfuse observability API to fetch and summarise recent traces.
Primarily used to diagnose implement-stage failures by inspecting the agent's own LLM-call traces
linked to a specific ticket or run.

## When to use it

- When the mill's implement stage fails to produce a PR and the assistant needs to inspect the LLM
  traces to understand why.
- When a ticket or run id is known and the assistant wants to see the most recent traces emitted
  during that run.
- When debugging an anomalous LLM response — fetch the trace by its id for a full structured
  summary.

## Allowed operation

| Tool                     | Description                                                        |
| ------------------------ | ------------------------------------------------------------------ |
| `inspect_langfuse_trace` | Fetch one or more Langfuse traces and return a structured summary. |

## Tool signatures

```python
inspect_langfuse_trace(
    trace_id: str = "",
    ticket_id: str = "",
    limit: int = 5,
) -> str
```

- Exactly one of `trace_id` or `ticket_id` must be provided.
- When `trace_id` is given: fetch that single trace and return a detailed summary (name, timestamps,
  input/output tokens, total cost, top-level observations, scores).
- When `ticket_id` is given: search for traces whose tags include `ticket_id:<value>` (most recent
  first) up to `limit`, and return a summary list (trace id, name, timestamp, duration, cost).
- `limit` caps the number of traces returned (default 5, max configured in settings).

## Return value

A JSON string with a `traces` list. Each trace entry carries:

| Field          | Description                                      |
| -------------- | ------------------------------------------------ |
| `id`           | Langfuse trace id                                |
| `name`         | Trace name (e.g. "implement", "chat-turn")       |
| `timestamp`    | ISO-8601 start time                              |
| `userId`       | User/agent id attached to the trace              |
| `latency`      | End-to-end duration in seconds                   |
| `totalCost`    | Total cost in USD                                |
| `usage`        | `{promptTokens, completionTokens, totalTokens}`  |
| `observations` | Count of observations (LLM calls, spans, events) |
| `scores`       | List of `{name, value}` score pairs              |

When `ticket_id` is given, an additional `ticket_id` field echoes the search key and `limit` echoes
the cap used.

## Safety

- Read-only — never mutates anything in Langfuse.
- Only reaches the configured Langfuse host (default `https://cloud.langfuse.com`).
- Authenticates with Basic auth using the configured public/secret key pair — no user credentials
  are ever exposed to the agent.
- The tool is only available when `langfuse_inspect.enabled` is `true` in the server config.

## Error handling — read before acting on a failure

- **Transient errors are retried automatically.** The tool retries timeouts, 5xx responses, and
  network failures up to 2 times with exponential backoff before returning an error.  If you
  receive an error, it has already survived multiple attempts — do NOT retry the tool repeatedly
  hoping for a different result.
- **Do NOT claim credentials are missing.** The tool authenticates via Basic auth using the
  `langfuse.projects` config block — it checks credentials up front and returns an explicit
  "credentials are not configured" error when they are absent.  Any other error (HTTP status,
  timeout, network failure) means the credentials *were* sent and the Langfuse host rejected or
  did not receive the request.  Blaming a missing credential when the tool did not return the
  credential-specific error message wastes time and erodes trust.
- **Do NOT suggest a restart.** The environment-variable credential path
  (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`) was decommissioned — the tool reads credentials
  from the server's JSON config file at startup.  A restart will not change the credential state
  and will not fix a network, timeout, or API-level error.
- **The self-check you already have:** call the tool with any known trace id or ticket id.  If
  it returns data, Langfuse access is working.  If it returns an error, read the error message
  literally — it tells you exactly what failed (network, timeout, HTTP status, missing
  credentials).  Do not layer your own theory on top of a clear error message.

## Example calls

```text
# Inspect a specific known trace
inspect_langfuse_trace(trace_id="01J...abc")

# Search for traces linked to a ticket
inspect_langfuse_trace(ticket_id="20260727T001240Z-add-capability-5bd6", limit=5)
```
