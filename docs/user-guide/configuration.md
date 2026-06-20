# Configuration

robotsix-chat is configured via a **three-layer cascade**:

1. **pydantic defaults** — built into the `Settings` model
2. **YAML config file** — `config/chat.local.yaml` (path overridable via `CHAT_CONFIG_PATH`)
3. **Environment variables** — override any YAML or default value

Every setting below can be placed in the YAML file (using the tree path shown) or
set as an environment variable.

## Top-level settings

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `llmio.model_level` | `LLMIO_MODEL_LEVEL` | `3` | LLM capability level (1–2 use OpenRouter with an API key; 3 uses the Claude SDK keyless transport) |
| `llmio.api_key` | `LLMIO_API_KEY` | `""` | OpenRouter API key (required for levels 1–2; ignored for level 3) |
| `agent.instruction` | `AGENT_INSTRUCTION` | `"You are a helpful assistant."` | System prompt sent to the LLM |
| `server.host` | `SERVER_HOST` | `"127.0.0.1"` | IP address the server binds to |
| `server.port` | `SERVER_PORT` | `8000` | TCP port the server listens on |
| `server.log_level` | `LOG_LEVEL` | `"INFO"` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `server.cors_allow_origins` | `CORS_ALLOW_ORIGINS` | `[]` | CORS allowed origins (YAML: JSON array; env: comma-separated list) |
| `server.correlation_id_header` | `CORRELATION_ID_HEADER` | `"X-Request-ID"` | HTTP header used to propagate request IDs |

## HTTP Basic Auth

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `auth.enabled` | `AUTH_ENABLED` | `false` | Enable HTTP Basic Auth (production: always `1` in the deploy compose file) |
| `auth.username` | `AUTH_USERNAME` | `"admin"` | Basic Auth username |
| `auth.password` | `AUTH_PASSWORD` | `""` | Basic Auth password (the deploy compose file enforces `CHAT_AUTH_PASSWORD`) |

## Memory (cognee)

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `memory.enabled` | `MEMORY_ENABLED` | `false` | Enable embedded cognee long-term memory |
| `memory.data_dir` | `MEMORY_DATA_DIR` | `".data/cognee"` | Directory for cognee storage (keep on `.data` bind mount for production) |
| `memory.recall_search_type` | `MEMORY_RECALL_SEARCH_TYPE` | `"GRAPH_COMPLETION"` | Cognee recall search strategy |
| `memory.llm.provider` | `MEMORY_LLM_PROVIDER` | `"custom"` | Provider for memory extraction (cognee's litellm `custom` provider) |
| `memory.llm.model` | `MEMORY_LLM_MODEL` | `"openrouter/deepseek/deepseek-v4-flash"` | Model for memory extraction |
| `memory.llm.endpoint` | `MEMORY_LLM_ENDPOINT` | `"https://openrouter.ai/api/v1"` | OpenRouter endpoint |
| `memory.llm.api_key` | `MEMORY_LLM_API_KEY` | `""` | OpenRouter API key for memory extraction |
| `memory.embedding.provider` | `MEMORY_EMBEDDING_PROVIDER` | `"openai_compatible"` | Embedding provider type |
| `memory.embedding.model` | `MEMORY_EMBEDDING_MODEL` | `"bge-m3"` | Embedding model name |
| `memory.embedding.endpoint` | `MEMORY_EMBEDDING_ENDPOINT` | `""` | Self-hosted embedding server URL (e.g. `http://host:11434/v1`) |
| `memory.embedding.dimensions` | `MEMORY_EMBEDDING_DIMENSIONS` | `1024` | Embedding vector size |
| `memory.embedding.api_key` | `MEMORY_EMBEDDING_API_KEY` | `"ollama"` | API key for the embedding server |
| `memory.embedding.huggingface_tokenizer` | `MEMORY_EMBEDDING_TOKENIZER` | `"BAAI/bge-m3"` | HuggingFace tokenizer name (optional fallback) |

## Mill (broker integration)

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `mill.enabled` | `MILL_ENABLED` | `false` | Enable robotsix-mill broker integration (adds `consult_mill` tool) |
| `mill.broker_host` | `MILL_BROKER_HOST` | `"ai-broker.robotsix.net"` | Agent-comm broker hostname |
| `mill.broker_port` | `MILL_BROKER_PORT` | `443` | Broker TCP port |
| `mill.broker_scheme` | `MILL_BROKER_SCHEME` | `"https"` | Transport scheme |
| `mill.broker_token` | `MILL_BROKER_TOKEN` | `""` | Bearer token registered on the broker for `agent_id` |
| `mill.agent_id` | `MILL_AGENT_ID` | `"robotsix-chat"` | Agent identity on the broker |
| `mill.board_manager_id` | `MILL_BOARD_MANAGER_ID` | `"board-manager-robotsix-mill"` | Target board manager agent ID |
| `mill.repo_id` | `MILL_REPO_ID` | `""` | Optional target repo (blank = board manager decides) |
| `mill.timeout` | `MILL_TIMEOUT` | `240.0` | Broker request timeout in seconds |

## Example YAML

```yaml
# config/chat.local.yaml
llmio:
  model_level: 3

server:
  host: "0.0.0.0"
  port: 8080
  cors_allow_origins: ["https://chat.example.com"]

auth:
  enabled: true
  username: "admin"
  password: ""  # set via AUTH_PASSWORD env var in production
```
