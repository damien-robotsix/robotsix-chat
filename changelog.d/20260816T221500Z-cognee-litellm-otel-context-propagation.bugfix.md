cognee's LLM spend is now priced correctly in its Langfuse project. litellm's
OTEL logger only creates the `litellm_request` GENERATION span when no parent
span is present in the ambient OTEL context; because cognee recall/ingestion
runs inside llmio's chat-turn span, every cognee call made during a chat turn
skipped that span and reached Langfuse as a bare `raw_gen_ai_request` child with
no model, usage or cost attached. Langfuse priced those calls at $0 while
OpenRouter still billed them — on 2026-08-16, 689 of 920 cognee calls were
costed at zero and cost reconciliation reported a 66-80% drift against the
provider on the `robotsix-chat-cognee` project. The cognee OTEL logger now sets
`ignore_context_propagation=True`, so it detaches from the chat-turn context and
emits a fully-costed generation span for every call (and stops mixing cognee
spans into chat traces).
