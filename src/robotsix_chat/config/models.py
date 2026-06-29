"""Pydantic sub-models that compose the top-level :class:`Settings`.

Each model is self-contained — zero intra-model dependencies — so they can
be imported directly without pulling in the full Settings cascade.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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
    """Diagnostics capture and systemic fix surfacing.

    When enabled, the agent captures diagnostic bundles for failure events
    and can detect recurring failure categories.  When a category crosses
    the recurrence threshold a ``FixProposal`` is auto-generated (but NOT
    auto-applied) for agent or human review.

    Applied fixes are tracked in the effectiveness store; after the
    observation window elapses a ``FixEffectivenessReport`` is generated
    comparing pre-fix and post-fix recurrence counts.

    Attributes:
        enabled: Master switch.  Default ``True``.
        store_path: Path to the diagnostic-event JSON persistence file.
            Default ``.data/diagnostics.json``.
        proposals_path: Path to the fix-proposal JSON persistence file.
            Default ``.data/fix_proposals.json``.
        effectiveness_path: Path to the effectiveness-report JSON
            persistence file.  Default ``.data/diagnostics_effectiveness.json``.
        recurrence_threshold: Minimum number of occurrences within the
            window to trigger a recurrence alert.  Default ``3``.
        recurrence_window_days: Look-back window in days for recurrence
            detection.  Default ``30``.
        observation_window_days: Days after a fix is applied to wait before
            generating an effectiveness report.  The pre-fix and post-fix
            windows are both this many days.  Default ``30``.

    """

    enabled: bool = True
    store_path: str = ".data/diagnostics.json"
    proposals_path: str = ".data/fix_proposals.json"
    effectiveness_path: str = ".data/diagnostics_effectiveness.json"
    recurrence_threshold: int = 3
    recurrence_window_days: int = 30
    observation_window_days: int = 30


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
