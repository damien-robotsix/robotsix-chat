"""Layered configuration for robotsix-chat.

Settings resolve through a single, predictable cascade that matches the
rest of the robotsix stack (``robotsix-mill`` / ``robotsix-auto-mail``):

    pydantic field defaults  →  YAML config file  →  environment variables

with each later layer overriding the earlier one field-by-field. YAML
loading is delegated to the shared ``robotsix-yaml-config`` library so
every repo in the stack reads YAML the same way.

The LLM is selected through ``robotsix-llmio``'s consumer-facing
``provider-model`` tier identifier (``robotsix_llmio.config``): you pick a
capability level and llmio resolves the provider + model from its baked
defaults, never a concrete provider class.

The YAML file lives at ``config/chat.local.yaml`` by default (gitignored
so credentials never land in the repo); copy ``config/chat.local.example.yaml``
to create it. Override the path with the ``CHAT_CONFIG_PATH`` env var.

A ``.env`` file in the working directory is still loaded (via
``python-dotenv``) before environment variables are read, so existing
``.env``-based deployments keep working.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from robotsix_llmio.config import LEVEL1_DEFAULT, LEVEL2_DEFAULT, LEVEL3_DEFAULT
from robotsix_yaml_config import (
    YamlConfigError,
    flatten_config,
    read_yaml_file,
)

logger = logging.getLogger(__name__)

# Default YAML config file (gitignored; copy from the committed example).
DEFAULT_CONFIG_PATH = Path("config") / "chat.local.yaml"

# Env var that overrides the YAML config file path.
CONFIG_PATH_ENV = "CHAT_CONFIG_PATH"

# Case-insensitive truthy spellings for the AUTH_ENABLED env override.
_TRUE_VALUES = {"1", "true", "yes", "on"}

# robotsix-llmio now owns the level → provider-model mapping. The chat
# just picks a capability *level*; the combined provider-model identifier for
# that level comes from llmio's baked default TierLevelConfig (single source
# of truth):
#   level 1 → openrouter[deepseek]-deepseek/deepseek-v4-flash  (cheapest)
#   level 2 → openrouter[deepseek]-deepseek/deepseek-v4-pro
#   level 3 → claudeSDK-opus  (most capable; keyless)
_LEVEL_DEFAULTS = {1: LEVEL1_DEFAULT, 2: LEVEL2_DEFAULT, 3: LEVEL3_DEFAULT}
_VALID_MODEL_LEVELS = set(_LEVEL_DEFAULTS)

# Provider prefix for the keyless Claude SDK tier (auth via logged-in
# `claude` CLI — no API key needed).
_KEYLESS_PROVIDER = "claudeSDK"


def level_needs_api_key(level: int) -> bool:
    """Whether *level*'s default provider requires an ``api_key``.

    True for key-bearing providers (e.g. ``openrouter``), False for the
    keyless ``claudeSDK`` provider. Unknown levels are treated as needing a
    key (model_level is validated separately before this matters).
    """
    tlc = _LEVEL_DEFAULTS.get(level)
    return tlc is None or tlc.provider != _KEYLESS_PROVIDER


# Maps nested YAML ``section.field`` paths to ``Settings`` field names.
# The whole ``auth`` mapping is passed through as-is (a dict) so pydantic
# parses it into the nested :class:`AuthSettings` model.
_YAML_PATH_TO_FIELD: dict[str, str] = {
    "llmio.model_level": "llmio_model_level",
    "llmio.api_key": "llmio_api_key",  # pragma: allowlist secret
    "llmio.subagent_model": "subagent_model",
    "llmio.check_loop_model": "check_loop_model",
    "agent.instruction": "agent_instruction",
    "server.host": "server_host",
    "server.port": "server_port",
    "server.idle_timeout_minutes": "idle_timeout_minutes",
    "server.max_background_tasks": "max_background_tasks",
    "server.max_check_loops": "max_check_loops",
    "server.min_check_loop_interval_seconds": "min_check_loop_interval_seconds",
    "server.log_level": "log_level",
    "server.cors_allow_origins": "cors_allow_origins",
    "server.correlation_id_header": "correlation_id_header",
    "auth": "auth",
    "memory": "memory",
    "mill": "mill",
    "mail": "mail",
    "calendar": "calendar",
    "conversation": "conversation",
    "refdocs": "refdocs",
    "board_reader": "board_reader",
    "diagnostics": "diagnostics",
    "version_check": "version_check",
    "knowledge": "knowledge",
    "diagnostics": "diagnostics",
    "self_review": "self_review",
    "component_agent": "component_agent",
    "component_client": "component_client",
    "pending_questions": "pending_questions",
    "direct_repo": "direct_repo",
}


class ConfigError(YamlConfigError):
    """Raised for config-loading failures (missing file, malformed YAML).

    Subclasses the shared base so ``except YamlConfigError`` handlers in
    the stack keep working.
    """


def _parse_bool(value: str) -> bool:
    """Parse an env-var string into a bool (``"true"``/``"1"``/… → True)."""
    return value.strip().lower() in _TRUE_VALUES


class AuthSettings(BaseModel):
    """HTTP Basic Auth settings gating the browser UI and ``/chat``.

    Attributes:
        enabled: When ``True``, every request except ``GET /health`` must
            carry valid HTTP Basic credentials.
        username: The single accepted username.
        password: The single accepted password (required when *enabled*).

    """

    enabled: bool = False
    username: str = "admin"
    password: str = ""


class MemoryLlmSettings(BaseModel):
    """Extraction-LLM config for cognee memory (OpenRouter via litellm).

    Defaults match the validated robotsix setup: the cheap OpenRouter DeepSeek
    model through the ``custom`` provider. ``api_key`` is required when memory
    is enabled (provide it via ``MEMORY_LLM_API_KEY``).
    """

    provider: str = "custom"
    model: str = "openrouter/deepseek/deepseek-v4-flash"
    endpoint: str = "https://openrouter.ai/api/v1"
    api_key: str = ""


class MemoryEmbeddingSettings(BaseModel):
    """Embedding config for cognee memory (remote OpenAI-compatible server).

    Defaults target a self-hosted Ollama ``bge-m3`` endpoint. ``provider`` must
    be ``openai_compatible`` for that path (it tolerates a non-OpenAI model
    name); ``endpoint`` (e.g. ``http://host:11434/v1``) is required when memory
    is enabled. ``dimensions`` is sticky — changing it invalidates stored
    vectors.
    """

    provider: str = "openai_compatible"
    model: str = "bge-m3"
    endpoint: str = ""
    dimensions: int = 1024
    api_key: str = "ollama"
    huggingface_tokenizer: str = "BAAI/bge-m3"


class MemorySettings(BaseModel):
    """Long-term agent memory (cognee). Disabled by default.

    Attributes:
        enabled: When ``True``, the agent recalls before and persists after each
            reply. Requires the ``memory`` extra (cognee) installed.
        data_dir: Directory for cognee's stores (relative to the working dir).
            Put it under the persistent ``.data`` mount so memory survives
            container redeploys.
        recall_search_type: cognee ``SearchType`` name used for recall.
            ``GRAPH_COMPLETION`` (default) returns clean, relevant facts as text
            but costs one (cheap) LLM call per message; retrieval-only types
            like ``CHUNKS``/``SUMMARIES`` are faster but return raw, noisier
            payloads.
        llm: Extraction-LLM config (graph building / consolidation).
        embedding: Embedding-server config (semantic search).

    """

    enabled: bool = False
    data_dir: str = ".data/cognee"
    recall_search_type: str = "GRAPH_COMPLETION"
    llm: MemoryLlmSettings = Field(default_factory=MemoryLlmSettings)
    embedding: MemoryEmbeddingSettings = Field(default_factory=MemoryEmbeddingSettings)


class RefDocsSettings(BaseModel):
    """Read-only reference-docs tool for the agent.

    Lets the agent fetch documentation from allowlisted GitHub repos on
    demand. Primarily used to consult the board-workflow reference repo
    when deciding whether a ticket needs manual human action. The tool is
    strictly read-only, fetches are on-demand (no bulk ingestion), and only
    repos in the *repos* allowlist are reachable.

    Attributes:
        enabled: Master switch. When ``False``, no refdocs tools are offered.
        repos: Allowlist of ``owner/name`` GitHub repos the agent may read.
            The board-workflow reference repo goes here. The tool refuses
            any repo not in this list.
        ref: Default git ref/branch to read from (``"main"``).
        github_token: Optional PAT for private team repos; public repos work
            without a token.
        base_url: Overridable base URL for GitHub Enterprise.
        timeout: Per-request HTTP timeout in seconds.

    """

    enabled: bool = False
    repos: list[str] = Field(default_factory=list)
    ref: str = "main"
    github_token: str = ""
    base_url: str = "https://api.github.com"
    timeout: float = 30.0


class VersionCheckSettings(BaseModel):
    """Self-version-check tool: compare running version vs latest GitHub release.

    Disabled by default. When enabled, the agent gains a tool that reports the
    running ``robotsix_chat.__version__`` and the latest published release of
    the configured GitHub repo, and flags when the deployment is out of date.

    Attributes:
        enabled: Master switch. When ``False``, no version-check tool is offered.
        repo: GitHub ``owner/name`` (e.g. ``robotsix/robotsix-chat``). Required
            when *enabled*.
        github_token: Optional PAT to avoid unauthenticated rate limits.
        base_url: Overridable base URL for GitHub Enterprise.
        timeout: Per-request HTTP timeout in seconds.
        cache_ttl: Seconds to cache the latest-release lookup (monotonic clock).

    Note: the check is only meaningful when releases bump
    ``robotsix_chat.__version__`` in lockstep with the GitHub release tag.

    """

    enabled: bool = False
    repo: str = ""
    github_token: str = ""
    base_url: str = "https://api.github.com"
    timeout: float = 30.0
    cache_ttl: float = 300.0


class MillSettings(BaseModel):
    """robotsix-mill integration over the agent-comm broker. Disabled by default.

    When enabled, the chat agent gains a tool that forwards natural-language
    requests to the mill's board manager (``board-manager-robotsix-mill``) over
    the broker and relays its reply — so a user can have the mill track/do
    development work from chat. Mirrors the cost-analyst → board pattern.

    Attributes:
        enabled: Master switch. Requires the ``broker`` extra (robotsix-agent-comm).
        broker_host: Broker hostname (the shared agent-comm broker).
        broker_port: Broker port (443 for the public TLS endpoint).
        broker_scheme: ``https`` (TLS) or ``http``.
        broker_token: This agent's bearer token, registered on the broker.
            Required when enabled.
        agent_id: This agent's id on the broker.
        board_manager_id: Recipient agent id — the mill's NL board manager.
        repo_id: Optional repo to scope requests to; empty lets the board manager
            choose the target repo from the conversation.
        timeout: Per-request timeout (seconds). The board manager is a
            multi-turn LLM agent that legitimately takes tens of seconds — and
            longer when its replies queue behind other mill work — so this is
            deliberately generous. A fast pre-flight reachability check (see
            ``BaseBrokeredClient``) fails in seconds when the broker/recipient
            is actually unreachable, so this long timeout only governs a
            reachable-but-slow board manager.

    """

    enabled: bool = False
    broker_host: str = "ai-broker.robotsix.net"
    broker_port: int = 443
    broker_scheme: str = "https"
    broker_token: str = ""
    agent_id: str = "robotsix-chat"
    board_manager_id: str = "board-manager-robotsix-mill"
    repo_id: str = ""
    timeout: float = 600.0  # 10 min — synthesis legitimately exceeds 5 min


class MailSettings(BaseModel):
    """robotsix-auto-mail integration over the agent-comm broker. Disabled by default.

    When enabled, the chat agent gains a tool that forwards natural-language
    requests to the auto-mail board manager
    (``board-manager-robotsix-auto-mail``) over the broker and relays its
    reply — so a user can view, triage, or comment on mail-agent tickets
    from chat. Mirrors the mill / ``consult_mill`` pattern exactly.

    Attributes:
        enabled: Master switch. Requires the ``broker`` extra (robotsix-agent-comm).
        broker_host: Broker hostname (the shared agent-comm broker).
        broker_port: Broker port (443 for the public TLS endpoint).
        broker_scheme: ``https`` (TLS) or ``http``.
        broker_token: This agent's bearer token, registered on the broker.
            Required when enabled.
        agent_id: This agent's id on the broker.
        board_manager_id: Recipient agent id — the mail board manager.
        timeout: Per-request timeout (seconds); generous, the recipient is an LLM.

    """

    enabled: bool = False
    broker_host: str = "ai-broker.robotsix.net"
    broker_port: int = 443
    broker_scheme: str = "https"
    broker_token: str = ""
    agent_id: str = "robotsix-chat"
    board_manager_id: str = "board-manager-robotsix-auto-mail"
    timeout: float = 240.0


class BoardReaderSettings(BaseModel):
    """Direct HTTP access to the mill's board API (same endpoint the UI uses).

    Lets the assistant list, read, and create tickets from the same HTTP
    endpoint the user's browser UI consumes, giving read/write parity with
    the user — no broker indirection, no NL reinterpretation.

    The board API is served by the mill's management-plane FastAPI app
    (typically on the same host at port 8077).  When *api_token* is set, it
    is sent as a ``Bearer`` token; on localhost deployments auth is often
    disabled (empty token = no ``Authorization`` header sent).

    Attributes:
        enabled: Master switch.  Independent of ``mill.enabled`` — the
            board reader works over HTTP even when the broker is offline.
        api_base_url: Base URL of the board HTTP API (no trailing slash).
        api_token: Optional bearer token; empty means no auth header.
        timeout: Per-request HTTP timeout in seconds.
        cache_ttl: Seconds to cache board list and ticket lookups
            (monotonic clock). Failed fetches are never cached.

    """

    enabled: bool = False
    api_base_url: str = "http://127.0.0.1:8077"
    api_token: str = ""
    timeout: float = 30.0
    cache_ttl: float = 60.0


class DiagnosticsSettings(BaseModel):
    """Blocked-ticket diagnostics capture and inspection.

    When enabled, the agent gains a ``list_diagnostic_records`` tool to
    inspect captured diagnostic bundles.  A background poller (started by
    the server) detects BLOCKED state transitions and records diagnostic
    snapshots.

    Attributes:
        enabled: Master switch.  Default ``False`` — diagnostics capture
            is opt-in.
        poll_interval: Seconds between board polls for BLOCKED transitions.
        data_dir: Path to the JSON persistence file.  Default
            ``.data/diagnostics.json``.

    """

    enabled: bool = False
    poll_interval: float = 30.0
    data_dir: str = ".data/diagnostics.json"


class DirectRepoSettings(BaseModel):
    """Direct-repo push-branch + open-PR capability as the robotsix-mill GitHub App.

    When enabled, the chat agent gains two tools: ``push_direct_repo_branch``
    (create/push a branch with file changes) and ``open_direct_repo_pr``
    (open a PR from a branch).  Both authenticate as the configured GitHub App
    installation (JWT → short-lived installation token) and dynamically resolve
    the allowed repo set from the installation at action time — no static
    allowlist.

    **Guardrails built into the tools (not configurable):**
    - Actions are ONLY permitted for tickets in BLOCKED state.
    - Repo scope is resolved dynamically from the GitHub App installation.
    - PRs are opened in a reviewable state with no auto-merge.
    - No merge capability exists on this path.

    Attributes:
        enabled: Master switch.  When ``False``, no direct-repo tools are
            offered.
        github_app_id: The GitHub App's numeric or slug id.  Required when
            *enabled*.
        github_app_private_key: The app's RSA private key in PEM format.
            Required when *enabled*.  Stored in config only — never
            hardcoded.
        github_app_installation_id: The installation id to act as.  The
            app must be installed on the target org/account.  Required when
            *enabled*.
        github_api_base_url: Overridable base URL for GitHub Enterprise.
        board_api_base_url: Base URL of the board HTTP API for ticket-state
            lookups (verifying BLOCKED state).
        board_api_token: Optional bearer token for the board API.
        timeout: Per-request HTTP timeout in seconds.

    """

    enabled: bool = False
    github_app_id: str = ""
    github_app_private_key: str = ""
    github_app_installation_id: str = ""
    github_api_base_url: str = "https://api.github.com"
    board_api_base_url: str = "http://127.0.0.1:8077"
    board_api_token: str = ""
    timeout: float = 30.0


class KnowledgeSettings(BaseModel):
    """Local, writable knowledge base for agent-authored operational notes.

    A deliberate, explicit, agent-curated store of durable lessons and findings
    — plain local JSON, no embeddings, no external service, always-on.  The
    agent writes notes via five tools (``add/append/update/list/read_knowledge_note``)
    and can re-read and revise them by id across sessions.

    This store is **complementary to**, not a duplicate of, the optional cognee
    episodic memory system (``memory/``).  cognee automatically recalls past
    conversations by similarity; this knowledge base holds notes the agent
    deliberately authors and addresses by id.

    Attributes:
        enabled: Master switch.  Default ``True`` — this is a purely local,
            no-credential, no-external-dependency primitive.
        path: Path to the JSON persistence file.  Default
            ``.data/knowledge.json``.

    """

    enabled: bool = True
    path: str = ".data/knowledge.json"


class DiagnosticsSettings(BaseModel):
    """Diagnostics capture and systemic fix surfacing.

    When enabled, the agent captures diagnostic bundles for failure events
    and can detect recurring failure categories.  When a category crosses
    the recurrence threshold a ``FixProposal`` is auto-generated (but NOT
    auto-applied) for agent or human review.

    Attributes:
        enabled: Master switch.  Default ``True``.
        store_path: Path to the diagnostic-event JSON persistence file.
            Default ``.data/diagnostics.json``.
        proposals_path: Path to the fix-proposal JSON persistence file.
            Default ``.data/fix_proposals.json``.
        recurrence_threshold: Minimum number of occurrences within the
            window to trigger a recurrence alert.  Default ``3``.
        recurrence_window_days: Look-back window in days for recurrence
            detection.  Default ``30``.

    """

    enabled: bool = True
    store_path: str = ".data/diagnostics.json"
    proposals_path: str = ".data/fix_proposals.json"
    recurrence_threshold: int = 3
    recurrence_window_days: int = 30


class SelfReviewSettings(BaseModel):
    """Self-review tool — a read-only digest of live conversation activity.

    When enabled, the agent gains a ``read_recent_activity`` tool that
    reads the in-process :class:`~robotsix_chat.chat.conversation.ConversationStore`
    (short-lived per-client conversation turns) and returns a human-readable
    multi-session digest.  This is a deliberate, explicit, cross-client
    snapshot — complementary to, but independent of, the optional cognee
    episodic memory subsystem (``src/robotsix_chat/memory/``).

    Default-disabled so behaviour is unchanged unless explicitly turned on.

    Attributes:
        enabled: Master switch. When ``True``, the ``read_recent_activity``
            tool is attached to the agent.
        recent_activity_limit: Maximum number of conversations returned by
            the tool (clamps the caller's ``limit`` argument).

    """

    enabled: bool = False
    recent_activity_limit: int = 20


class CalendarSettings(BaseModel):
    """Calendar/tasks integration over the agent-comm broker. Disabled by default.

    When enabled, the chat agent gains tools that forward natural-language
    calendar and task requests to ``robotsix-calendar-agent`` over the broker
    and relay its reply — so a user can query their schedule, create/update
    events, and manage to-dos from chat. Mirrors the mill→board pattern.

    Both calendar and task requests route to the same recipient
    (``calendar_agent_id``) under the assumption that a single calendar agent
    handles CalDAV events (``VEVENT``) and to-dos (``VTODO``). If a separate
    tasks recipient is needed later, add a ``tasks_agent_id`` field and pass it
    from the task tools.

    Attributes:
        enabled: Master switch. Requires the ``broker`` extra (robotsix-agent-comm).
        broker_host: Broker hostname (the shared agent-comm broker).
        broker_port: Broker port (443 for the public TLS endpoint).
        broker_scheme: ``https`` (TLS) or ``http``.
        broker_token: This agent's bearer token, registered on the broker.
            Required when enabled.
        agent_id: This agent's id on the broker.
        calendar_agent_id: Recipient agent id — the calendar/tasks agent.
        timeout: Per-request timeout (seconds); generous, the recipient is an LLM.
        cache_ttl: How long to cache query results (seconds).  Query calls
            (``query_calendar``, ``query_tasks``) within this window return
            the cached result without a broker round-trip.  Manage calls
            (``manage_calendar``, ``manage_tasks``) invalidate the cache
            for their domain.

    """

    enabled: bool = False
    broker_host: str = "ai-broker.robotsix.net"
    broker_port: int = 443
    broker_scheme: str = "https"
    broker_token: str = ""
    agent_id: str = "robotsix-chat"
    calendar_agent_id: str = "robotsix-calendar"
    timeout: float = 240.0
    cache_ttl: float = 60.0


class ComponentAgentSettings(BaseModel):
    """Component agent responder settings. Disabled by default.

    When enabled, robotsix-chat registers itself on the agent-comm broker
    as a discoverable component agent, serving ``monitor``, ``config-get``,
    and ``config-set`` request kinds so external callers can inspect live
    runtime state and mutate configuration over the existing bearer-token
    channel — no new side channel.

    Attributes:
        enabled: Master switch. Requires the ``broker`` extra (robotsix-agent-comm).
        broker_host: Broker hostname (the shared agent-comm broker).
        broker_port: Broker port (443 for the public TLS endpoint).
        broker_scheme: ``https`` (TLS) or ``http``.
        broker_token: This agent's bearer token, registered on the broker.
            Required when enabled.
        agent_id: This agent's id on the broker (the responder's identity).
            Default ``robotsix-chat-component`` — distinct from the client ids
            used by mill/calendar.
        timeout: Per-request timeout (seconds).

    """

    enabled: bool = False
    broker_host: str = "ai-broker.robotsix.net"
    broker_port: int = 443
    broker_scheme: str = "https"
    broker_token: str = ""
    agent_id: str = "robotsix-chat-component"
    timeout: float = 240.0


class ComponentTarget(BaseModel):
    """A single component agent that the chat may inspect or configure.

    Attributes:
        agent_id: Broker agent id of the target component.
        label: Optional human-readable label shown in discovery output.

    """

    agent_id: str
    label: str = ""


class ComponentClientSettings(BaseModel):
    """Component agent client settings — inspect and configure remote agents.

    When enabled, the chat agent gains four tools: ``list_component_agents``,
    ``get_component_telemetry``, ``get_component_config``, and
    ``set_component_config`` so it can enumerate configured component agents,
    read live telemetry, and read/update configuration on demand.

    Attributes:
        enabled: Master switch. Requires the ``broker`` extra (robotsix-agent-comm).
        broker_host: Broker hostname (the shared agent-comm broker).
        broker_port: Broker port (443 for the public TLS endpoint).
        broker_scheme: ``https`` (TLS) or ``http``.
        broker_token: This agent's bearer token, registered on the broker.
            Required when enabled.
        agent_id: This agent's id on the broker (the requester identity).
            Default ``robotsix-chat``.
        timeout: Per-request timeout (seconds).
        components: Allowlist of component agents the chat may contact.
            Each entry has an ``agent_id`` and an optional ``label``.

    """

    enabled: bool = False
    broker_host: str = "ai-broker.robotsix.net"
    broker_port: int = 443
    broker_scheme: str = "https"
    broker_token: str = ""
    agent_id: str = "robotsix-chat"
    timeout: float = 240.0
    components: list[ComponentTarget] = Field(default_factory=list)


class PendingQuestionsSettings(BaseModel):
    """Pending-questions panel and agent tool for awaiting-user prompts.

    When enabled (default), the agent can raise structured questions the user
    needs to answer — they appear in a panel above the chat input, update in
    real time, and the user's inline answer is fed back into the conversation.

    Attributes:
        enabled: Master switch.  Default ``True`` — this is a core UI/agent
            primitive with no external dependencies.

    """

    enabled: bool = True


class ConversationSettings(BaseModel):
    """Multi-session conversation continuity for the browser chat.

    The server groups conversations by a per-browser ``owner_id`` and addresses
    individual sessions by ``session_id``. Each owner can have multiple named
    sessions with independent turn histories. History is **never** wiped on
    idle — sessions are persistent when ``persist_path`` is configured.

    Attributes:
        idle_reset_seconds: Retained for compatibility; no longer triggers
            destructive history reset (sessions are explicit and persistent).
        max_history_turns: Most recent user/assistant turns kept per
            session and replayed to the agent (bounds prompt size).
        max_conversations: Maximum number of distinct sessions tracked at once
            (LRU-evicted); bounds the in-memory store.
        persist_path: Path to the JSON persistence file. Default
            ``.data/conversations.json``. Set to an empty string to disable.

    """

    idle_reset_seconds: int = 1800
    max_history_turns: int = 50
    max_conversations: int = 1000
    persist_path: str = ".data/conversations.json"


# Version stamp for the agent_instruction default literal.
# Bump on every change to Settings.agent_instruction and update
# docs/system_prompt_changelog.md with a new entry + SHA256.
SYSTEM_PROMPT_VERSION = 14


class Settings(BaseModel):
    """Application settings, resolved from defaults → YAML → environment.

    The LLM is configured the robotsix-llmio way — pick a capability
    ``model_level`` and llmio resolves the provider + model for that level
    (from its baked default :class:`~robotsix_llmio.config.TierLevelConfig`).

    Attributes:
        llmio_model_level: Capability level — ``1`` (cheapest/fastest), ``2``,
            or ``3`` (most capable). The level encodes the provider + model:
            by default levels 1-2 use ``openrouter`` and level 3 uses
            ``claudeSDK``/``opus``.
        llmio_api_key: Provider API key, forwarded to llmio when the chosen
            level's provider needs one (e.g. ``openrouter``); unused
            by keyless providers like ``claudeSDK``.
        subagent_model: Bare Claude model name override for background
            sub-agents spawned via ``delegate_task``.  Only applied when the
            foreground level uses the keyless ``claudeSDK`` provider
            (level 3).  ``"sonnet"`` (default) is cheaper than Opus and
            stays on the subscription; set ``"haiku"`` for cheaper
            delegation, or ``null`` to disable the downgrade (delegation
            tasks then match the foreground model).  Ignored for levels 1–2
            (OpenRouter).  Env override: ``LLMIO_SUBAGENT_MODEL``.

            Check-loop workers use :attr:`check_loop_model` instead; the two
            pools have independent model overrides.
        check_loop_model: Bare Claude model name override for check-loop
            workers (recurring monitoring / status-check ticks).  Only
            applied when the foreground level uses the keyless ``claudeSDK``
            provider (level 3).  ``"haiku"`` (default) is the cheapest
            subscription tier — ideal for binary "is it done yet?" polling.
            Set ``"sonnet"`` for more nuanced monitoring, or ``null`` to
            disable the downgrade (check-loop ticks then match the foreground
            model).  Ignored for levels 1–2 (OpenRouter).
            Env override: ``LLMIO_CHECK_LOOP_MODEL``.
        agent_instruction: System instruction handed to the LLM agent.
            Includes delegate-vs-inline guidance for background tasks.
        server_host: Host address the chat SSE server binds to.
        server_port: Port the chat SSE server listens on.
        idle_timeout_minutes: Minutes of no user activity before the UI
            auto-restarts the conversation; ``0`` disables the feature.
            Env override: ``IDLE_TIMEOUT_MINUTES``.
        max_background_tasks: Maximum number of concurrently-running background
            sub-agent tasks per process.  Env override: ``MAX_BACKGROUND_TASKS``.
        max_check_loops: Maximum number of concurrent check loops per process.
            Env override: ``MAX_CHECK_LOOPS``.
        min_check_loop_interval_seconds: Minimum allowed interval between
            check-loop iterations, in seconds. Env override:
            ``MIN_CHECK_LOOP_INTERVAL_SECONDS``.
        log_level: Python logging level name.
        cors_allow_origins: Origins allowed to call /chat cross-origin
            (empty = none; ``["*"]`` = any). Only needed when the browser
            UI is hosted on a different origin than the server.
        correlation_id_header: HTTP header name used for the correlation /
            request-id (both inbound and outbound). Default ``X-Request-ID``.
        auth: HTTP Basic Auth settings gating the UI and ``/chat``.
        max_images_per_message: Maximum number of images a client may attach to
            a single ``POST /chat`` request.  Default ``8``.
            Env override: ``MAX_IMAGES_PER_MESSAGE``.
        max_image_bytes: Maximum decoded size (bytes) of a single attached
            image.  Default ``5_242_880`` (5 MiB).  Env override:
            ``MAX_IMAGE_BYTES``.
        allowed_image_media_types: Media types accepted for image attachments.
            Default ``["image/png", "image/jpeg", "image/gif", "image/webp"]``.
            Env override: ``ALLOWED_IMAGE_MEDIA_TYPES`` (comma-separated).

    """

    llmio_model_level: int = 3
    llmio_api_key: str = ""
    subagent_model: str | None = "sonnet"
    check_loop_model: str | None = "haiku"
    agent_instruction: str = (
        "You are a helpful assistant. "
        "You have a local, durable knowledge base "
        "(add/append/update/list/read_knowledge_note) "
        "for operational notes and lessons you deliberately author — "
        "consult it at the start of every session and write durable "
        "findings to it. Unlike the stable, human-governed system "
        "prompt (which you must not modify), these notes are yours to "
        "author and revise by id. This store is distinct from the "
        "automatic cognee conversation memory — cognee recalls past "
        "exchanges by similarity, while these notes you explicitly "
        "create and address by id. "
        "Answer quick questions inline. "
        "When a request is judged to take a while — multi-step research, "
        "long generation, or anything that would stall your reply — call "
        "the delegate_task tool to offload it to a background sub-agent. "
        "The tool returns a task id immediately; tell the user the work "
        "is running in the background and they'll be notified when it "
        "finishes."
        "\n\n"
        "Board/mill rules:\n"
        "– To READ board/ticket state, always use list_board_tickets or "
        "read_board_ticket — these call the SAME HTTP endpoint the user's "
        "browser UI consumes, so you see exactly what the user sees.  "
        "Never narrate or fabricate ticket states; always verify with "
        "the board reader tools first.\n"
        "– For complex WRITE operations (migrate tickets between repos, "
        "transition ticket state, triage that requires board-manager "
        "intelligence), use consult_mill — the broker-based board manager "
        "handles these. For simple ticket creation, use create_board_ticket "
        "— it is faster, uses fewer tokens, and includes built-in duplicate "
        "detection.\n"
        "– Do all board work inline — never offload board actions through "
        "delegate_task. Delegate-task results are never returned, so a "
        "ticket filed that way may silently fail with no feedback. "
        "This is now enforced — delegate_task will refuse board/ticket "
        "work and direct you to consult_mill.\n"
        "– Before filing ANY new ticket, list_board_tickets for the target "
        "repo and check whether an existing OPEN ticket already covers the "
        "same intent; comment on / reuse it instead of filing a duplicate. "
        "create_board_ticket does this for you automatically and will warn "
        "if a similar ticket exists — act on that warning.\n"
        "– After creating a ticket, verify it landed on "
        "the correct board with list_board_tickets. The board manager "
        "(consult_mill) may route tickets to robotsix-mill by default; "
        "if misplaced, request a migration to the correct board "
        "(e.g. robotsix-chat) — also inline via consult_mill.\n"
        "– Never offer to manually promote a ticket from draft to ready. "
        "The draft→ready transition is automatic (auto-pickup); the system "
        "picks up tickets on its own once they leave draft.\n"
        "– When launching a check loop (start_check_loop) that monitors "
        "mill/board/thread/ticket status, set verify_via_board=True. "
        "Never assert board/thread status without a fresh consult_mill read "
        "— fabricating or narrating status without reading the board is "
        "prohibited.\n"
        "\n"
        "Calendar/task tools:\n"
        "– query_calendar, manage_calendar, query_tasks, and manage_tasks "
        "are available for calendar and task management through the "
        "configured calendar agent. They may be disabled in deployments "
        "that lack a calendar integration. Never propose building a new "
        "calendar integration — if these tools are unavailable, briefly "
        "note it rather than suggesting alternatives.\n"
        "\n"
        "Autonomy:\n"
        "– Proactively perform actions that are clearly safe and reversible "
        "without waiting for explicit human validation — do not ask for "
        "permission when the action is low-risk and can be easily undone. "
        "Examples: approving low-risk documentation/prompt changes, resuming "
        "held work after a known blocker has been resolved, or stopping a "
        "check loop that has reached a verified terminal state.\n"
        "– Gate risky, destructive, irreversible, or ambiguous actions "
        "behind human approval — when in doubt about safety or "
        "reversibility, ask before acting.\n"
        "– When running inside a check loop (start_check_loop) and you "
        "confirm the monitored condition has reached a verified terminal/"
        "completion state (e.g. a watched ticket is closed/done), call "
        "stop_check_loop to self-stop the loop immediately. Do NOT emit "
        "repeated COMPLETED/NO_CHANGE reports — the check loop's job is "
        "done once the terminal state is verified.\n"
        "\n\n"
        "Efficiency:\n"
        "– If a required tool is missing, state it in one sentence and stop — "
        "do not explore alternatives, explain why, or narrate checking for it.\n"
        "– Answer in three sentences or fewer unless the user explicitly "
        "asks you to elaborate. Do NOT volunteer multi-row markdown tables, "
        "timeline/audit dumps, or recap lists — emit those formats ONLY when "
        "the user explicitly requests them (e.g. 'show me a table', 'give me "
        "the full audit'). Never repeat content already shown earlier in the "
        "same conversation.\n"
        "– All tools are already loaded and available for the entire "
        "session; there is no separate tool-loading step. Never narrate "
        "loading, preparing, or fetching tools (e.g. 'I'll load the "
        "tools…', 'Let me load the task management tool first') and never "
        "announce or run a 'capability check'. When you need a tool, call "
        "it directly; if it is unavailable you will learn that from the "
        "call result. Do not restate tool descriptions across turns."
        "\n\nYou are a conversational assistant with no ability to run shell "
        "commands, read or edit files, browse the web, or otherwise access the host "
        "system or its network. You can only converse and use the tools explicitly "
        "provided to you in this session. If a request needs access you don't have, "
        "briefly say so and suggest an alternative; never narrate or pretend to "
        "perform such actions."
    )
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    idle_timeout_minutes: int = 30
    max_background_tasks: int = 5
    max_check_loops: int = 5
    min_check_loop_interval_seconds: float = 60.0
    log_level: str = "INFO"
    cors_allow_origins: list[str] = Field(default_factory=list)
    correlation_id_header: str = "X-Request-ID"
    auth: AuthSettings = Field(default_factory=AuthSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    mill: MillSettings = Field(default_factory=MillSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    calendar: CalendarSettings = Field(default_factory=CalendarSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    diagnostics: DiagnosticsSettings = Field(default_factory=DiagnosticsSettings)
    refdocs: RefDocsSettings = Field(default_factory=RefDocsSettings)
    board_reader: BoardReaderSettings = Field(default_factory=BoardReaderSettings)
    diagnostics: DiagnosticsSettings = Field(default_factory=DiagnosticsSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    self_review: SelfReviewSettings = Field(default_factory=SelfReviewSettings)
    component_agent: ComponentAgentSettings = Field(
        default_factory=ComponentAgentSettings
    )
    version_check: VersionCheckSettings = Field(default_factory=VersionCheckSettings)
    component_client: ComponentClientSettings = Field(
        default_factory=ComponentClientSettings
    )
    pending_questions: PendingQuestionsSettings = Field(
        default_factory=PendingQuestionsSettings
    )
    direct_repo: DirectRepoSettings = Field(default_factory=DirectRepoSettings)
    max_images_per_message: int = 8
    max_image_bytes: int = 5_242_880
    allowed_image_media_types: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"]
    )

    def model_post_init(self, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations."""
        if self.llmio_model_level not in _VALID_MODEL_LEVELS:
            raise ValueError(
                f"llmio.model_level must be one of {sorted(_VALID_MODEL_LEVELS)}, "
                f"got {self.llmio_model_level!r}"
            )
        # The keyless Claude SDK provider (level 3) needs no API key;
        # key-bearing providers (e.g. openrouter, levels 1-2) require one.
        if level_needs_api_key(self.llmio_model_level) and not self.llmio_api_key:
            raise ValueError(
                f"llmio.api_key must be set for model_level "
                f"{self.llmio_model_level} (its provider needs a key) — provide "
                "it via LLMIO_API_KEY, a .env file, or the `llmio.api_key` field "
                "of your config file (or use model_level 3, which is keyless)"
            )
        if self.auth.enabled and not self.auth.password:
            raise ValueError(
                "auth.password must be set when auth is enabled — provide it "
                "via AUTH_PASSWORD or the `auth.password` field of your config file"
            )
        if self.memory.enabled:
            if not self.memory.llm.api_key:
                raise ValueError(
                    "memory.llm.api_key must be set when memory is enabled — "
                    "provide it via MEMORY_LLM_API_KEY or the `memory.llm.api_key` "
                    "field of your config file"
                )
            if not self.memory.embedding.endpoint:
                raise ValueError(
                    "memory.embedding.endpoint must be set when memory is enabled "
                    "(e.g. http://host:11434/v1) — provide it via "
                    "MEMORY_EMBEDDING_ENDPOINT or the config file"
                )
        if self.idle_timeout_minutes < 0:
            raise ValueError(
                f"idle_timeout_minutes must be >= 0, got {self.idle_timeout_minutes!r}"
            )
        if self.max_background_tasks < 1:
            raise ValueError(
                f"max_background_tasks must be >= 1, got {self.max_background_tasks!r}"
            )
        if self.max_check_loops < 1:
            raise ValueError(
                f"max_check_loops must be >= 1, got {self.max_check_loops!r}"
            )
        if self.min_check_loop_interval_seconds < 1.0:
            raise ValueError(
                f"min_check_loop_interval_seconds must be >= 1.0, "
                f"got {self.min_check_loop_interval_seconds!r}"
            )
        if self.mill.enabled:
            if not self.mill.broker_token:
                raise ValueError(
                    "mill.broker_token must be set when mill is enabled — provide "
                    "it via MILL_BROKER_TOKEN or the `mill.broker_token` config field"
                )
            if not self.mill.broker_host:
                raise ValueError(
                    "mill.broker_host must be set when mill is enabled — provide it "
                    "via MILL_BROKER_HOST or the config file"
                )
        if self.mail.enabled:
            if not self.mail.broker_token:
                raise ValueError(
                    "mail.broker_token must be set when mail is enabled — provide "
                    "it via MAIL_BROKER_TOKEN or the `mail.broker_token` config field"
                )
            if not self.mail.broker_host:
                raise ValueError(
                    "mail.broker_host must be set when mail is enabled — provide it "
                    "via MAIL_BROKER_HOST or the config file"
                )
        if self.calendar.enabled:
            if not self.calendar.broker_token:
                raise ValueError(
                    "calendar.broker_token must be set when calendar is enabled — "
                    "provide it via CALENDAR_BROKER_TOKEN or the "
                    "`calendar.broker_token` config field"
                )
            if not self.calendar.broker_host:
                raise ValueError(
                    "calendar.broker_host must be set when calendar is enabled — "
                    "provide it via CALENDAR_BROKER_HOST or the config file"
                )
        if self.component_agent.enabled:
            if not self.component_agent.broker_token:
                raise ValueError(
                    "component_agent.broker_token must be set when "
                    "component_agent is enabled — provide it via "
                    "COMPONENT_AGENT_BROKER_TOKEN or the "
                    "`component_agent.broker_token` config field"
                )
            if not self.component_agent.broker_host:
                raise ValueError(
                    "component_agent.broker_host must be set when "
                    "component_agent is enabled — provide it via "
                    "COMPONENT_AGENT_BROKER_HOST or the config file"
                )
        if self.component_client.enabled:
            if not self.component_client.broker_token:
                raise ValueError(
                    "component_client.broker_token must be set when "
                    "component_client is enabled — provide it via "
                    "COMPONENT_CLIENT_BROKER_TOKEN or the "
                    "`component_client.broker_token` config field"
                )
            if not self.component_client.broker_host:
                raise ValueError(
                    "component_client.broker_host must be set when "
                    "component_client is enabled — provide it via "
                    "COMPONENT_CLIENT_BROKER_HOST or the config file"
                )
        if self.refdocs.enabled and not self.refdocs.repos:
            raise ValueError(
                "refdocs.repos must be non-empty when refdocs is enabled — "
                "provide it via REFDOCS_REPOS or the `refdocs.repos` config field"
            )
        if self.version_check.enabled and not self.version_check.repo:
            raise ValueError(
                "version_check.repo is required when version_check.enabled is true — "
                "provide it via VERSION_CHECK_REPO or the "
                "`version_check.repo` config field"
            )

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Settings:
        """Load settings through the full cascade (YAML file + environment).

        Resolution order (highest priority last):

        1. pydantic field defaults
        2. the YAML config file — explicit *config_path* arg, else the
           ``CHAT_CONFIG_PATH`` env var, else ``config/chat.local.yaml`` if
           present (otherwise no file)
        3. environment variables (with ``.env`` support)

        Pass ``config_path=""`` to skip the YAML file entirely (used by the
        test suite).
        """
        _load_dotenv()
        nested = cls._read_yaml(config_path)
        flat = flatten_config(nested, _YAML_PATH_TO_FIELD)
        return cls._build(flat)

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from environment variables only (no YAML file).

        Calls ``load_dotenv()`` first so a ``.env`` file in the working
        directory (or any parent) is picked up automatically. Equivalent
        to ``Settings.load(config_path="")``.
        """
        _load_dotenv()
        return cls._build({})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @classmethod
    def _read_yaml(cls, config_path: str | Path | None) -> dict[str, Any]:
        """Resolve the YAML config path and read it into a nested dict.

        Returns an empty dict when no file applies. Raises
        :class:`FileNotFoundError` when an explicit path (arg or
        ``CHAT_CONFIG_PATH``) points at a missing file, and
        :class:`ConfigError` on malformed YAML.
        """
        explicit = True
        if config_path is not None:
            if config_path == "":
                return {}
            path = Path(config_path)
        else:
            env_path = os.getenv(CONFIG_PATH_ENV)
            if env_path:
                path = Path(env_path)
            elif DEFAULT_CONFIG_PATH.exists():
                path, explicit = DEFAULT_CONFIG_PATH, False
            else:
                return {}

        if explicit and not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            return read_yaml_file(path)
        except YamlConfigError as exc:
            raise ConfigError(str(exc)) from exc

    @classmethod
    def _build(cls, flat: dict[str, Any]) -> Settings:
        """Overlay environment variables onto *flat* YAML values and build."""
        raw: dict[str, Any] = {
            k: v
            for k, v in flat.items()
            if k
            not in (
                "auth",
                "memory",
                "mill",
                "mail",
                "calendar",
                "conversation",
                "refdocs",
                "knowledge",
                "self_review",
                "version_check",
                "component_agent",
                "component_client",
                "diagnostics",
            )
        }
        auth_raw: dict[str, Any] = dict(flat.get("auth") or {})

        def env_override(field: str, env_name: str) -> None:
            value = os.getenv(env_name)
            if value is not None:
                raw[field] = value

        env_override("llmio_api_key", "LLMIO_API_KEY")
        subagent_val = os.getenv("LLMIO_SUBAGENT_MODEL")
        if subagent_val is not None:
            raw["subagent_model"] = subagent_val or None
        check_loop_val = os.getenv("LLMIO_CHECK_LOOP_MODEL")
        if check_loop_val is not None:
            raw["check_loop_model"] = check_loop_val or None
        env_override("agent_instruction", "AGENT_INSTRUCTION")
        env_override("server_host", "SERVER_HOST")
        env_override("log_level", "LOG_LEVEL")

        level_str = os.getenv("LLMIO_MODEL_LEVEL")
        if level_str is not None:
            try:
                raw["llmio_model_level"] = int(level_str)
            except ValueError:
                raise ValueError(
                    f"LLMIO_MODEL_LEVEL must be an integer, got {level_str!r}"
                ) from None

        port_str = os.getenv("SERVER_PORT")
        if port_str is not None:
            try:
                raw["server_port"] = int(port_str)
            except ValueError:
                raise ValueError(
                    f"SERVER_PORT must be an integer, got {port_str!r}"
                ) from None

        timeout_str = os.getenv("IDLE_TIMEOUT_MINUTES")
        if timeout_str is not None:
            try:
                raw["idle_timeout_minutes"] = int(timeout_str)
            except ValueError:
                raise ValueError(
                    f"IDLE_TIMEOUT_MINUTES must be an integer, got {timeout_str!r}"
                ) from None

        bg_tasks_str = os.getenv("MAX_BACKGROUND_TASKS")
        if bg_tasks_str is not None:
            try:
                raw["max_background_tasks"] = int(bg_tasks_str)
            except ValueError:
                raise ValueError(
                    f"MAX_BACKGROUND_TASKS must be an integer, got {bg_tasks_str!r}"
                ) from None

        max_loops_str = os.getenv("MAX_CHECK_LOOPS")
        if max_loops_str is not None:
            try:
                raw["max_check_loops"] = int(max_loops_str)
            except ValueError:
                raise ValueError(
                    f"MAX_CHECK_LOOPS must be an integer, got {max_loops_str!r}"
                ) from None

        min_interval_str = os.getenv("MIN_CHECK_LOOP_INTERVAL_SECONDS")
        if min_interval_str is not None:
            try:
                raw["min_check_loop_interval_seconds"] = float(min_interval_str)
            except ValueError:
                raise ValueError(
                    f"MIN_CHECK_LOOP_INTERVAL_SECONDS must be a float, "
                    f"got {min_interval_str!r}"
                ) from None

        cors_raw = os.getenv("CORS_ALLOW_ORIGINS")
        if cors_raw is not None:
            raw["cors_allow_origins"] = [
                origin.strip() for origin in cors_raw.split(",") if origin.strip()
            ]

        env_override("correlation_id_header", "CORRELATION_ID_HEADER")

        max_img_str = os.getenv("MAX_IMAGES_PER_MESSAGE")
        if max_img_str is not None:
            try:
                raw["max_images_per_message"] = int(max_img_str)
            except ValueError:
                raise ValueError(
                    f"MAX_IMAGES_PER_MESSAGE must be an integer, got {max_img_str!r}"
                ) from None

        max_bytes_str = os.getenv("MAX_IMAGE_BYTES")
        if max_bytes_str is not None:
            try:
                raw["max_image_bytes"] = int(max_bytes_str)
            except ValueError:
                raise ValueError(
                    f"MAX_IMAGE_BYTES must be an integer, got {max_bytes_str!r}"
                ) from None

        allowed_types = os.getenv("ALLOWED_IMAGE_MEDIA_TYPES")
        if allowed_types is not None:
            raw["allowed_image_media_types"] = [
                t.strip() for t in allowed_types.split(",") if t.strip()
            ]

        auth_enabled = os.getenv("AUTH_ENABLED")
        if auth_enabled is not None:
            auth_raw["enabled"] = _parse_bool(auth_enabled)
        auth_username = os.getenv("AUTH_USERNAME")
        if auth_username is not None:
            auth_raw["username"] = auth_username
        auth_password = os.getenv("AUTH_PASSWORD")
        if auth_password is not None:
            auth_raw["password"] = auth_password

        if auth_raw:
            raw["auth"] = auth_raw

        memory_raw = _build_memory_raw(flat.get("memory"))
        if memory_raw:
            raw["memory"] = memory_raw

        mill_raw = _build_mill_raw(flat.get("mill"))
        if mill_raw:
            raw["mill"] = mill_raw

        mail_raw = _build_mail_raw(flat.get("mail"))
        if mail_raw:
            raw["mail"] = mail_raw

        calendar_raw = _build_calendar_raw(flat.get("calendar"))
        if calendar_raw:
            raw["calendar"] = calendar_raw

        conversation_raw = _build_conversation_raw(flat.get("conversation"))
        if conversation_raw:
            raw["conversation"] = conversation_raw

        refdocs_raw = _build_refdocs_raw(flat.get("refdocs"))
        if refdocs_raw:
            raw["refdocs"] = refdocs_raw

        board_reader_raw = _build_board_reader_raw(flat.get("board_reader"))
        if board_reader_raw:
            raw["board_reader"] = board_reader_raw

        diagnostics_raw = _build_diagnostics_raw(flat.get("diagnostics"))
        if diagnostics_raw:
            raw["diagnostics"] = diagnostics_raw

        knowledge_raw = _build_knowledge_raw(flat.get("knowledge"))
        if knowledge_raw:
            raw["knowledge"] = knowledge_raw

        diagnostics_raw = _build_diagnostics_raw(flat.get("diagnostics"))
        if diagnostics_raw:
            raw["diagnostics"] = diagnostics_raw

        self_review_raw = _build_self_review_raw(flat.get("self_review"))
        if self_review_raw:
            raw["self_review"] = self_review_raw

        component_agent_raw = _build_component_agent_raw(flat.get("component_agent"))
        if component_agent_raw:
            raw["component_agent"] = component_agent_raw

        version_check_raw = _build_version_check_raw(flat.get("version_check"))
        if version_check_raw:
            raw["version_check"] = version_check_raw

        component_client_raw = _build_component_client_raw(flat.get("component_client"))
        if component_client_raw:
            raw["component_client"] = component_client_raw

        pending_questions_raw = _build_pending_questions_raw(
            flat.get("pending_questions")
        )
        if pending_questions_raw:
            raw["pending_questions"] = pending_questions_raw

        direct_repo_raw = _build_direct_repo_raw(flat.get("direct_repo"))
        if direct_repo_raw:
            raw["direct_repo"] = direct_repo_raw

        return cls(**raw)


def _build_memory_raw(yaml_memory: Any) -> dict[str, Any]:
    """Overlay ``MEMORY_*`` env vars onto the YAML ``memory`` subtree.

    Returns a nested dict (with ``llm`` / ``embedding`` sub-dicts) ready to be
    parsed into :class:`MemorySettings`, or an empty dict when nothing is set.
    """
    memory_raw: dict[str, Any] = dict(yaml_memory or {})
    llm_raw: dict[str, Any] = dict(memory_raw.get("llm") or {})
    embed_raw: dict[str, Any] = dict(memory_raw.get("embedding") or {})

    def env_set(target: dict[str, Any], field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            target[field] = value

    enabled = os.getenv("MEMORY_ENABLED")
    if enabled is not None:
        memory_raw["enabled"] = _parse_bool(enabled)
    env_set(memory_raw, "data_dir", "MEMORY_DATA_DIR")
    env_set(memory_raw, "recall_search_type", "MEMORY_RECALL_SEARCH_TYPE")

    env_set(llm_raw, "provider", "MEMORY_LLM_PROVIDER")
    env_set(llm_raw, "model", "MEMORY_LLM_MODEL")
    env_set(llm_raw, "endpoint", "MEMORY_LLM_ENDPOINT")
    env_set(llm_raw, "api_key", "MEMORY_LLM_API_KEY")

    env_set(embed_raw, "provider", "MEMORY_EMBEDDING_PROVIDER")
    env_set(embed_raw, "model", "MEMORY_EMBEDDING_MODEL")
    env_set(embed_raw, "endpoint", "MEMORY_EMBEDDING_ENDPOINT")
    env_set(embed_raw, "api_key", "MEMORY_EMBEDDING_API_KEY")
    env_set(embed_raw, "huggingface_tokenizer", "MEMORY_EMBEDDING_TOKENIZER")

    dims = os.getenv("MEMORY_EMBEDDING_DIMENSIONS")
    if dims is not None:
        try:
            embed_raw["dimensions"] = int(dims)
        except ValueError:
            raise ValueError(
                f"MEMORY_EMBEDDING_DIMENSIONS must be an integer, got {dims!r}"
            ) from None

    if llm_raw:
        memory_raw["llm"] = llm_raw
    if embed_raw:
        memory_raw["embedding"] = embed_raw
    return memory_raw


def _build_mill_raw(yaml_mill: Any) -> dict[str, Any]:
    """Overlay ``MILL_*`` env vars onto the YAML ``mill`` subtree.

    Returns a dict ready to parse into :class:`MillSettings`, or empty when
    nothing is set.
    """
    mill_raw: dict[str, Any] = dict(yaml_mill or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            mill_raw[field] = value

    enabled = os.getenv("MILL_ENABLED")
    if enabled is not None:
        mill_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "MILL_BROKER_HOST")
    env_set("broker_scheme", "MILL_BROKER_SCHEME")
    env_set("broker_token", "MILL_BROKER_TOKEN")
    env_set("agent_id", "MILL_AGENT_ID")
    env_set("board_manager_id", "MILL_BOARD_MANAGER_ID")
    env_set("repo_id", "MILL_REPO_ID")

    port_str = os.getenv("MILL_BROKER_PORT")
    if port_str is not None:
        try:
            mill_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"MILL_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("MILL_TIMEOUT")
    if timeout_str is not None:
        try:
            mill_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"MILL_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return mill_raw


def _build_mail_raw(yaml_mail: Any) -> dict[str, Any]:
    """Overlay ``MAIL_*`` env vars onto the YAML ``mail`` subtree.

    Returns a dict ready to parse into :class:`MailSettings`, or empty when
    nothing is set.
    """
    mail_raw: dict[str, Any] = dict(yaml_mail or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            mail_raw[field] = value

    enabled = os.getenv("MAIL_ENABLED")
    if enabled is not None:
        mail_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "MAIL_BROKER_HOST")
    env_set("broker_scheme", "MAIL_BROKER_SCHEME")
    env_set("broker_token", "MAIL_BROKER_TOKEN")
    env_set("agent_id", "MAIL_AGENT_ID")
    env_set("board_manager_id", "MAIL_BOARD_MANAGER_ID")

    port_str = os.getenv("MAIL_BROKER_PORT")
    if port_str is not None:
        try:
            mail_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"MAIL_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("MAIL_TIMEOUT")
    if timeout_str is not None:
        try:
            mail_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"MAIL_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return mail_raw


def _build_calendar_raw(yaml_calendar: Any) -> dict[str, Any]:
    """Overlay ``CALENDAR_*`` env vars onto the YAML ``calendar`` subtree.

    Returns a dict ready to parse into :class:`CalendarSettings`, or empty when
    nothing is set.
    """
    calendar_raw: dict[str, Any] = dict(yaml_calendar or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            calendar_raw[field] = value

    enabled = os.getenv("CALENDAR_ENABLED")
    if enabled is not None:
        calendar_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "CALENDAR_BROKER_HOST")
    env_set("broker_scheme", "CALENDAR_BROKER_SCHEME")
    env_set("broker_token", "CALENDAR_BROKER_TOKEN")
    env_set("agent_id", "CALENDAR_AGENT_ID")
    env_set("calendar_agent_id", "CALENDAR_CALENDAR_AGENT_ID")

    port_str = os.getenv("CALENDAR_BROKER_PORT")
    if port_str is not None:
        try:
            calendar_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"CALENDAR_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("CALENDAR_TIMEOUT")
    if timeout_str is not None:
        try:
            calendar_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"CALENDAR_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    cache_ttl_str = os.getenv("CALENDAR_CACHE_TTL")
    if cache_ttl_str is not None:
        try:
            calendar_raw["cache_ttl"] = float(cache_ttl_str)
        except ValueError:
            raise ValueError(
                f"CALENDAR_CACHE_TTL must be a number, got {cache_ttl_str!r}"
            ) from None

    return calendar_raw


def _build_conversation_raw(yaml_conversation: Any) -> dict[str, Any]:
    """Overlay ``CONVERSATION_*`` env vars onto the YAML ``conversation`` subtree.

    Returns a dict ready to parse into :class:`ConversationSettings`, or empty
    when nothing is set.
    """
    conversation_raw: dict[str, Any] = dict(yaml_conversation or {})

    def env_int(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is None:
            return
        try:
            conversation_raw[field] = int(value)
        except ValueError:
            raise ValueError(f"{env_name} must be an integer, got {value!r}") from None

    env_int("idle_reset_seconds", "CONVERSATION_IDLE_RESET_SECONDS")
    env_int("max_history_turns", "CONVERSATION_MAX_HISTORY_TURNS")
    env_int("max_conversations", "CONVERSATION_MAX_CONVERSATIONS")

    persist_path = os.getenv("CONVERSATION_PERSIST_PATH")
    if persist_path is not None:
        conversation_raw["persist_path"] = persist_path

    return conversation_raw


def _build_refdocs_raw(yaml_refdocs: Any) -> dict[str, Any]:
    """Overlay ``REFDOCS_*`` env vars onto the YAML ``refdocs`` subtree.

    Returns a dict ready to parse into :class:`RefDocsSettings`, or empty
    when nothing is set.
    """
    refdocs_raw: dict[str, Any] = dict(yaml_refdocs or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            refdocs_raw[field] = value

    enabled = os.getenv("REFDOCS_ENABLED")
    if enabled is not None:
        refdocs_raw["enabled"] = _parse_bool(enabled)
    env_set("github_token", "REFDOCS_GITHUB_TOKEN")
    env_set("ref", "REFDOCS_REF")
    env_set("base_url", "REFDOCS_BASE_URL")

    repos_raw = os.getenv("REFDOCS_REPOS")
    if repos_raw is not None:
        refdocs_raw["repos"] = [
            repo.strip() for repo in repos_raw.split(",") if repo.strip()
        ]

    timeout_str = os.getenv("REFDOCS_TIMEOUT")
    if timeout_str is not None:
        try:
            refdocs_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"REFDOCS_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return refdocs_raw


def _build_board_reader_raw(yaml_board_reader: Any) -> dict[str, Any]:
    """Overlay ``BOARD_READER_*`` env vars onto the YAML ``board_reader`` subtree.

    Returns a dict ready to parse into :class:`BoardReaderSettings`, or empty
    when nothing is set.
    """
    board_reader_raw: dict[str, Any] = dict(yaml_board_reader or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            board_reader_raw[field] = value

    enabled = os.getenv("BOARD_READER_ENABLED")
    if enabled is not None:
        board_reader_raw["enabled"] = _parse_bool(enabled)
    env_set("api_base_url", "BOARD_READER_API_BASE_URL")
    env_set("api_token", "BOARD_READER_API_TOKEN")

    timeout_str = os.getenv("BOARD_READER_TIMEOUT")
    if timeout_str is not None:
        try:
            board_reader_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"BOARD_READER_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    cache_ttl_str = os.getenv("BOARD_READER_CACHE_TTL")
    if cache_ttl_str is not None:
        try:
            board_reader_raw["cache_ttl"] = float(cache_ttl_str)
        except ValueError:
            raise ValueError(
                f"BOARD_READER_CACHE_TTL must be a number, got {cache_ttl_str!r}"
            ) from None

    return board_reader_raw


def _build_diagnostics_raw(yaml_diagnostics: Any) -> dict[str, Any]:
    """Overlay ``DIAGNOSTICS_*`` env vars onto the YAML ``diagnostics`` subtree.

    Returns a dict ready to parse into :class:`DiagnosticsSettings`, or empty
    when nothing is set.
    """
    diagnostics_raw: dict[str, Any] = dict(yaml_diagnostics or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            diagnostics_raw[field] = value

    enabled = os.getenv("DIAGNOSTICS_ENABLED")
    if enabled is not None:
        diagnostics_raw["enabled"] = _parse_bool(enabled)
    env_set("data_dir", "DIAGNOSTICS_DATA_DIR")

    poll_str = os.getenv("DIAGNOSTICS_POLL_INTERVAL")
    if poll_str is not None:
        try:
            diagnostics_raw["poll_interval"] = float(poll_str)
        except ValueError:
            raise ValueError(
                f"DIAGNOSTICS_POLL_INTERVAL must be a number, got {poll_str!r}"
            ) from None

    return diagnostics_raw


def _build_direct_repo_raw(yaml_direct_repo: Any) -> dict[str, Any]:
    """Overlay ``DIRECT_REPO_*`` env vars onto the YAML ``direct_repo`` subtree.

    Returns a dict ready to parse into :class:`DirectRepoSettings`, or empty
    when nothing is set.
    """
    dr_raw: dict[str, Any] = dict(yaml_direct_repo or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            dr_raw[field] = value

    enabled = os.getenv("DIRECT_REPO_ENABLED")
    if enabled is not None:
        dr_raw["enabled"] = _parse_bool(enabled)
    env_set("github_app_id", "DIRECT_REPO_GITHUB_APP_ID")
    env_set("github_app_private_key", "DIRECT_REPO_GITHUB_APP_PRIVATE_KEY")
    env_set("github_app_installation_id", "DIRECT_REPO_GITHUB_APP_INSTALLATION_ID")
    env_set("github_api_base_url", "DIRECT_REPO_GITHUB_API_BASE_URL")
    env_set("board_api_base_url", "DIRECT_REPO_BOARD_API_BASE_URL")
    env_set("board_api_token", "DIRECT_REPO_BOARD_API_TOKEN")

    timeout_str = os.getenv("DIRECT_REPO_TIMEOUT")
    if timeout_str is not None:
        try:
            dr_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"DIRECT_REPO_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return dr_raw


def _build_knowledge_raw(yaml_knowledge: Any) -> dict[str, Any]:
    """Overlay ``KNOWLEDGE_*`` env vars onto the YAML ``knowledge`` subtree.

    Returns a dict ready to parse into :class:`KnowledgeSettings`, or empty
    when nothing is set.
    """
    knowledge_raw: dict[str, Any] = dict(yaml_knowledge or {})

    enabled = os.getenv("KNOWLEDGE_ENABLED")
    if enabled is not None:
        knowledge_raw["enabled"] = _parse_bool(enabled)

    path = os.getenv("KNOWLEDGE_PATH")
    if path is not None:
        knowledge_raw["path"] = path

    return knowledge_raw


def _build_diagnostics_raw(yaml_diagnostics: Any) -> dict[str, Any]:
    """Overlay ``DIAGNOSTICS_*`` env vars onto the YAML ``diagnostics`` subtree.

    Returns a dict ready to parse into :class:`DiagnosticsSettings`, or empty
    when nothing is set.
    """
    raw: dict[str, Any] = dict(yaml_diagnostics or {})

    enabled = os.getenv("DIAGNOSTICS_ENABLED")
    if enabled is not None:
        raw["enabled"] = _parse_bool(enabled)

    store_path = os.getenv("DIAGNOSTICS_STORE_PATH")
    if store_path is not None:
        raw["store_path"] = store_path

    proposals_path = os.getenv("DIAGNOSTICS_PROPOSALS_PATH")
    if proposals_path is not None:
        raw["proposals_path"] = proposals_path

    threshold_str = os.getenv("DIAGNOSTICS_RECURRENCE_THRESHOLD")
    if threshold_str is not None:
        try:
            raw["recurrence_threshold"] = int(threshold_str)
        except ValueError:
            raise ValueError(
                f"DIAGNOSTICS_RECURRENCE_THRESHOLD must be an integer, "
                f"got {threshold_str!r}"
            ) from None

    window_str = os.getenv("DIAGNOSTICS_RECURRENCE_WINDOW_DAYS")
    if window_str is not None:
        try:
            raw["recurrence_window_days"] = int(window_str)
        except ValueError:
            raise ValueError(
                f"DIAGNOSTICS_RECURRENCE_WINDOW_DAYS must be an integer, "
                f"got {window_str!r}"
            ) from None

    return raw


def _build_pending_questions_raw(yaml_data: Any) -> dict[str, Any]:
    """Overlay ``PENDING_QUESTIONS_*`` env vars onto the YAML ``pending_questions``.

    Returns a dict ready to parse into :class:`PendingQuestionsSettings`, or empty
    when nothing is set.
    """
    raw: dict[str, Any] = dict(yaml_data or {})

    enabled = os.getenv("PENDING_QUESTIONS_ENABLED")
    if enabled is not None:
        raw["enabled"] = _parse_bool(enabled)

    return raw


def _build_self_review_raw(yaml_self_review: Any) -> dict[str, Any]:
    """Overlay ``SELF_REVIEW_*`` env vars onto the YAML ``self_review`` subtree.

    Returns a dict ready to parse into :class:`SelfReviewSettings`, or empty
    when nothing is set.
    """
    self_review_raw: dict[str, Any] = dict(yaml_self_review or {})

    enabled = os.getenv("SELF_REVIEW_ENABLED")
    if enabled is not None:
        self_review_raw["enabled"] = _parse_bool(enabled)

    limit_str = os.getenv("SELF_REVIEW_RECENT_ACTIVITY_LIMIT")
    if limit_str is not None:
        try:
            self_review_raw["recent_activity_limit"] = int(limit_str)
        except ValueError:
            raise ValueError(
                f"SELF_REVIEW_RECENT_ACTIVITY_LIMIT must be an integer, "
                f"got {limit_str!r}"
            ) from None

    return self_review_raw


def _build_component_agent_raw(yaml_component_agent: Any) -> dict[str, Any]:
    """Overlay ``COMPONENT_AGENT_*`` env vars onto the YAML ``component_agent`` subtree.

    Returns a dict ready to parse into :class:`ComponentAgentSettings`, or empty
    when nothing is set.
    """
    component_agent_raw: dict[str, Any] = dict(yaml_component_agent or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            component_agent_raw[field] = value

    enabled = os.getenv("COMPONENT_AGENT_ENABLED")
    if enabled is not None:
        component_agent_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "COMPONENT_AGENT_BROKER_HOST")
    env_set("broker_scheme", "COMPONENT_AGENT_BROKER_SCHEME")
    env_set("broker_token", "COMPONENT_AGENT_BROKER_TOKEN")
    env_set("agent_id", "COMPONENT_AGENT_AGENT_ID")

    port_str = os.getenv("COMPONENT_AGENT_BROKER_PORT")
    if port_str is not None:
        try:
            component_agent_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_AGENT_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("COMPONENT_AGENT_TIMEOUT")
    if timeout_str is not None:
        try:
            component_agent_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_AGENT_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return component_agent_raw


def _build_version_check_raw(yaml_version_check: Any) -> dict[str, Any]:
    """Overlay ``VERSION_CHECK_*`` env vars onto the YAML ``version_check`` subtree.

    Returns a dict ready to parse into :class:`VersionCheckSettings`, or empty
    when nothing is set.
    """
    version_check_raw: dict[str, Any] = dict(yaml_version_check or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            version_check_raw[field] = value

    enabled = os.getenv("VERSION_CHECK_ENABLED")
    if enabled is not None:
        version_check_raw["enabled"] = _parse_bool(enabled)
    env_set("repo", "VERSION_CHECK_REPO")
    env_set("github_token", "VERSION_CHECK_GITHUB_TOKEN")
    env_set("base_url", "VERSION_CHECK_BASE_URL")

    timeout_str = os.getenv("VERSION_CHECK_TIMEOUT")
    if timeout_str is not None:
        try:
            version_check_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"VERSION_CHECK_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    cache_ttl_str = os.getenv("VERSION_CHECK_CACHE_TTL")
    if cache_ttl_str is not None:
        try:
            version_check_raw["cache_ttl"] = float(cache_ttl_str)
        except ValueError:
            raise ValueError(
                f"VERSION_CHECK_CACHE_TTL must be a number, got {cache_ttl_str!r}"
            ) from None

    return version_check_raw


def _build_component_client_raw(yaml_component_client: Any) -> dict[str, Any]:
    """Overlay ``COMPONENT_CLIENT_*`` env vars onto the ``component_client`` subtree.

    Returns a dict ready to parse into
    :class:`ComponentClientSettings`, or empty when nothing is set.
    """
    cc_raw: dict[str, Any] = dict(yaml_component_client or {})

    def env_set(field: str, env_name: str) -> None:
        value = os.getenv(env_name)
        if value is not None:
            cc_raw[field] = value

    enabled = os.getenv("COMPONENT_CLIENT_ENABLED")
    if enabled is not None:
        cc_raw["enabled"] = _parse_bool(enabled)
    env_set("broker_host", "COMPONENT_CLIENT_BROKER_HOST")
    env_set("broker_scheme", "COMPONENT_CLIENT_BROKER_SCHEME")
    env_set("broker_token", "COMPONENT_CLIENT_BROKER_TOKEN")
    env_set("agent_id", "COMPONENT_CLIENT_AGENT_ID")

    port_str = os.getenv("COMPONENT_CLIENT_BROKER_PORT")
    if port_str is not None:
        try:
            cc_raw["broker_port"] = int(port_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_CLIENT_BROKER_PORT must be an integer, got {port_str!r}"
            ) from None

    timeout_str = os.getenv("COMPONENT_CLIENT_TIMEOUT")
    if timeout_str is not None:
        try:
            cc_raw["timeout"] = float(timeout_str)
        except ValueError:
            raise ValueError(
                f"COMPONENT_CLIENT_TIMEOUT must be a number, got {timeout_str!r}"
            ) from None

    return cc_raw


def _load_dotenv() -> None:
    """Load a ``.env`` file into the environment if python-dotenv is present."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover — python-dotenv is a required dep
        logger.debug("python-dotenv not installed; skipping .env loading")
