Main-agent Langfuse tracing: export `LANGFUSE_BASE_URL` (the name `robotsix-llmio` reads) alongside
`LANGFUSE_HOST`. Without it the OTLP exporter fell back to Langfuse Cloud US and every span batch
was rejected with 401, so the self-hosted project received no traces.
