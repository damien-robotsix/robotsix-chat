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
| `llmio.model_level` | `LLMIO_MODEL_LEVEL` | `3` | LLM capability level. `1` (cheapest, OpenRouter DeepSeek Flash), `2` (OpenRouter DeepSeek Pro), `3` (Claude SDK Opus — keyless). Levels 1–2 require `llmio.api_key`. |
| `llmio.api_key` | `LLMIO_API_KEY` | `""` | OpenRouter API key. Required for levels 1–2; ignored when using level 3 (Claude SDK, keyless). |
| `agent.instruction` | `AGENT_INSTRUCTION` | `"You are a helpful assistant. You have a local, durable knowledge base (add/append/update/list/read_knowledge_note) for operational notes and lessons you deliberately author — consult it at the start of every session and write durable findings to it. Unlike the stable, human-governed system prompt (which you must not modify), these notes are yours to author and revise by id. This store is distinct from the automatic cognee conversation memory — cognee recalls past exchanges by similarity, while these notes you explicitly create and address by id. Answer quick questions inline. When a request is judged to take a while — multi-step research, long generation, or anything that would stall your reply — call the delegate_task tool to offload it to a background sub-agent. The tool returns a task id immediately; tell the user the work is running in the background and they'll be notified when it finishes.\n\nBoard/mill rules:\n– To READ board/ticket state, always use list_board_tickets or read_board_ticket — these call the SAME HTTP endpoint the user's browser UI consumes, so you see exactly what the user sees.  Never narrate or fabricate ticket states; always verify with the board reader tools first.\n– For WRITE operations (create tickets, migrate, transition), use consult_mill — the broker-based board manager handles writes.\n– Do all board work inline — never offload board actions through delegate_task. Delegate-task results are never returned, so a ticket filed that way may silently fail with no feedback.\n– Before filing ANY new ticket, list_board_tickets for the target repo and check whether an existing OPEN ticket already covers the same intent; comment on / reuse it instead of filing a duplicate. create_board_ticket does this for you automatically and will warn if a similar ticket exists — act on that warning.\n– After creating a ticket via consult_mill, verify it landed on the correct board with list_board_tickets. New tickets default to robotsix-mill regardless of source; if misplaced, request a migration to the correct board (e.g. robotsix-chat) — also inline via consult_mill.\n– Never offer to manually promote a ticket from draft to ready. The draft→ready transition is automatic (auto-pickup); the system picks up tickets on its own once they leave draft.\n– When launching a check loop (start_check_loop) that monitors mill/board/thread/ticket status, set verify_via_board=True. Never assert board/thread status without a fresh consult_mill read — fabricating or narrating status without reading the board is prohibited.\n\nCalendar/task tools:\n– query_calendar, manage_calendar, query_tasks, and manage_tasks already exist in robotsix-chat (built by build_calendar_tools() over the agent-comm broker). They default to disabled (CalendarSettings.enabled=False, requires CALENDAR_BROKER_TOKEN). Never propose building a new Google OAuth or any new calendar integration — the fix is enabling and configuring the existing tools.\n\nEfficiency:\n– If a required tool is missing, state it in one sentence and stop — do not explore alternatives, explain why, or narrate checking for it.\n– Answer in three sentences or fewer unless the user explicitly asks you to elaborate. Do NOT volunteer multi-row markdown tables, timeline/audit dumps, or recap lists — emit those formats ONLY when the user explicitly requests them (e.g. 'show me a table', 'give me the full audit'). Never repeat content already shown earlier in the same conversation.\n– All tools are already loaded and available for the entire session; there is no separate tool-loading step. Never narrate loading, preparing, or fetching tools (e.g. 'I'll load the tools…', 'Let me load the task management tool first') and never announce or run a 'capability check'. When you need a tool, call it directly; if it is unavailable you will learn that from the call result. Do not restate tool descriptions across turns."` | System prompt sent to the LLM.  **Version-governed** — any change must bump `SYSTEM_PROMPT_VERSION` in `src/robotsix_chat/config.py`, add a new entry to the [System Prompt Changelog](system_prompt_changelog.md) with the new SHA256, and keep this table row in sync with the verbatim default. || — (no YAML path) | `CHAT_CONFIG_PATH` | `"config/chat.local.yaml"` | Overrides the path to the YAML config file. Read before the cascade — this is how you point at a different config file at startup. Not a pydantic field; purely an env var. |
| `server.host` | `SERVER_HOST` | `"127.0.0.1"` | IP address the server binds to. |
| `server.port` | `SERVER_PORT` | `8000` | TCP port the server listens on. |
| `server.log_level` | `LOG_LEVEL` | `"INFO"` | Python log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `server.cors_allow_origins` | `CORS_ALLOW_ORIGINS` | `[]` | Origins allowed to call `/chat` cross-origin. YAML: JSON array (`["https://example.com"]` or `["*"]`). Env: comma-separated list (`https://a.com,https://b.com`). Empty = none allowed. |
| `server.correlation_id_header` | `CORRELATION_ID_HEADER` | `"X-Request-ID"` | HTTP header name for the correlation / request-id (both inbound and outbound). |
| `server.idle_timeout_minutes` | `IDLE_TIMEOUT_MINUTES` | `30` | Minutes of no user activity before the UI auto-restarts the conversation. `0` disables the feature. |
| `server.max_background_tasks` | `MAX_BACKGROUND_TASKS` | `5` | Maximum number of concurrently-running background sub-agent tasks per process. |

## Image attachments

Image attachments let the user send pictures alongside or instead of text.
They are delivered as multimodal content so a vision-capable LLM can process
them.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `max_images_per_message` | `MAX_IMAGES_PER_MESSAGE` | `8` | Maximum number of images a client may attach to a single `/chat` request. |
| `max_image_bytes` | `MAX_IMAGE_BYTES` | `5242880` (5 MiB) | Maximum decoded size (bytes) of a single attached image. |
| `allowed_image_media_types` | `ALLOWED_IMAGE_MEDIA_TYPES` | `image/png,image/jpeg,image/gif,image/webp` | Comma-separated list of accepted media types for image attachments. |

**Important:** the default `llmio_model_level = 3` routes to `claude_sdk`, which
currently **drops image content silently** (its internal `_content_to_text()`
flattens non-text parts to `str(...)`).  To have the assistant actually *see*
images, configure a vision-capable OpenRouter model at level 1 or 2 (e.g.
`llmio.model_level: 2` with a multimodal model).  Full level-3 image support
requires an external change to `robotsix_llmio`'s claude_sdk model to map image
parts into the Claude SDK request format.

## HTTP Basic Auth

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `auth.enabled` | `AUTH_ENABLED` | `false` | Enable HTTP Basic Auth gating the browser UI and `/chat`. Truthy: `"1"`, `"true"`, `"yes"`, `"on"`. The production deploy compose file hardcodes this to `1`. |
| `auth.username` | `AUTH_USERNAME` | `"admin"` | Basic Auth username. |
| `auth.password` | `AUTH_PASSWORD` | `""` | Basic Auth password. Required when auth is enabled. In production the deploy compose file enforces `CHAT_AUTH_PASSWORD` and passes it through as `AUTH_PASSWORD`. |

## Conversation

Multi-turn conversation continuity for the browser chat. The server keys
conversations by a per-browser `client_id` (sent automatically by the UI).
Messages within the idle window share a conversation — prior turns are fed
back to the agent and traces are grouped under one session. After the window
expires a fresh conversation starts with empty history.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `conversation.idle_reset_seconds` | `CONVERSATION_IDLE_RESET_SECONDS` | `1800` | Idle gap (seconds) after which the next message starts a new conversation. Default 1800 = 30 minutes. |
| `conversation.max_history_turns` | `CONVERSATION_MAX_HISTORY_TURNS` | `50` | Most recent user/assistant turn-pairs kept per conversation and replayed to the agent (bounds prompt size). |
| `conversation.max_conversations` | `CONVERSATION_MAX_CONVERSATIONS` | `1000` | Maximum number of distinct clients tracked at once (LRU-evicted). Bounds the in-memory store. |

## Memory (cognee)

Long-term agent memory — gives the agent continuity across conversations:
it recalls relevant memory before each reply and persists the exchange after
(the write runs in the background, off the reply's latency path). Disabled by
default; requires `uv sync --extra memory`.

**Architecture:** cognee runs embedded. The extraction LLM is OpenRouter and the
embedding model is a self-hosted, remote OpenAI-compatible server (e.g. Ollama).

When enabled, `memory.llm.api_key` and `memory.embedding.endpoint` are required.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `memory.enabled` | `MEMORY_ENABLED` | `false` | Enable embedded cognee long-term memory. |
| `memory.data_dir` | `MEMORY_DATA_DIR` | `".data/cognee"` | Directory for cognee stores. Keep on a persistent mount (`.data`) so memory survives container redeploys. |
| `memory.recall_search_type` | `MEMORY_RECALL_SEARCH_TYPE` | `"GRAPH_COMPLETION"` | Cognee search type used for recall. `GRAPH_COMPLETION` returns clean, relevant facts as text but costs one (cheap) LLM call per message. `CHUNKS`/`SUMMARIES` are faster but return raw, noisier payloads. |

### Memory LLM (extraction)

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `memory.llm.provider` | `MEMORY_LLM_PROVIDER` | `"custom"` | Provider for memory extraction (cognee's litellm `custom` provider). |
| `memory.llm.model` | `MEMORY_LLM_MODEL` | `"openrouter/deepseek/deepseek-v4-flash"` | Model for memory extraction (graph building / consolidation). |
| `memory.llm.endpoint` | `MEMORY_LLM_ENDPOINT` | `"https://openrouter.ai/api/v1"` | OpenRouter API endpoint. |
| `memory.llm.api_key` | `MEMORY_LLM_API_KEY` | `""` | OpenRouter API key for the extraction LLM. Required when memory is enabled. |

### Memory Embedding

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `memory.embedding.provider` | `MEMORY_EMBEDDING_PROVIDER` | `"openai_compatible"` | Embedding provider type. Must be `openai_compatible` for a self-hosted Ollama endpoint. |
| `memory.embedding.model` | `MEMORY_EMBEDDING_MODEL` | `"bge-m3"` | Embedding model name. |
| `memory.embedding.endpoint` | `MEMORY_EMBEDDING_ENDPOINT` | `""` | Self-hosted embedding server URL (e.g. `http://host:11434/v1`). Required when memory is enabled. |
| `memory.embedding.dimensions` | `MEMORY_EMBEDDING_DIMENSIONS` | `1024` | Embedding vector size. Sticky — changing it invalidates stored vectors. |
| `memory.embedding.api_key` | `MEMORY_EMBEDDING_API_KEY` | `"ollama"` | API key for the embedding server. |
| `memory.embedding.huggingface_tokenizer` | `MEMORY_EMBEDDING_TOKENIZER` | `"BAAI/bge-m3"` | HuggingFace tokenizer name for the embedding model. |

## Mill (broker integration)

robotsix-mill integration over the agent-comm broker. When enabled, the chat
agent gains a `consult_mill` tool that forwards natural-language requests to
the mill's board manager (`board-manager-robotsix-mill`) and relays its reply —
so a user can have the mill track/do development work (create/triage tickets,
ask status) from chat. Disabled by default; requires `uv sync --extra broker`.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `mill.enabled` | `MILL_ENABLED` | `false` | Enable mill broker integration (adds `consult_mill` tool). |
| `mill.broker_host` | `MILL_BROKER_HOST` | `"ai-broker.robotsix.net"` | Agent-comm broker hostname. |
| `mill.broker_port` | `MILL_BROKER_PORT` | `443` | Broker TCP port. |
| `mill.broker_scheme` | `MILL_BROKER_SCHEME` | `"https"` | Transport scheme (`https` or `http`). |
| `mill.broker_token` | `MILL_BROKER_TOKEN` | `""` | This agent's bearer token, registered on the broker. Required when mill is enabled. |
| `mill.agent_id` | `MILL_AGENT_ID` | `"robotsix-chat"` | This agent's identity on the broker. |
| `mill.board_manager_id` | `MILL_BOARD_MANAGER_ID` | `"board-manager-robotsix-mill"` | Target board manager agent ID. |
| `mill.repo_id` | `MILL_REPO_ID` | `""` | Optional target repo to scope requests to. Empty = board manager picks the target repo from the conversation. |
| `mill.timeout` | `MILL_TIMEOUT` | `240.0` | Per-request timeout (seconds). Generous because the recipient is an LLM. |

## Mail

robotsix-auto-mail integration over the agent-comm broker. When enabled, the chat
agent gains a `consult_mail` tool that forwards natural-language requests to
the auto-mail board manager (`board-manager-robotsix-auto-mail`) and relays its
reply — so a user can view, triage, or comment on mail-agent tickets from chat.
Disabled by default; requires `uv sync --extra broker`.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `mail.enabled` | `MAIL_ENABLED` | `false` | Master switch. Requires the `broker` extra. |
| `mail.broker_host` | `MAIL_BROKER_HOST` | `ai-broker.robotsix.net` | Shared broker hostname. |
| `mail.broker_port` | `MAIL_BROKER_PORT` | `"443"` | Broker port. |
| `mail.broker_scheme` | `MAIL_BROKER_SCHEME` | `https` | `https` or `http`. |
| `mail.broker_token` | `MAIL_BROKER_TOKEN` | `""` | Bearer token (required when enabled). |
| `mail.agent_id` | `MAIL_AGENT_ID` | `robotsix-chat` | This agent's broker identity. |
| `mail.board_manager_id` | `MAIL_BOARD_MANAGER_ID` | `board-manager-robotsix-auto-mail` | Target board manager agent ID. |
| `mail.timeout` | `MAIL_TIMEOUT` | `240.0` | Per-request timeout (seconds). |

## Calendar (broker integration)

Calendar/tasks integration over the agent-comm broker. When enabled, the chat
agent gains tools that forward natural-language calendar and task requests to
`robotsix-calendar-agent` (query schedule, create/update events, manage to-dos)
and relay its reply. Disabled by default; requires `uv sync --extra broker`.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `calendar.enabled` | `CALENDAR_ENABLED` | `false` | Enable calendar broker integration (adds calendar/task tools). |
| `calendar.broker_host` | `CALENDAR_BROKER_HOST` | `"ai-broker.robotsix.net"` | Agent-comm broker hostname. |
| `calendar.broker_port` | `CALENDAR_BROKER_PORT` | `443` | Broker TCP port. |
| `calendar.broker_scheme` | `CALENDAR_BROKER_SCHEME` | `"https"` | Transport scheme (`https` or `http`). |
| `calendar.broker_token` | `CALENDAR_BROKER_TOKEN` | `""` | This agent's bearer token, registered on the broker. Required when calendar is enabled. |
| `calendar.agent_id` | `CALENDAR_AGENT_ID` | `"robotsix-chat"` | This agent's identity on the broker. |
| `calendar.calendar_agent_id` | `CALENDAR_CALENDAR_AGENT_ID` | `"calendar-agent-robotsix"` | Target calendar/tasks agent ID. |
| `calendar.timeout` | `CALENDAR_TIMEOUT` | `240.0` | Per-request timeout (seconds). Generous because the recipient is an LLM. |

## Component Agent (embedded responder)

When enabled, robotsix-chat registers itself on the agent-comm broker as a
discoverable component agent, serving `monitor`, `config-get`, and
`config-set` request kinds — so external callers can inspect live runtime
state and mutate configuration over the existing bearer-token channel.
Disabled by default; requires `uv sync --extra broker`.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `component_agent.enabled` | `COMPONENT_AGENT_ENABLED` | `false` | Master switch. Requires the `broker` extra. |
| `component_agent.broker_host` | `COMPONENT_AGENT_BROKER_HOST` | `"ai-broker.robotsix.net"` | Agent-comm broker hostname. |
| `component_agent.broker_port` | `COMPONENT_AGENT_BROKER_PORT` | `443` | Broker TCP port. |
| `component_agent.broker_scheme` | `COMPONENT_AGENT_BROKER_SCHEME` | `"https"` | Transport scheme (`https` or `http`). |
| `component_agent.broker_token` | `COMPONENT_AGENT_BROKER_TOKEN` | `""` | This agent's bearer token, registered on the broker. Required when enabled. |
| `component_agent.agent_id` | `COMPONENT_AGENT_AGENT_ID` | `"robotsix-chat-component"` | This agent's identity on the broker (the *responder's* broker id — distinct from client ids used by mill/calendar). |
| `component_agent.timeout` | `COMPONENT_AGENT_TIMEOUT` | `240.0` | Per-request timeout (seconds). |

## Reference Docs (refdocs)

Read-only reference-docs tool — lets the agent fetch documentation from
allowlisted GitHub repos on demand. Primarily used to consult the
board-workflow reference repo when deciding whether a ticket needs manual
human action. The tool is strictly read-only, fetches are on-demand (no bulk
ingestion), and only repos in the allowlist are reachable.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `refdocs.enabled` | `REFDOCS_ENABLED` | `false` | Enable the refdocs tool. Requires `refdocs.repos` to be non-empty. |
| `refdocs.repos` | `REFDOCS_REPOS` | `[]` | Allowlist of `owner/name` GitHub repos the agent may read. YAML: JSON array. Env: comma-separated list (e.g. `org/repo1,org/repo2`). Required when enabled. |
| `refdocs.ref` | `REFDOCS_REF` | `"main"` | Default git ref/branch to read from. |
| `refdocs.github_token` | `REFDOCS_GITHUB_TOKEN` | `""` | Optional PAT for private team repos. Public repos work without a token. |
| `refdocs.base_url` | `REFDOCS_BASE_URL` | `"https://api.github.com"` | Overridable base URL for GitHub Enterprise. |
| `refdocs.timeout` | `REFDOCS_TIMEOUT` | `30.0` | Per-request HTTP timeout (seconds). |

## Board Reader (HTTP board read access)

Direct HTTP access to the mill's board API — lets the assistant list, read, and
create tickets from the SAME HTTP endpoint the user's browser UI uses (read/write
parity with the user — no broker indirection, no NL reinterpretation). Disabled by
default; independent of the broker-based mill integration (works even when the
broker is offline).

Provides three tools: `list_board_tickets` (calls `GET /tickets`),
`read_board_ticket` (calls `GET /tickets/{id}`), and `create_board_ticket`
(calls `POST /tickets`).

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `board_reader.enabled` | `BOARD_READER_ENABLED` | `false` | Enable the board reader tools. |
| `board_reader.api_base_url` | `BOARD_READER_API_BASE_URL` | `"http://127.0.0.1:8077"` | Base URL of the board's HTTP API (no trailing slash). |
| `board_reader.api_token` | `BOARD_READER_API_TOKEN` | `""` | Optional bearer token for the board API (empty = no `Authorization` header). |
| `board_reader.timeout` | `BOARD_READER_TIMEOUT` | `30.0` | Per-request HTTP timeout (seconds). |

## Self-review

A read-only digest of live conversation activity from the in-process
:class:`~robotsix_chat.chat.conversation.ConversationStore`. When enabled, the
agent gains a ``read_recent_activity`` tool that returns a human-readable
multi-session summary of recent cross-client conversation turns. This is a
deliberate, explicit snapshot — complementary to, but independent of, the
optional cognee episodic memory subsystem (``src/robotsix_chat/memory/``).
Default-disabled so behaviour is unchanged unless explicitly turned on.

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `self_review.enabled` | `SELF_REVIEW_ENABLED` | `false` | Master switch — enables the ``read_recent_activity`` tool. |
| `self_review.recent_activity_limit` | `SELF_REVIEW_RECENT_ACTIVITY_LIMIT` | `20` | Maximum number of conversations returned by the tool. |

## Knowledge (writable agent knowledge base)

A local, writable, always-on knowledge base the agent uses to persist durable
operational notes and lessons across sessions. Five tools
(`add_knowledge_note`, `append_to_knowledge_note`, `update_knowledge_note`,
`list_knowledge_notes`, `read_knowledge_note`) let the agent deliberately
create, append to, update, list, and read back structured notes by a stable
`id`.  The store is a plain JSON file on disk — no embeddings, no external
service, no extra dependencies, and default ``enabled: true``.

**Boundary:** this is the agent's deliberate, explicit, self-authored
operational-note store.  It is distinct from both:

* the **human-governed system prompt** (`agent_instruction`) — stable
  behaviour rules the agent must not modify, and
* the **optional cognee episodic memory** (`memory/`) — which automatically
  recalls entire past conversations by fuzzy similarity, while this KB holds
  notes the agent explicitly writes and addresses by ``id``.

The two memory mechanisms are complementary and independent: cognee = "what
was said before, fuzzily recalled"; this KB = "operational notes/lessons I
deliberately authored and can revise by id."

| YAML path | Env var | Default | Description |
|---|---|---|---|
| `knowledge.enabled` | `KNOWLEDGE_ENABLED` | `true` | Enable the writable knowledge base (adds five knowledge-note tools). |
| `knowledge.path` | `KNOWLEDGE_PATH` | `".data/knowledge.json"` | Path to the JSON persistence file. |

## Example YAML

```yaml
# config/chat.local.yaml
llmio:
  model_level: 3

server:
  host: "0.0.0.0"
  port: 8080
  cors_allow_origins: ["https://chat.example.com"]
  idle_timeout_minutes: 60

auth:
  enabled: true
  username: "admin"
  password: ""  # set via AUTH_PASSWORD env var in production

memory:
  enabled: true
  data_dir: ".data/cognee"
  llm:
    api_key: "sk-or-..."
  embedding:
    endpoint: "http://host:11434/v1"

mill:
  enabled: true
  broker_token: "..."

calendar:
  enabled: true
  broker_token: "..."

component_agent:
  enabled: true
  broker_token: "..."

conversation:
  idle_reset_seconds: 3600
  max_history_turns: 30

refdocs:
  enabled: true
  repos: ["damien-robotsix/board-workflow"]

knowledge:
  enabled: true
  path: ".data/knowledge.json"
```
