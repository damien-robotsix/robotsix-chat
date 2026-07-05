"""SSE wire-format constants — single source of truth for tests and consumers."""

SSE_CONTENT_TYPE = "text/event-stream"
SSE_TOKEN_TYPE = "token"  # noqa: S105 — SSE event type name, not a credential
SSE_DONE_TYPE = "done"
SSE_ERROR_TYPE = "error"

# The agent returns its reply as a single block only once the whole pipeline
# (memory recall, the LLM, any tool calls) completes — which can be many seconds
# with no bytes on the wire. A silent connection that long gets dropped by the
# browser/proxy ("NetworkError"). Emit an SSE *comment* heartbeat immediately and
# on this interval so the data channel never goes quiet. Comments carry no
# ``data:`` line, so clients ignore them.
SSE_HEARTBEAT_INTERVAL = 5.0
SSE_HEARTBEAT_FRAME = b": keepalive\n\n"
