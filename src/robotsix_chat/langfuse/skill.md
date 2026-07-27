# Langfuse trace inspection

Read-only tool that queries the Langfuse observability API to fetch and
summarise recent traces.  Primarily used to diagnose implement-stage
failures by inspecting the agent's own LLM-call traces linked to a
specific ticket or run.

## When to use it

- When the mill's implement stage fails to produce a PR and the
  assistant needs to inspect the LLM traces to understand why.
- When a ticket or run id is known and the assistant wants to see the
  most recent traces emitted during that run.
- When debugging an anomalous LLM response — fetch the trace by its
  id for a full structured summary.

## Allowed operation

| Tool | Description |
|------|-------------|
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
- When `trace_id` is given: fetch that single trace and return a
  detailed summary (name, timestamps, input/output tokens, total
  cost, top-level observations, scores).
- When `ticket_id` is given: search for traces whose tags include
  `ticket_id:<value>` (most recent first) up to `limit`, and return
  a summary list (trace id, name, timestamp, duration, cost).
- `limit` caps the number of traces returned (default 5, max
  configured in settings).

## Return value

A JSON string with a `traces` list.  Each trace entry carries:

| Field | Description |
|-------|-------------|
| `id` | Langfuse trace id |
| `name` | Trace name (e.g. "implement", "chat-turn") |
| `timestamp` | ISO-8601 start time |
| `userId` | User/agent id attached to the trace |
| `latency` | End-to-end duration in seconds |
| `totalCost` | Total cost in USD |
| `usage` | `{promptTokens, completionTokens, totalTokens}` |
| `observations` | Count of observations (LLM calls, spans, events) |
| `scores` | List of `{name, value}` score pairs |

When `ticket_id` is given, an additional `ticket_id` field echoes the
search key and `limit` echoes the cap used.

## Safety

- Read-only — never mutates anything in Langfuse.
- Only reaches the configured Langfuse host (default
  `https://cloud.langfuse.com`).
- Authenticates with Basic auth using the configured public/secret key
  pair — no user credentials are ever exposed to the agent.
- The tool is only available when `langfuse_inspect.enabled` is
  `true` in the server config.

## Example calls

```
# Inspect a specific known trace
inspect_langfuse_trace(trace_id="01J...abc")

# Search for traces linked to a ticket
inspect_langfuse_trace(ticket_id="20260727T001240Z-add-capability-5bd6", limit=5)
```
