Make the recall concurrency bound process-wide instead of per-instance. The
server builds one `CogneeMemory` per agent — the main chat agent plus one per
background agent via `ReadOnlyMemory(build_memory(…))` — and production runs
six of them, each logging its own "cognee memory configured". They all contend
on the same process-global cognee stores, so the instance-scoped semaphore
introduced alongside `recall_max_concurrency` really admitted 6x its limit.
