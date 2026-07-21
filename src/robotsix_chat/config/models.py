"""Pydantic sub-models that compose the top-level :class:`Settings`.

Each model is self-contained — zero intra-model dependencies — so they can
be imported directly without pulling in the full Settings cascade.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class LangfuseSettings(BaseModel):
    """Langfuse observability credentials."""

    public_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    host: str = "https://cloud.langfuse.com"
    model_config = ConfigDict(extra="forbid")


class MemoryLlmSettings(BaseModel):
    """Extraction-LLM config for cognee memory (OpenRouter via litellm).

    Defaults match the validated robotsix setup: Claude Haiku through
    OpenRouter's ``custom`` provider. ``api_key`` is required when memory
    is enabled (provide it via ``MEMORY_LLM_API_KEY``).
    """

    provider: str = "custom"
    # deepseek-v4-flash produced malformed JSON under instructor's
    # structured-output prompting, causing multi-minute retry stalls in
    # production (2026-07-09).
    model: str = "openrouter/anthropic/claude-haiku-4.5"
    endpoint: str = "https://openrouter.ai/api/v1"
    api_key: SecretStr = SecretStr("")
    model_config = ConfigDict(extra="forbid")


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
    api_key: SecretStr = SecretStr("ollama")
    huggingface_tokenizer: str = "BAAI/bge-m3"
    model_config = ConfigDict(extra="forbid")


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
        recall_timeout_seconds: Hard timeout (seconds) for a single ``recall``
            call.  On expiry the recall degrades to ``""`` — the agent proceeds
            without memory.  Default 60 s.
        remember_timeout_seconds: Hard timeout (seconds) for a single
            ``remember`` call (cognify consolidation).  On expiry the write is
            skipped and a warning is logged.  Default 300 s.
        write_backlog_path: Path to a durable JSONL backlog for exchanges that
            could not be persisted after retries are exhausted.  The backlog is
            drained opportunistically on subsequent successful writes.
            Default ``/data/cognee/backlog.jsonl``.
        datafusion_runtime_memory_limit: DataFusion memory-pool limit applied
            via the ``DATAFUSION_RUNTIME_MEMORY_LIMIT`` env var before cognee
            import.  Accepts human-readable sizes (``"256M"``, ``"1G"``, ...).
            Bounds the LanceDB worker subprocess memory so a single large
            ``merge_insert`` does not OOM the container.  Default ``"256M"``
            (safe for a 2 GB container; raise for larger limits).
        frozen_store_alert_minutes: Consecutive-write-failure duration (minutes)
            after which a ``WARNING`` diagnostic is emitted so a silently
            frozen vector store cannot go unnoticed for days.  Default ``10.0``.
        write_throttle_seconds: Delay (seconds) between serialised writes so
            the LanceDB worker subprocess can complete its ``merge_insert``
            before the next write starts.  Prevents a burst of many concurrent
            writes from collectively exhausting the worker's memory.
            Default ``0.5``.
        llm: Extraction-LLM config (graph building / consolidation).
        embedding: Embedding-server config (semantic search).
        langfuse: Dedicated Langfuse credentials for the
            ``robotsix-chat-cognee`` project (separate from the main chat's
            Langfuse project). When the public key is empty, cognee LLM calls
            are not traced.

    """

    enabled: bool = False
    data_dir: str = "/data/cognee"
    recall_search_type: str = "GRAPH_COMPLETION"
    recall_timeout_seconds: float = 60.0
    remember_timeout_seconds: float = 300.0
    write_backlog_path: str = "/data/cognee/backlog.jsonl"
    datafusion_runtime_memory_limit: str = "256M"
    frozen_store_alert_minutes: float = 10.0
    write_throttle_seconds: float = 0.5
    llm: MemoryLlmSettings = Field(default_factory=MemoryLlmSettings)
    embedding: MemoryEmbeddingSettings = Field(default_factory=MemoryEmbeddingSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    model_config = ConfigDict(extra="forbid")


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
    github_token: SecretStr = SecretStr("")
    base_url: str = "https://api.github.com"
    timeout: float = 30.0
    model_config = ConfigDict(extra="forbid")


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
    github_token: SecretStr = SecretStr("")
    base_url: str = "https://api.github.com"
    timeout: float = 30.0
    cache_ttl: float = 300.0
    model_config = ConfigDict(extra="forbid")


class MailSettings(BaseModel):
    """Direct HTTP access to the auto-mail board server. Disabled by default.

    When enabled, the chat agent gains discrete tools that call the
    auto-mail board HTTP API directly (no broker indirection, no NL
    reinterpretation): get the board content, check email status, move
    / delete / archive emails, and run triage.

    Attributes:
        enabled: Master switch.  When ``False``, no mail tools are offered.
        api_base_url: Base URL of the auto-mail board HTTP server (no
            trailing slash).  Default ``http://127.0.0.1:8077``.
        api_token: Optional Bearer token; empty means no Authorization
            header.
        timeout: Per-request HTTP timeout in seconds.

    """

    enabled: bool = False
    api_base_url: str = "http://127.0.0.1:8077"
    api_token: SecretStr = SecretStr("")
    timeout: float = 30.0

    model_config = ConfigDict(extra="forbid")


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
            Default ``/data/diagnostics.json``.
        proposals_path: Path to the fix-proposal JSON persistence file.
            Default ``/data/fix_proposals.json``.
        effectiveness_path: Path to the effectiveness-report JSON
            persistence file.  Default ``/data/diagnostics_effectiveness.json``.
        recurrence_threshold: Minimum number of occurrences within the
            window to trigger a recurrence alert.  Default ``3``.
        recurrence_window_days: Look-back window in days for recurrence
            detection.  Default ``30``.
        observation_window_days: Days after a fix is applied to wait before
            generating an effectiveness report.  The pre-fix and post-fix
            windows are both this many days.  Default ``30``.

    """

    enabled: bool = True
    store_path: str = "/data/diagnostics.json"
    proposals_path: str = "/data/fix_proposals.json"
    effectiveness_path: str = "/data/diagnostics_effectiveness.json"
    recurrence_threshold: int = 3
    recurrence_window_days: int = 30
    observation_window_days: int = 30
    model_config = ConfigDict(extra="forbid")


class DirectRepoSettings(BaseModel):
    """Direct-repo push-branch, open-PR, and direct-fix capability.

    Authenticates as the robotsix-mill GitHub App.  When enabled, the chat
    agent gains tools: ``push_direct_repo_branch``
    (create/push a branch with file changes), ``open_direct_repo_pr``
    (open a PR from a branch), and — when *direct_fix_enabled* is also
    ``True`` — ``direct_fix`` (push a commit directly to a target branch,
    bypassing the PR flow).  All authenticate as the configured GitHub App
    installation (JWT → short-lived installation token) and dynamically
    resolve the allowed repo set from the installation at action time —
    no static allowlist.

    **Guardrails built into the tools (not configurable):**
    - Actions are ONLY permitted for tickets in BLOCKED state.
    - Repo scope is resolved dynamically from the GitHub App installation.
    - PRs are opened in a reviewable state with no auto-merge.
    - No merge capability exists on this path.

    **Additional guardrails for ``direct_fix``:**
    - Ticket must have exhausted its spawn limit (≥3 implement cycles)
      verified against the board API.
    - Every direct-fix action is logged at WARNING level for auditability.

    Attributes:
        enabled: Master switch.  When ``False``, no direct-repo tools are
            offered.
        direct_fix_enabled: When ``True`` (and *enabled* is ``True``), the
            ``direct_fix`` tool is available for pushing commits directly
            to a target branch after mill exhaustion.  Default ``False``.
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
    direct_fix_enabled: bool = False
    github_app_id: str = ""
    github_app_private_key: SecretStr = SecretStr("")
    github_app_installation_id: str = ""
    github_api_base_url: str = "https://api.github.com"
    board_api_base_url: str = "http://127.0.0.1:8077"
    board_api_token: SecretStr = SecretStr("")
    timeout: float = 30.0
    model_config = ConfigDict(extra="forbid")


class RepoStudySettings(BaseModel):
    """Temporary local repo workspaces the agent can fetch and study.

    When enabled, the chat agent gains read-only tools to download a GitHub
    repository snapshot (tarball — no ``git`` binary involved), extract it
    into a temporary workspace under *data_dir*, and study it locally
    (list / read / regex-search files) before dropping it.  Workspaces are
    transient: they are deleted on demand (``drop_repo_workspace``) and
    swept automatically once older than *ttl_minutes*.

    Authentication reuses the ``direct_repo`` GitHub App credentials when
    they are configured (the app's installation scope defines the private
    repos the agent may fetch); without them only public repositories are
    reachable.  No new credential fields are introduced.

    Attributes:
        enabled: Master switch.  When ``False``, no repo-study tools are
            offered.
        data_dir: Directory holding the temporary workspaces.  Default
            ``/data/repo_study`` (on the persistent volume, so a redeploy
            mid-study does not lose the workspace; the TTL sweep still
            bounds growth).
        ttl_minutes: Age after which a workspace is deleted by the sweep
            that runs on every repo-study tool call.
        max_archive_bytes: Maximum size of the downloaded tarball.
        max_extracted_bytes: Maximum total uncompressed size of a workspace.
        max_read_bytes: Maximum bytes returned by a single file read.
        timeout: Per-request HTTP timeout in seconds for the download.

    """

    enabled: bool = False
    data_dir: str = "/data/repo_study"
    ttl_minutes: int = 240
    max_archive_bytes: int = 67_108_864
    max_extracted_bytes: int = 268_435_456
    max_read_bytes: int = 204_800
    timeout: float = 60.0
    model_config = ConfigDict(extra="forbid")


class KnowledgeSettings(BaseModel):
    """Local, writable knowledge base for agent-authored operational notes.

    A deliberate, explicit, agent-curated store of durable lessons and findings
    — plain local JSON, no embeddings, no external service, always-on.  The
    agent writes notes via five tools
    (``add_knowledge_note``, ``append_to_knowledge_note``,
    ``update_knowledge_note``, ``list_knowledge_notes``,
    ``read_knowledge_note``)
    and can re-read and revise them by id across sessions.

    This store is **complementary to**, not a duplicate of, the optional cognee
    episodic memory system (``memory/``).  cognee automatically recalls past
    conversations by similarity; this knowledge base holds notes the agent
    deliberately authors and addresses by id.

    Attributes:
        enabled: Master switch.  Default ``True`` — this is a purely local,
            no-credential, no-external-dependency primitive.
        path: Path to the JSON persistence file.  Default
            ``/data/knowledge.json``.

    """

    enabled: bool = True
    path: str = "/data/knowledge.json"
    model_config = ConfigDict(extra="forbid")


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
    model_config = ConfigDict(extra="forbid")


class ComponentTarget(BaseModel):
    """A single component agent that the chat may inspect or configure.

    Attributes:
        base_url: Base URL of the component agent (e.g.
            ``"http://comp-1:8090"``).
        label: Optional human-readable label shown in discovery output.

    """

    base_url: str
    label: str = ""
    model_config = ConfigDict(extra="forbid")


class ComponentClientSettings(BaseModel):
    """Component agent client settings — inspect and configure remote agents.

    When enabled, the chat agent gains four tools: ``list_component_agents``,
    ``get_component_telemetry``, ``get_component_config``, and
    ``set_component_config`` so it can enumerate configured component agents,
    read live telemetry, and read/update configuration on demand via direct
    HTTP.

    Attributes:
        enabled: Master switch.
        timeout: Per-request HTTP timeout (seconds).
        components: Allowlist of component agents the chat may contact.
            Each entry has a ``base_url`` and an optional ``label``.

    """

    enabled: bool = False
    timeout: float = 240.0
    components: list[ComponentTarget] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid")


class SubsessionsSettings(BaseModel):
    """Unified subsession system — background agents spawned from a chat.

    A subsession is a background agent run (``task``, ``periodic``, or
    ``user_chat``) spawned by the main chat agent — or, nested, by another
    subsession — with its own model level chosen by task difficulty.

    Attributes:
        max_concurrent: Process-wide cap on simultaneously active
            subsessions (all kinds, all depths).
            Env override: ``SUBSESSIONS_MAX_CONCURRENT``.
        max_depth: Maximum nesting depth.  The main chat session is depth
            0; its subsessions are depth 1.  Agents at ``max_depth`` get
            no spawn tools.  Env override: ``SUBSESSIONS_MAX_DEPTH``.
        default_model_level: llmio capability level used when the
            spawning agent does not pick one explicitly (1 cheapest … 4
            frontier).  Env override: ``SUBSESSIONS_DEFAULT_MODEL_LEVEL``.
        min_interval_seconds: Minimum interval for ``periodic``
            subsessions.  Env override: ``SUBSESSIONS_MIN_INTERVAL_SECONDS``.
        auto_stop_no_change_runs: A periodic subsession auto-closes after
            this many consecutive ``NO_CHANGE`` runs.
            Env override: ``SUBSESSIONS_AUTO_STOP_NO_CHANGE_RUNS``.
        human_approval_timeout_runs: When a periodic subsession's checkpoint
            indicates the monitored ticket is in ``human_issue_approval``
            state, auto-escalate (close with reason
            ``human_approval_timeout``) after this many consecutive
            ``NO_CHANGE`` runs.  Default ``5``.
            Env override: ``SUBSESSIONS_HUMAN_APPROVAL_TIMEOUT_RUNS``.
        run_timeout_seconds: Hard per-run timeout for a single subsession
            agent turn (recall + LLM call + delivery).  On expiry the run
            is marked failed and the schedule continues instead of staying
            ``running`` forever.  Default 600 s.
            Env override: ``SUBSESSIONS_RUN_TIMEOUT_SECONDS``.
        store_path: JSON persistence file (periodic subsessions resume
            across restarts).  Env override: ``SUBSESSIONS_STORE_PATH``.
        transcript_max_entries: Per-subsession transcript retention cap.
            Env override: ``SUBSESSIONS_TRANSCRIPT_MAX_ENTRIES``.

    """

    max_concurrent: int = 8
    max_depth: int = 3
    default_model_level: int = 2
    min_interval_seconds: float = 60.0
    auto_stop_no_change_runs: int = 5
    human_approval_timeout_runs: int = 5
    run_timeout_seconds: float = 600.0
    store_path: str = "/data/subsessions.json"
    transcript_max_entries: int = 200
    model_config = ConfigDict(extra="forbid")


class ConversationSettings(BaseModel):
    """Multi-session conversation continuity for the browser chat.

    The server groups conversations by a per-browser ``owner_id`` and addresses
    individual sessions by ``session_id``. Each owner can have multiple named
    sessions with independent turn histories. History is **never** wiped on
    idle — sessions are persistent when ``persist_path`` is configured.

    Attributes:
        max_history_turns: Most recent user/assistant turns kept per
            session and replayed to the agent (bounds prompt size).
        max_conversations: Maximum number of distinct sessions tracked at once
            (LRU-evicted); bounds the in-memory store.
        persist_path: Path to the JSON persistence file. Default
            ``/data/conversations.json``. Set to an empty string to disable.

    """

    max_history_turns: int = 50
    max_conversations: int = 1000
    persist_path: str = "/data/conversations.json"
    model_config = ConfigDict(extra="forbid")


class LifecycleSettings(BaseModel):
    """Read-only deploy-lifecycle API access for the agent.

    When enabled, the chat agent gains read-only tools to inspect the
    central-deploy lifecycle server: list services, check service status
    and health, and read configuration and environment (with secrets
    already masked as ``***`` server-side by ``_mask_secrets``).

    Attributes:
        enabled: Master switch.  When ``False``, no lifecycle tools are
            offered.
        base_url: Base URL of the deploy-lifecycle API server (no trailing
            slash).
        api_key: API key sent as the ``X-API-Key`` header.  Injected
            server-side from ``ROBOTSIX_LIFECYCLE_API_KEY``.
        timeout: Per-request HTTP timeout in seconds.

    """

    enabled: bool = False
    base_url: str = ""
    api_key: SecretStr = SecretStr("")
    timeout: float = 30.0
    model_config = ConfigDict(extra="forbid")


class GitHubSecuritySettings(BaseModel):
    """Repository security-feature toggle via the GitHub App installation.

    When enabled, the chat agent gains a ``set_repo_security_and_analysis``
    tool that can enable or disable repository-level security features
    (dependency graph, advanced security, secret scanning) on repos under
    the configured GitHub App's installation scope.

    **Guardrails built into the tool (not configurable):**
    - Repo scope is resolved dynamically from the GitHub App installation
      (list-installation-repositories) — no static allowlist.
    - Only repos within the installation scope are modifiable.
    - Each feature toggle explicitly requires ``"enabled"`` or ``"disabled"``
      — no accidental bulk changes.

    Attributes:
        enabled: Master switch.  When ``False``, no security-feature tool
            is offered.
        github_org: GitHub organisation name whose repos are in scope
            (e.g. ``"damien-robotsix"``).  The tool only targets repos
            under this org.
        deploy_api_key: API key that clients must present in the
            ``X-API-Key`` header when calling the
            ``PATCH /chat/github/repos/{owner}/{repo}/settings``
            endpoint.  When empty, the endpoint returns 503 (unconfigured).
        timeout: Per-request HTTP timeout in seconds.

    Note: GitHub App authentication is delegated to
    :class:`DirectRepoSettings` — those credentials must also be configured
    for the tool to function.

    """

    enabled: bool = False
    github_org: str = "damien-robotsix"
    deploy_api_key: SecretStr = SecretStr("")
    timeout: float = 30.0
    model_config = ConfigDict(extra="forbid")


class NotificationSettings(BaseModel):
    """Browser notification settings — lets the agent alert the user proactively.

    When enabled, the agent gains a ``notify_user`` tool that publishes a
    notification event to connected clients over the existing SSE channel
    (EventBus).  The user's browser renders the event via the native
    Notifications API.

    Delivery only reaches clients that are currently connected — the
    notification is silently dropped when no browser is listening.

    Attributes:
        enabled: Master switch.  When ``False``, no notify_user tool is
            offered.

    """

    enabled: bool = False
    model_config = ConfigDict(extra="forbid")


class FeedbackSettings(BaseModel):
    """Automated feedback analysis for continuous self-improvement.

    When enabled, a feedback run analyses the conversation at compaction
    and session-end boundaries, then files improvement tickets via the
    board's ``POST /tickets/ingest`` endpoint.  Tickets flow through the
    normal human-approval workflow — the feedback run never auto-approves.

    Attributes:
        enabled: Master switch.  When ``False``, no feedback runs occur.
        model_level: llmio capability level for the feedback-analysis
            agent (a cheap, single-turn extraction call).  Default ``1``.
        board_url: Base URL of the board HTTP API (no trailing slash).
            Required when *enabled* — the runner POSTs to
            ``{board_url}/tickets/ingest``.
        board_api_token: Optional Bearer token for the board API.
        timeout: Per-request HTTP timeout in seconds for ingest calls.
            The set of allowed target repos is resolved dynamically at
            run-time from the deploy server's chat-component roster
            (``DEPLOY_API_KEY`` env var) intersected with the mill board's
            repo registry — no static allowlist is needed.

    """

    enabled: bool = False
    model_level: int = 1
    board_url: str = ""
    board_api_token: SecretStr = SecretStr("")
    timeout: float = 60.0
    model_config = ConfigDict(extra="forbid")


class RenderUrlSettings(BaseModel):
    """Read-only URL rendering with headless Chromium (Playwright).

    When enabled, the agent gains a tool that loads a URL in a headless
    Chromium browser (via Playwright), takes a full-page screenshot, and
    extracts the accessibility tree — both returned as structured output.
    No interactive browsing, form-filling, or navigation beyond the initial
    page load is permitted.

    Attributes:
        enabled: Master switch.  When ``False``, no URL-render tool is offered.
        timeout: Per-request timeout in seconds for the page load.
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.

    """

    enabled: bool = False
    timeout: float = 30.0
    viewport_width: int = 1280
    viewport_height: int = 720
    model_config = ConfigDict(extra="forbid")


class HttpProbeSettings(BaseModel):
    """Read-only HTTP uptime/render-probe tool for the agent.

    When enabled, the agent gains an ``http_probe`` tool that performs a
    plain HTTPS GET to a public URL (follows redirects, short timeout)
    and returns the HTTP status, final URL, response time, Content-Type,
    response size, and a snippet of the body text with optional content
    assertions.

    Attributes:
        enabled: Master switch.  When ``False``, no http_probe tool is offered.
        timeout: Per-request HTTP timeout in seconds (default 10 s).
        allowlist: Hostnames (no protocol, no path) that the tool is permitted to
            probe.  At minimum must include ``www.robotsix.net`` and
            ``robotsix.net``.  When empty, the tool permits any public hostname.
        max_body_bytes: Maximum bytes of the response body to read and
            return to the agent (default 2048 — ~2 KB).
        max_redirects: Maximum number of redirects to follow (default 5).

    """

    enabled: bool = False
    timeout: float = 10.0
    allowlist: list[str] = Field(
        default_factory=lambda: ["www.robotsix.net", "robotsix.net"]
    )
    max_body_bytes: int = 2048
    max_redirects: int = 5
    model_config = ConfigDict(extra="forbid")


class AutonomousSettings(BaseModel):
    """Native autonomous chat sessions — self-directed agent loops.

    When enabled, the agent can run fully autonomous sessions that pick a
    subject, draft a plan, await operator approval, then execute to
    completion with auto-cycling.

    Attributes:
        enabled: Master switch.  Default ``False``.
        approval_marker: Marker string the agent emits after drafting a plan
            to signal it is awaiting operator approval.
        completion_marker: Marker string the agent emits when the plan is
            complete; triggers auto-close and respawn.
        max_auto_turns: Maximum number of automatic agent turns during the
            execution phase before reverting to ``awaiting_approval``.

    """

    enabled: bool = False
    approval_marker: str = "---AWAITING APPROVAL---"
    completion_marker: str = "---AUTONOMOUS COMPLETE---"
    max_auto_turns: int = 20
    model_config = ConfigDict(extra="forbid")


class CentralDeploySettings(BaseModel):
    """Central-deploy roster and component-access settings.

    Provides the base URL and bearer token for the central-deploy
    management-plane API.  At session start the agent fetches the
    ``GET /chat/components`` roster (a list of component agents the chat
    is allowed to call), caches it with a short TTL, and loads each
    component's declared skill into the agent.

    Attributes:
        url: Base URL of the central-deploy API (no trailing slash).
        api_token: Bearer token for authenticating to the central-deploy
            API.  Required when any component access is expected.
        roster_cache_ttl: Seconds to cache the roster before re-fetching.
            Default 300 (5 min).

    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    api_token: SecretStr = SecretStr("")
    roster_cache_ttl: float = 300.0
    component_response_max_chars: int = 200_000
