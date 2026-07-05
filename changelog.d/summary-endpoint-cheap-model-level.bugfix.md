`POST /summary` (regenerated after every assistant turn) reused the main conversation agent — often
the most expensive configured tier — for a bounded JSON-extraction task. It now runs on a dedicated
agent at a new `summary_model_level` setting (default level 1, the cheapest tier). Unlike
`llmio_model_level`, a missing OpenRouter key for this level is not fatal: the server logs a warning
and falls back to the keyless level 3 instead of failing to start.
