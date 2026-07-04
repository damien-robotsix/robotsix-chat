BREAKING: Config migrated from YAML cascade to JSON (`robotsix-config`).

The `config/chat.local.yaml` config file and all env-var config overrides (`LLMIO_*`, `MEMORY_*`,
`LANGFUSE_*`, `MILL_*`, `CALENDAR_*`, etc.) are no longer read by the app. Only
`ROBOTSIX_CONFIG_FILE` (file locator) is consumed from env.

OPS CUTOVER — required before redeployment: Transcribe the following from central-deploy's env store
into `/home/app/config/config.json` on the deploy host BEFORE restarting:

| Env var (old)                   | JSON path                      | Known / Notes                         |
| ------------------------------- | ------------------------------ | ------------------------------------- |
| LLMIO_MODEL_LEVEL               | llmio_model_level              | 4                                     |
| LLMIO_API_KEY                   | llmio_api_key                  | from env store                        |
| MEMORY_ENABLED                  | memory.enabled                 | true                                  |
| MEMORY_LLM_API_KEY              | memory.llm.api_key             | OpenRouter key                        |
| MEMORY_EMBEDDING_ENDPOINT       | memory.embedding.endpoint      | https://embed.robotsix.net/v1         |
| MEMORY_EMBEDDING_API_KEY        | memory.embedding.api_key       | bearer token                          |
| LANGFUSE_PUBLIC_KEY             | langfuse.public_key            | main project key                      |
| LANGFUSE_SECRET_KEY             | langfuse.secret_key            | main project secret                   |
| LANGFUSE_HOST (if set)          | langfuse.host                  | custom host or omit for cloud default |
| MEMORY_LANGFUSE_PUBLIC_KEY      | memory.langfuse.public_key     | robotsix-chat-cognee project key      |
| MEMORY_LANGFUSE_SECRET_KEY      | memory.langfuse.secret_key     | robotsix-chat-cognee project secret   |
| MILL_ENABLED / MILL\_\*         | mill.enabled / mill.\*         | from env store                        |
| MILL_BROKER_TOKEN               | mill.broker_token              | from env store                        |
| CALENDAR_ENABLED / CALENDAR\_\* | calendar.enabled / calendar.\* | from env store                        |
| CALENDAR_BROKER_TOKEN           | calendar.broker_token          | from env store                        |
| AUTH\_\* (gateway-only)         | N/A — central-deploy gateway   | no change needed                      |

WARNING: The 2026-07-03 deployment previously lost env values during a restart. Verify all env store
values before cutover.
