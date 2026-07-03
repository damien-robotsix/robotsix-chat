Isolate cognee's Langfuse OTLP tracing from llmio's global tracer provider (`skip_set_global`):
cognee spans were landing in the main robotsix-chat Langfuse project instead of
robotsix-chat-cognee.
