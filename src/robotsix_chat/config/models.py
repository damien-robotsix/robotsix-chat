"""Pydantic sub-models that compose the top-level :class:`Settings`.

Each model is self-contained — zero intra-model dependencies — so they can
be imported directly without pulling in the full Settings cascade.
"""

from __future__ import annotations

import enum
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

_logger = logging.getLogger(__name__)

#: Langfuse project for the main chat agent's LLM traffic.  The component
#: standard fixes a component's main project name as ``<repo>``.
PROJECT_MAIN = "robotsix-chat"

#: Langfuse project for the cognee memory subsystem's own LLM traffic.  Per
#: the one-project-per-function rule each LLM-generating subsystem traces to
#: its own ``<repo>-<function>`` project, never the component's main one.
PROJECT_MEMORY = "robotsix-chat-cognee"


class LangfuseProjectCreds(BaseModel):
    """Credentials for one Langfuse project.

    Attributes:
        public_key: Langfuse public key for the project.
        secret_key: Langfuse secret key for the project.
        project_id: Langfuse project id.  Optional — only consumers that
            address a project by id rather than by name need it.

    """

    public_key: SecretStr = SecretStr("")
    secret_key: SecretStr = SecretStr("")
    project_id: str = ""
    model_config = ConfigDict(extra="forbid")

    def is_configured(self) -> bool:
        """Return ``True`` when both key halves are set."""
        return bool(
            self.public_key.get_secret_value() and self.secret_key.get_secret_value()
        )


class LangfuseSettings(BaseModel):
    """Canonical Langfuse credential block (component standard).

    One block per component, holding the instance ``host`` and every
    Langfuse project the component traces to, keyed by the project's
    **name**.  The component standard fixes those names as ``<repo>`` for
    the component's main LLM function and ``<repo>-<function>`` for each
    additional LLM-generating subsystem — so this component declares
    ``robotsix-chat`` (main agent) and ``robotsix-chat-cognee`` (memory).

    Keeping every project in one standard block is what lets central-deploy
    enumerate the fleet's credentials uniformly and dispatch them to the
    consumers that need them (the chat trace proxy, cost-monitor's
    reconciliation).  See ``PROJECT_MAIN`` / ``PROJECT_MEMORY`` in
    :mod:`robotsix_chat.config` for this component's names.

    Attributes:
        host: Langfuse instance base URL.
        projects: Langfuse project name → credentials.

    """

    host: str = "https://cloud.langfuse.com"
    projects: dict[str, LangfuseProjectCreds] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_single_project_keys(cls, data: Any) -> Any:
        """Strip the pre-block ``public_key``/``secret_key`` fields.

        A deployed ``config.json`` written before the credential block
        existed carries these at the top of ``langfuse``.  ``extra="forbid"``
        would otherwise reject the whole file and crash-loop the container on
        the first start after an image upgrade.

        The values are **not** migrated — per the standard there is no
        credential fallback, so an unmigrated deployment traces nothing and
        reports no projects until its config is rewritten.  Dropping them
        only keeps that a visible, fixable state instead of an outage.
        """
        if isinstance(data, dict):
            legacy = [k for k in ("public_key", "secret_key") if k in data]
            if legacy:
                data = {k: v for k, v in data.items() if k not in legacy}
                _logger.warning(
                    "Ignoring legacy langfuse.%s — credentials now live in "
                    "langfuse.projects.<project-name>; this deployment will "
                    "not trace until its config is migrated",
                    "/".join(legacy),
                )
        return data

    def creds(self, project: str) -> LangfuseProjectCreds:
        """Return credentials for *project*, or empty creds when absent.

        Absent and half-filled projects both yield credentials whose
        ``is_configured()`` is ``False``, so callers degrade to "tracing
        off" rather than raising.
        """
        return self.projects.get(project) or LangfuseProjectCreds()


class LangfuseInspectSettings(BaseModel):
    """Langfuse trace-inspection tool — lets the agent query recent traces.

    Reuses the main ``langfuse`` credentials (public key + secret key + host)
    for API authentication — no separate credential fields.  When enabled, the
    agent gains an ``inspect_langfuse_trace`` tool that fetches and summarises
    recent implement traces for a given ticket or trace id.

    Attributes:
        enabled: Master switch.  Default ``False``.
        max_traces: Maximum number of traces returned per query.  Default ``5``.

    """

    enabled: bool = False
    max_traces: int = 5
    model_config = ConfigDict(extra="forbid")


class MemoryLlmSettings(BaseModel):
    """Extraction-LLM config for cognee memory (OpenRouter via litellm).

    Defaults match the validated robotsix setup: ``gpt-5-nano`` (cognee's
    cheapest model, reliable json_mode structured output) through
    OpenRouter's ``custom`` provider. ``api_key`` is required when memory
    is enabled (provide it via ``MEMORY_LLM_API_KEY``).

    The default is deliberately a **cheap** model: cognee runs an
    extraction/consolidation LLM call per message, so an expensive default
    silently burns credits whenever the config is reset or clobbered. Do
    not default this to a frontier/expensive model.
    """

    provider: str = "custom"
    # gpt-5-nano is ~10x cheaper than gpt-5-mini and still produces
    # reliable json_mode structured output for cognee's extraction tasks.
    # Earlier defaults were expensive (claude-haiku-4.5 — ~$20/day,
    # gpt-5-mini — ~$15/week in production) or unreliable (deepseek-v4-flash
    # produced malformed JSON under instructor, causing multi-minute retry
    # stalls, 2026-07-09).
    model: str = "openrouter/openai/gpt-5-nano"
    endpoint: str = "https://openrouter.ai/api/v1"
    api_key: SecretStr = SecretStr("")
    max_completion_tokens: int = 1024
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
        background_recall_enabled: When ``True`` (default), background agents
            (subsessions and the autonomous loop) may READ memory even when
            their write gate is off — they get recall plus the
            ``search_memory`` tool, but ``remember`` is a no-op.  This is the
            setting that makes the read/write asymmetry usable: recall is a
            retrieval-only lookup (~0.4 s warm, no LLM call) while cognify is
            a multi-minute LLM pipeline, so there is no reason to deny
            background agents the accumulated context just because they must
            not pay to write it back.  Set ``False`` to restore the previous
            all-or-nothing behaviour.
        subsession_enabled: When ``False`` (default), subsession agents
            (task / periodic / user_chat workers) get a ``NullMemory`` — they
            neither recall nor cognify.  ``enabled`` alone only gates the
            interactive main-chat agent; these background agents run
            continuously (periodic subsessions fire on a timer with no user
            present) and each turn otherwise pays a recall + a full cognify
            extraction pipeline, so cognee cost accrues 24/7.  Turn this on
            only if background agents genuinely need cross-run memory.
        autonomous_enabled: When ``False`` (default), the autonomous
            auto-continue agent gets a ``NullMemory`` for the same reason —
            auto-continue turns run unattended and would otherwise cognify
            every turn.  Independent of ``subsession_enabled`` so the two
            background classes can be gated separately.
        data_dir: Directory for cognee's stores (relative to the working dir).
            Put it under the persistent ``.data`` mount so memory survives
            container redeploys.
        recall_search_type: cognee ``SearchType`` name used for the AUTOMATIC
            per-message recall.  Default ``CHUNKS`` — pure retrieval, no LLM
            call, so every chat turn stays cheap and fast.  The previous
            default ``GRAPH_COMPLETION`` ran an LLM completion over the graph
            on EVERY message; live it added an LLM hop per turn and timed out
            (90 s) eight times in one observed day, each time stalling the
            reply and then proceeding memory-less anyway.  Deep, LLM-mediated
            search is still available on demand — see
            ``deep_recall_search_type`` and the ``search_memory`` tool.
        recall_timeout_seconds: Hard timeout (seconds) for a single automatic
            ``recall`` call.  On expiry the recall degrades to ``""`` — the
            agent proceeds without memory.  Default 60 s.
        recall_max_concurrency: How many recalls may run inside cognee at
            once; further callers queue.  Bounded because cognee serialises
            internally on its SQLite metadata store: letting every caller in
            at once does not make them finish sooner, it makes them all miss
            the deadline together.  Observed in production as thundering
            herds — 15 recalls issued within seconds of boot all expired at
            the same instant, while every recall that ran uncontended
            returned in 0.5-1.3 s.  Default 4.
        deep_recall_search_type: cognee ``SearchType`` for the on-demand
            ``search_memory`` tool.  Default ``GRAPH_COMPLETION`` — the
            expensive, LLM-mediated graph search, now paid only when the
            agent deliberately asks for it instead of on every turn.
        deep_recall_timeout_seconds: Hard timeout (seconds) for one
            ``search_memory`` tool call.  More generous than the automatic
            recall's — the tool is invoked deliberately, so waiting longer is
            acceptable where stalling every message was not.  Default 180 s.
        remember_timeout_seconds: Hard timeout (seconds) for ONE ``remember``
            attempt (cognify consolidation).  Default 900 s — raised from 300
            after 20 consecutive timeouts in one afternoon: cognify is a
            multi-minute LLM pipeline contending with recall for cognee's
            stores, and 300 s simply was not enough for it to finish.
        remember_max_attempts: How many times a write is attempted before the
            exchange is parked in the backlog.  Default 3.  Each attempt gets
            the full ``remember_timeout_seconds``; failures back off
            exponentially from ``remember_retry_backoff_seconds``.
        remember_retry_backoff_seconds: Base delay before retrying a failed
            write, doubling per attempt.  Backoff matters more than the retry
            count here — an immediate retry re-enters the same store
            contention that caused the failure.  Default 30 s.
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
            after which an ``ERROR`` diagnostic is emitted and memory is flagged
            ``degraded`` on ``GET /health`` — so a silently frozen vector store
            cannot go unnoticed.  Default ``10.0``.
        auto_recovery_enabled: When ``True`` (default), a freeze that persists
            past ``frozen_store_recovery_minutes`` triggers a guarded self-restart
            (via ``lifecycle.self_restart`` →
            ``POST /chat/services/{name}/restart``) — the proven remedy for the
            orphaned LanceDB/sqlite lock — instead of staying frozen until a
            human restarts the container.  Requires ``lifecycle.enabled`` **and**
            ``lifecycle.service_name`` (the self-restart transport); otherwise
            recovery is skipped and the freeze is only surfaced.
        frozen_store_recovery_minutes: Freeze duration (minutes) after which
            auto-recovery self-restart is attempted.  Should be greater than
            ``frozen_store_alert_minutes`` so the store is surfaced as degraded
            before a restart is attempted.  Default ``15.0``.
        recovery_cooldown_minutes: Minimum interval (minutes) between two
            auto-recovery self-restart attempts — a loop guard so a store that
            re-freezes immediately after a restart cannot restart-loop.
            Default ``30.0``.
        write_throttle_seconds: Delay (seconds) between serialised writes so
            the LanceDB worker subprocess can complete its ``merge_insert``
            before the next write starts.  Prevents a burst of many concurrent
            writes from collectively exhausting the worker's memory.
            Default ``0.5``.
        llm: Extraction-LLM config (graph building / consolidation).
        embedding: Embedding-server config (semantic search).
        langfuse_project: Name of the Langfuse project cognee's own LLM
            traffic traces to — looked up in the top-level ``langfuse``
            block, which is where the credentials live.  Separate from the
            main chat project by the one-project-per-function rule; when
            that project is absent or half-configured, cognee LLM calls are
            not traced.

    """

    enabled: bool = False
    background_recall_enabled: bool = True
    subsession_enabled: bool = False
    autonomous_enabled: bool = False
    data_dir: str = "/data/cognee"
    recall_search_type: str = "CHUNKS"
    recall_timeout_seconds: float = 60.0
    recall_max_concurrency: int = 4
    deep_recall_search_type: str = "GRAPH_COMPLETION"
    deep_recall_timeout_seconds: float = 180.0
    remember_timeout_seconds: float = 900.0
    remember_max_attempts: int = 3
    remember_retry_backoff_seconds: float = 30.0
    write_backlog_path: str = "/data/cognee/backlog.jsonl"
    datafusion_runtime_memory_limit: str = "256M"
    frozen_store_alert_minutes: float = 10.0
    auto_recovery_enabled: bool = True
    frozen_store_recovery_minutes: float = 15.0
    recovery_cooldown_minutes: float = 30.0
    write_throttle_seconds: float = 0.5
    llm: MemoryLlmSettings = Field(default_factory=MemoryLlmSettings)
    embedding: MemoryEmbeddingSettings = Field(default_factory=MemoryEmbeddingSettings)
    langfuse_project: str = PROJECT_MEMORY
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_langfuse_block(cls, data: Any) -> Any:
        """Strip the removed ``memory.langfuse`` credential sub-block.

        Deployed configs written before the migration still carry it; with
        ``extra="forbid"`` that would reject the file outright and crash-loop
        the container on the first start after an image upgrade.  The
        credentials are not migrated — the memory project's keys now live in
        the top-level block under ``memory.langfuse_project``'s name.
        """
        if isinstance(data, dict) and "langfuse" in data:
            data = {k: v for k, v in data.items() if k != "langfuse"}
            _logger.warning(
                "Ignoring legacy memory.langfuse — the memory project's "
                "credentials now live in the top-level langfuse.projects "
                "block, keyed by memory.langfuse_project"
            )
        return data


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
        base_url: Overridable base URL for GitHub Enterprise.
        timeout: Per-request HTTP timeout in seconds.

    Authentication reuses the ``direct_repo`` GitHub App credentials when
    they are configured; without them only public repositories are reachable.

    """

    enabled: bool = False
    repos: list[str] = Field(default_factory=list)
    ref: str = "main"
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
        base_url: Overridable base URL for GitHub Enterprise.
        timeout: Per-request HTTP timeout in seconds.
        cache_ttl: Seconds to cache the latest-release lookup (monotonic clock).

    Authentication reuses the ``direct_repo`` GitHub App credentials when
    they are configured; without them the lookup is unauthenticated (subject
    to lower rate limits).

    Note: the check is only meaningful when releases bump
    ``robotsix_chat.__version__`` in lockstep with the GitHub release tag.

    """

    enabled: bool = False
    repo: str = ""
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
    - ``merge_direct_repo_pr`` can merge an approved, mergeable PR when the ticket is
      in BLOCKED state — do not merge before the human gate is satisfied.

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
    board_api_base_url: str = "http://mill:8077"
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

    enabled: bool = True
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
        max_idle_runs: A periodic subsession auto-pauses (closes with
            reason ``"paused"``) after this many consecutive
            ``NO_CHANGE`` runs.  Set to ``0`` to disable.
            Default ``3``.
            Env override: ``SUBSESSIONS_MAX_IDLE_RUNS``.
        human_approval_timeout_runs: When a periodic subsession's checkpoint
            indicates the monitored ticket is in ``human_issue_approval``
            state, auto-escalate (close with reason
            ``human_approval_timeout``) after this many consecutive
            ``NO_CHANGE`` runs.  Default ``5``.
            Env override: ``SUBSESSIONS_HUMAN_APPROVAL_TIMEOUT_RUNS``.
        human_approval_timeout_seconds: Wall-clock backstop for the
            ``human_issue_approval`` stuck-ticket gate.  When the checkpoint
            has carried ``last_known_state='human_issue_approval'`` for
            longer than this many seconds, auto-escalate (close with reason
            ``human_approval_timeout``) even if the ``NO_CHANGE`` run count
            has not yet reached ``human_approval_timeout_runs``.  Default
            ``300`` (5 minutes).
            Env override: ``SUBSESSIONS_HUMAN_APPROVAL_TIMEOUT_SECONDS``.
        pre_authorized_ticket_patterns: Glob patterns (``fnmatch``) matching
            ticket IDs that are pre-authorized under a standing operator
            directive.  When a monitored ticket's ID matches any pattern,
            the ``human_issue_approval`` gate is bypassed — the system
            auto-escalates immediately (reason ``pre_authorized_approval``)
            instead of waiting for ``human_approval_timeout_runs``.
            Default ``[]``.
        run_timeout_seconds: Hard per-run timeout for a single subsession
            agent turn (recall + LLM call + delivery).  On expiry the run
            is marked failed and the schedule continues instead of staying
            ``running`` forever.  Default 600 s.
            Env override: ``SUBSESSIONS_RUN_TIMEOUT_SECONDS``.
        store_path: JSON persistence file (periodic subsessions resume
            across restarts).  Env override: ``SUBSESSIONS_STORE_PATH``.
        transcript_max_entries: Per-subsession transcript retention cap.
            Env override: ``SUBSESSIONS_TRANSCRIPT_MAX_ENTRIES``.
        mill_recovery_initial_backoff_seconds: Initial backoff (seconds)
            when a ticket monitor enters mill-recovery mode after
            consecutive failures.  Doubles on each retry up to
            *mill_recovery_max_backoff_seconds*.  Default ``60.0``.
            Env override: ``SUBSESSIONS_MILL_RECOVERY_INITIAL_BACKOFF_SECONDS``.
        mill_recovery_max_backoff_seconds: Maximum backoff (seconds) for
            mill-recovery retries.  Default ``3600.0`` (1 hour).
            Env override: ``SUBSESSIONS_MILL_RECOVERY_MAX_BACKOFF_SECONDS``.
        mill_recovery_max_retries: Maximum number of recovery retries
            before the subsession is permanently closed.  Default ``10``.
            Env override: ``SUBSESSIONS_MILL_RECOVERY_MAX_RETRIES``.
        periodic_max_interval_seconds: Upper bound (seconds) for a
            periodic subsession's self-adjusted interval.  The
            ``adjust_periodic_interval`` tool clamps to this value.
            Default ``3600.0`` (1 hour).
            Env override: ``SUBSESSIONS_PERIODIC_MAX_INTERVAL_SECONDS``.
        periodic_max_total_runs: Upper bound for a periodic subsession's
            self-adjusted ``max_runs`` (total run budget).  The
            ``adjust_periodic_budget`` tool clamps to this value.
            Default ``100``.
            Env override: ``SUBSESSIONS_PERIODIC_MAX_TOTAL_RUNS``.
        user_chat_max_retries: Maximum number of automatic retries for
            ``user_chat`` and ``task`` subsession failures.  Each retry
            re-launches the subsession with the prior error folded into
            the prompt so the agent can self-correct.  Once exhausted the
            subsession is failed and, for ``user_chat``, the original
            decision prompt is surfaced in the main conversation as a
            fallback so the operator can answer directly.  Default ``3``.
            Env override: ``SUBSESSIONS_USER_CHAT_MAX_RETRIES``.
        transient_error_max_retries: Maximum retry attempts when a
            periodic subsession's agent turn fails with a transient API
            error (e.g. upstream provider hiccup).  Retries use
            exponential backoff between *transient_error_backoff_base*
            and *transient_error_backoff_cap*.  When retries are
            exhausted the run is skipped and the schedule continues
            rather than permanently failing the subsession.
            Default ``3``.
            Env override: ``SUBSESSIONS_TRANSIENT_ERROR_MAX_RETRIES``.
        transient_error_backoff_base: Initial backoff in seconds for
            transient-error retries (doubles each attempt).
            Default ``1.0``.
            Env override: ``SUBSESSIONS_TRANSIENT_ERROR_BACKOFF_BASE``.
        transient_error_backoff_cap: Maximum backoff in seconds for
            transient-error retries.  Default ``30.0``.
            Env override: ``SUBSESSIONS_TRANSIENT_ERROR_BACKOFF_CAP``.

    """

    max_concurrent: int = 8
    max_depth: int = 3
    default_model_level: int = 2
    min_interval_seconds: float = 60.0
    auto_stop_no_change_runs: int = 3
    max_idle_runs: int = 3
    human_approval_timeout_runs: int = 5
    human_approval_timeout_seconds: float = 300.0
    pre_authorized_ticket_patterns: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns (fnmatch) matching ticket IDs that are "
            "pre-authorized under a standing operator directive.  When a "
            "monitored ticket's ID matches any pattern, the "
            "human_issue_approval gate is bypassed — the system "
            "auto-escalates immediately (reason 'pre_authorized_approval') "
            "instead of waiting for human_approval_timeout_runs."
        ),
    )
    run_timeout_seconds: float = 600.0
    store_path: str = "/data/subsessions.json"
    transcript_max_entries: int = 200
    mill_recovery_initial_backoff_seconds: float = 60.0
    mill_recovery_max_backoff_seconds: float = 3600.0
    mill_recovery_max_retries: int = 10
    paused_monitor_poll_interval_seconds: float = Field(
        default=60.0,
        description=(
            "Interval (seconds) between polls of paused periodic "
            "monitors.  The background watcher checks each paused "
            "monitor's ticket state via the mill API; when the "
            "ticket's state differs from the checkpoint's "
            "``last_known_state`` the monitor is reopened and "
            "re-spawned.  Set to ``0`` to disable runtime polling "
            "(paused monitors only resume on service restart)."
        ),
    )
    paused_monitor_long_poll_interval_seconds: float = Field(
        default=15.0,
        description=(
            "Interval (seconds) between direct mill API polls by a "
            "paused periodic monitor in its wait loop.  Each paused "
            "monitor polls the mill for its tracked ticket's state "
            "at this interval; when the state differs from the "
            "checkpoint's ``last_known_state`` the monitor resumes "
            "immediately (zero added latency).  The background "
            "watcher's ``paused_monitor_poll_interval_seconds`` "
            "(60 s default) serves as a safety-net backup.  Set to "
            "``0`` to disable per-monitor long-polling (watcher-only "
            "wake)."
        ),
    )
    paused_monitor_auto_resume_seconds: float = Field(
        default=1800.0,
        description=(
            "Maximum wall-clock seconds a paused periodic monitor "
            "remains paused before auto-resuming regardless of "
            "ticket-state changes.  When a monitor has been paused "
            "for longer than this interval (e.g. 1800 s = 30 min), "
            "it resumes its normal periodic cycle so the operator "
            "does not need to manually intervene.  Set to ``0`` to "
            "disable time-based auto-resume (monitor stays paused "
            "until a state change or manual message arrives)."
        ),
    )
    paused_monitor_max_reblock_resumes: int = Field(
        default=3,
        description=(
            "Maximum number of consecutive BLOCKED-on-resume events "
            "before a paused periodic monitor is closed with reason "
            "``repeated_blocked``.  When a ticket is BLOCKED on every "
            "resume (the agent keeps hitting the same failure without "
            "making progress), auto-retry is futile — the monitor is "
            "closed so the operator can intervene.  Default ``3``."
        ),
    )
    paused_monitor_reblock_notify_threshold: int = Field(
        default=2,
        description=(
            "Number of consecutive BLOCKED-on-resume events before an "
            "SSE notification is sent to the parent conversation "
            "alerting the operator that the monitor is re-blocking.  "
            "This surfaces silent auto-resume→re-block loops so the "
            "operator can decide whether to rebase the branch, revert "
            "problematic files, or take other action before the "
            "``paused_monitor_max_reblock_resumes`` cap is reached.  "
            "Set to ``0`` to disable notifications.  Default ``2``."
        ),
    )
    event_driven_timeout_seconds: float = Field(
        default=900.0,
        description=(
            "Default timeout (seconds) for wait-for-event subsessions. "
            "When no matching mill event arrives within this window, the "
            "monitor runs a safety-net turn to verify state via the board "
            "API in case an event was lost, then re-arms the wait."
        ),
    )
    periodic_max_interval_seconds: float = Field(
        default=3600.0,
        description=(
            "Upper bound (seconds) for a periodic subsession's "
            "self-adjusted interval.  The adjust_periodic_interval tool "
            "clamps to this value.  Default 3600 (1 hour)."
        ),
    )
    periodic_max_total_runs: int = Field(
        default=100,
        description=(
            "Upper bound for a periodic subsession's self-adjusted "
            "max_runs (total run budget).  The adjust_periodic_budget "
            "tool clamps to this value.  Default 100."
        ),
    )
    user_chat_max_retries: int = 3
    transient_error_max_retries: int = 3
    transient_error_backoff_base: float = 1.0
    transient_error_backoff_cap: float = 30.0
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
    """Deploy-lifecycle API access for the agent.

    When enabled, the chat agent gains tools to inspect the
    central-deploy lifecycle server: list services, check service status
    and health, read configuration and environment (with secrets
    already masked as ``***`` server-side by ``_mask_secrets``), and
    restart services.

    Attributes:
        enabled: Master switch.  When ``False``, no lifecycle tools are
            offered.
        base_url: Base URL of the deploy-lifecycle API server (no trailing
            slash), e.g. ``http://central-deploy:8100``.
        api_key: API key sent as the ``X-API-Key`` header.
        service_name: This service's own name as registered with the deploy
            server (e.g. ``"chat"``).  Required for ``self_restart`` — the
            deploy server has no bare ``/self/restart`` route, so a service
            restarts itself by naming itself at
            ``POST /chat/services/{service_name}/restart``.  When empty,
            ``self_restart`` (and the cognee frozen-store auto-recovery that
            depends on it) is unavailable.
        timeout: Per-request HTTP timeout in seconds.

    """

    enabled: bool = False
    base_url: str = ""
    default_protocol: str = "http"
    api_key: SecretStr = SecretStr("")
    service_name: str = ""
    timeout: float = 30.0
    self_restart_max_retries: int = 3
    self_restart_backoff_base: float = 1.0
    self_restart_backoff_cap: float = 30.0
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

    Note: GitHub App authentication is delegated to
    :class:`DirectRepoSettings` — those credentials must also be configured
    for the tool to function.

    """

    enabled: bool = False
    github_org: str = "damien-robotsix"
    deploy_api_key: SecretStr = SecretStr("")
    model_config = ConfigDict(extra="forbid")


class GitHubActionsSettings(BaseModel):
    """GitHub Actions secrets and workflow dispatch via the GitHub App installation.

    When enabled, the chat agent gains ``set_actions_secret`` and
    ``dispatch_workflow`` tools that can create/update repository Actions
    secrets and trigger ``workflow_dispatch`` events on repos under the
    configured GitHub App's installation scope.

    **Guardrails built into the tools (not configurable):**
    - Repo scope is resolved dynamically from the GitHub App installation
      (list-installation-repositories) — no static allowlist.
    - Only repos within the installation scope are modifiable.
    - Secret encryption uses libsodium sealed-box (requires ``pynacl``).
    - Both tools are confirmation-gated: the agent must confirm the exact
      repo, secret name (or workflow id + ref) with the user before calling.

    Attributes:
        enabled: Master switch.  When ``False``, no Actions tools are offered.
        github_org: GitHub organisation name whose repos are in scope
            (e.g. ``"damien-robotsix"``).
        deploy_api_key: API key that clients must present in the
            ``X-API-Key`` header when calling the Actions endpoints.
            When empty, the endpoints return 503 (unconfigured).

    Note: GitHub App authentication is delegated to
    :class:`DirectRepoSettings` — those credentials must also be configured
    for the tools to function.

    """

    enabled: bool = False
    github_org: str = "damien-robotsix"
    deploy_api_key: SecretStr = SecretStr("")
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

    enabled: bool = True
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
        deploy_api_key: Bearer / X-API-Key token for the central-deploy
            roster endpoint (``GET /chat/components``). Required when
            the feedback runner needs to resolve allowed repos via the
            deploy roster.
        timeout: Per-request HTTP timeout in seconds for ingest calls.
            The set of allowed target repos is resolved dynamically at
            run-time from the deploy server's chat-component roster
            intersected with the mill board's repo registry — no static
            allowlist is needed.
        max_tickets_per_run: Ceiling on tickets filed by one feedback run.
            A run fires at every compaction and session-end boundary, and
            was previously unbounded: across 37 observed runs it filed 114
            tickets, mean 3.08, peaking at 9 from a single run. Excess
            tickets are dropped with a warning naming each one. ``0``
            disables filing while leaving analysis on.

    """

    enabled: bool = False
    model_level: int = 1
    board_url: str = ""
    board_api_token: SecretStr = SecretStr("")
    deploy_api_key: SecretStr = SecretStr("")
    timeout: float = 60.0
    max_tickets_per_run: int = 3
    model_config = ConfigDict(extra="forbid")


class RenderUrlSettings(BaseModel):
    """Read-only URL rendering with headless Chromium (Playwright).

    When enabled, the agent gains a tool that loads a URL in a headless
    Chromium browser (via Playwright), takes a full-page screenshot, and
    extracts the ARIA accessibility tree — both returned as structured output.
    No interactive browsing, form-filling, or navigation beyond the initial
    page load is permitted.

    Attributes:
        enabled: Master switch.  When ``False``, no URL-render tool is offered.
        timeout: Per-request timeout in seconds for the page load.
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.

    """

    enabled: bool = True
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

    enabled: bool = True
    timeout: float = 10.0
    allowlist: list[str] = Field(
        default_factory=lambda: ["www.robotsix.net", "robotsix.net"]
    )
    max_body_bytes: int = 2048
    max_redirects: int = 5
    model_config = ConfigDict(extra="forbid")


class DockerDigestSettings(BaseModel):
    """Read-only Docker digest resolution tool for the agent.

    When enabled, the agent gains a ``resolve_docker_digest`` tool that
    resolves a Docker image reference (e.g. ``python:3.14-slim``) and
    target platform to its immutable ``sha256:...`` content digest by
    querying the Docker Registry v2 HTTP API.

    Attributes:
        enabled: Master switch.  When ``False``, no docker_digest tool is offered.
        timeout: Per-request HTTP timeout in seconds (default 30 s).
        registry_host: Docker Registry v2 hostname for manifest lookups.
            Default ``registry-1.docker.io`` (Docker Hub).
        auth_url: Token-authentication endpoint for bearer tokens.
            Default ``https://auth.docker.io/token`` (Docker Hub's auth
            service).

    """

    enabled: bool = True
    timeout: float = 30.0
    registry_host: str = "registry-1.docker.io"
    auth_url: str = "https://auth.docker.io/token"
    model_config = ConfigDict(extra="forbid")


class PublicFetchSettings(BaseModel):
    """Scoped public-repo-fetch tool for the chat agent.

    When enabled, the agent gains a ``fetch_public_url`` tool that performs
    a plain HTTP(S) GET to a user-provided public URL, returns the raw
    text/file contents with metadata, and writes an audit-log entry per
    fetch.  SSRF protection blocks internal/private IP ranges for public
    hosts.  Fleet components, resolved from the central-deploy roster, are
    trusted by the operator: they bypass the SSRF check and the domain
    allowlist, and their requests carry server-injected basic auth.

    Attributes:
        enabled: Master switch.  When ``False``, no tool is offered.
        timeout: Per-request HTTP timeout in seconds (default 10 s).
        max_body_bytes: Maximum bytes of the response body to read and
            return to the agent (default 1_048_576 — ~1 MB).
        max_redirects: Maximum number of redirects to follow (default 5).
        domain_allowlist: Optional list of hostnames (no protocol, no
            path) that the tool is permitted to fetch.  When empty, any
            public hostname is allowed (subject to SSRF checks).
        rate_limit_requests: Maximum number of requests allowed within
            ``rate_limit_window_seconds`` (default 10).
        rate_limit_window_seconds: Sliding window in seconds for the
            rate limiter (default 60.0).

    """

    enabled: bool = False
    timeout: float = 10.0
    max_body_bytes: int = 1_048_576
    max_redirects: int = 5
    domain_allowlist: list[str] = Field(default_factory=list)
    rate_limit_requests: int = 10
    rate_limit_window_seconds: float = 60.0
    model_config = ConfigDict(extra="forbid")


class TriggerType(enum.StrEnum):
    """How an autonomous session is re-triggered after completion."""

    periodic = "periodic"
    """Wait ``trigger_interval_seconds``, then restart."""

    on_close = "on_close"
    """Restart immediately when the previous run completes (continuous mode)."""


class AutonomousSessionDefinition(BaseModel):
    """Definition of one named autonomous session.

    Each definition maps to one autonomous session owner (``autonomous:<name>``
    when the preset name is not ``"default"``, otherwise the bare
    ``autonomous`` pseudo-owner).  The runner respects per-definition prompts,
    trigger type, and the enabled flag independently.

    Attributes:
        name: Unique identifier for this session definition.
        prompt: Custom kickoff prompt appended to the autonomous protocol
            supplement.  When empty, the agent uses the standard "Pick a
            subject and draft a plan" prompt.
        trigger_type: How the session is re-triggered after completion —
            ``"periodic"`` (wait ``trigger_interval_seconds``) or
            ``"on_close"`` (restart immediately, continuous mode).
        trigger_interval_seconds: Delay between completion and restart for
            ``periodic`` trigger.  Ignored for ``on_close``.  Default 45 s.
        enabled: When ``False``, the definition is skipped — no session is
            created for it.
        self_refine: When ``True``, after each run completes an LLM
            refinement step proposes an updated prompt addendum that folds
            in the run's feedback.  The next run uses the refined prompt.
            Default ``False`` (static presets keep running verbatim).
        self_refine_require_approval: When ``True``, refinements enter
            ``pending`` state and require operator approval before they
            take effect.  When ``False``, refinements are auto-accepted.
            Default ``False``.

    """

    name: str
    prompt: str = ""
    trigger_type: TriggerType = TriggerType.periodic
    trigger_interval_seconds: float = Field(default=45.0, ge=0.0)
    max_auto_turns: int = Field(
        default=20,
        description=(
            "Maximum number of automatic agent turns during the "
            "execution phase before reverting to proposal."
        ),
    )
    enabled: bool = True
    self_refine: bool = False
    self_refine_require_approval: bool = False
    model_config = ConfigDict(extra="forbid")


class AutonomousSettings(BaseModel):
    """Native autonomous chat sessions — self-directed agent loops.

    Autonomous sessions are defined entirely through the ``sessions`` presets
    list.  Each preset carries its own prompt, trigger type, max turns, and
    enabled flag — there are no legacy single-session keys.

    The built-in default preset ``{"name": "default"}`` ships in the schema
    defaults (the field default) and in the committed config template so it
    is always visible in the UI.  The runner reads only the configured
    presets list — there is no hidden or implicit fallback session.

    Attributes:
        proposal_marker: Marker string the agent emits after drafting a plan
            to signal the plan is ready for operator review.  The session
            enters the ``proposal`` state and waits for the operator to
            comment before beginning execution.
        completion_marker: Marker string the agent emits when the plan is
            complete.  The session stays open after completion; the operator
            must explicitly close it.
        continue_interval_seconds: Minimum delay between auto-continue cycles
            (throttle).  Also serves as the default trigger interval for
            synthesized sessions when no presets are configured.
        max_idle_auto_turns: Maximum number of consecutive NO_CHANGE / idle
            auto-continue turns before the loop halts (reverts to proposal).
        stale_monitor_runs_before_completion: Number of consecutive NO_CHANGE
            cycles after which a periodic monitor is considered 'stale'.
        sessions: List of named autonomous session definitions.  When
            explicitly cleared, no autonomous sessions run — presets are the
            sole enablement model.  Each entry defines a prompt, trigger, max
            turns, and enabled flag for one autonomous session.

    """

    proposal_marker: str = "---PROPOSAL READY---"
    completion_marker: str = "---AUTONOMOUS COMPLETE---"
    continue_interval_seconds: float = 45.0
    max_idle_auto_turns: int = Field(
        default=5,
        description=(
            "Maximum number of consecutive NO_CHANGE / idle auto-continue "
            "turns before the loop halts (reverts to proposal).  A turn is "
            "idle when the agent reply is a recognised no-op sentinel "
            "(NO_CHANGE, nothing changed, …).  Set to 0 to disable the "
            "idle cap and only rely on per-preset max_auto_turns."
        ),
    )
    stale_monitor_runs_before_completion: int = Field(
        default=3,
        description=(
            "Number of consecutive NO_CHANGE cycles after which a periodic "
            "monitor is considered 'stale' — the agent may declare the "
            "autonomous session complete even while the monitor is still "
            "running.  Monitors continue in the background.  "
            "Env override: ``AUTONOMOUS_STALE_MONITOR_RUNS_BEFORE_COMPLETION``."
        ),
    )
    sessions: list[AutonomousSessionDefinition] = Field(
        default_factory=lambda: [AutonomousSessionDefinition(name="default")],
        description=(
            "Named autonomous session definitions.  The built-in default "
            'preset ``{"name": "default"}`` ships in the schema defaults '
            "and in the committed config template so it is always visible.  "
            "When the list is explicitly cleared, no autonomous sessions run "
            "— presets are the sole enablement model.  Each entry defines a "
            "prompt, trigger, max turns, and enabled flag for one autonomous "
            "session."
        ),
    )
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_autonomous_keys(cls, data: Any) -> Any:
        """Strip removed single-session keys and relocate ``max_auto_turns``.

        Legacy keys ``enabled``, ``initial_task``, ``session_color``,
        ``persist_path``, and ``pending_subsession_wait_timeout`` are
        stripped silently — they have no equivalent in the preset model.

        The built-in default preset (``{"name": "default"}``) is now carried
        in the ``sessions`` field default (schema default), not injected here.
        Existing deployments that lack a ``sessions`` key receive the default
        from the field default; deployments that explicitly clear the list
        run no autonomous sessions.

        The global ``max_auto_turns`` value is migrated into every session
        preset that does not already define its own ``max_auto_turns``,
        then the global key is removed.
        """
        if not isinstance(data, dict):
            return data

        _stripped_keys = (
            "enabled",
            "initial_task",
            "session_color",
            "persist_path",
            "pending_subsession_wait_timeout",
        )
        for key in _stripped_keys:
            data.pop(key, None)

        # Migrate global max_auto_turns into each preset that lacks it.
        legacy_max_turns = data.pop("max_auto_turns", None)
        if legacy_max_turns is not None and isinstance(data.get("sessions"), list):
            for preset in data["sessions"]:
                if isinstance(preset, dict) and "max_auto_turns" not in preset:
                    preset["max_auto_turns"] = legacy_max_turns

        return data


class AutonomySettings(BaseModel):
    """Operator-configurable autonomy tier for reducing interruptions.

    When enabled, the chat agent can self-authorize certain low-risk
    actions that would normally require operator approval.  The default
    is conservative — every action is gated so behaviour only changes
    when the operator explicitly opts in.

    Even at the highest tier these actions remain HARD-GATED:
    merges touching ``.github/workflows/**``, ``secrets/**``, ``.env*``
    or any security-sensitive path; deletions of tracked files or
    directories; priority/scope changes with broad blast radius;
    ambiguous or novel mutation types; and any action whose safety the
    agent cannot independently verify.

    Attributes:
        auto_approve_self_authored: When ``True``, the agent may
            auto-approve ``human_issue_approval`` tickets that it
            (or a chat-agent feedback source) authored, provided the
            target repo is in ``auto_approve_repo_allowlist`` and the
            change is non-destructive / reversible.
        auto_approve_repo_allowlist: Repository names (e.g.
            ``"robotsix-chat"``) eligible for auto-approval when
            ``auto_approve_self_authored`` is enabled.  Tickets
            targeting repos not listed here are always gated.
        suppress_no_change_monitors: When ``True``, periodic and event
            monitor outcomes that carry no actionable delta
            (NO_CHANGE, completed normally, auto-paused) do not
            generate an operator-facing turn.  Only blockers, decisions
            that fail auto-approval criteria, and terminal failures
            are surfaced.

    """

    model_config = ConfigDict(extra="forbid")

    auto_approve_self_authored: bool = Field(
        default=False,
        description=(
            "When True, auto-approve self-authored human_issue_approval "
            "tickets for repos in the allowlist."
        ),
    )
    auto_approve_repo_allowlist: list[str] = Field(
        default_factory=list,
        description=(
            "Repos eligible for auto-approval when "
            "auto_approve_self_authored is enabled."
        ),
    )
    suppress_no_change_monitors: bool = Field(
        default=False,
        description=(
            "When True, suppress operator-facing turns for monitor "
            "outcomes with no actionable delta."
        ),
    )


class ComponentCredentials(BaseModel):
    """Stored credentials for a single roster component.

    Keys are component IDs matching the ``id`` field returned by the
    central-deploy ``GET /chat/components`` roster. Each entry carries
    credentials for all supported auth schemes; the roster entry's ``auth.type``
    selects which fields are used.

    Attributes:
        basic_auth_username: Username for HTTP Basic authentication.
        basic_auth_password: Password for HTTP Basic authentication.
        header_token: Token value for header-based authentication
            (e.g. ``X-API-Key``).

    """

    basic_auth_username: SecretStr = SecretStr("")
    basic_auth_password: SecretStr = SecretStr("")
    header_token: SecretStr = SecretStr("")
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
        component_credentials: Per-component credentials keyed by
            component id.  Each entry carries credentials for all
            supported auth schemes; the roster entry's ``auth.type``
            selects which fields are used.

    """

    model_config = ConfigDict(extra="forbid")

    url: str = ""
    api_token: SecretStr = SecretStr("")
    roster_cache_ttl: float = 300.0
    component_response_max_chars: int = 200_000
    component_credentials: dict[str, ComponentCredentials] = Field(default_factory=dict)
    component_fallbacks: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Baked-in fallback base URLs for components that may be missing "
            "from the central-deploy roster (e.g. after a redeploy). "
            'Keyed by component id (e.g. "robotsix-mill"). When the roster '
            "returned by central-deploy is missing a component, the fallback "
            "URL is used instead. This keeps monitors running through "
            "transient roster gaps."
        ),
    )


class SftpSettings(BaseModel):
    """SFTP config-restore settings.

    Provides credentials and connection parameters for the SFTP config-restore
    capability.  When enabled, the agent gains tools to read, list, and
    (confirmation-gated) write files on a remote SFTP server — used to
    restore known-good configuration files when diagnostics detect they are
    missing.

    Attributes:
        enabled: Master switch.  When ``False`` (default), no SFTP tools
            are registered and the agent runs exactly as before.
        host: SFTP server hostname or IP address.
        port: SFTP server port (default 22).
        username: SFTP username for authentication.
        password: Password for password-based authentication.  Leave empty
            when using key-based auth.
        private_key: OpenSSH-format private key for key-based
            authentication.  Leave empty when using password auth.
        private_key_passphrase: Passphrase for *private_key*, if the key
            is encrypted.
        known_hosts: OpenSSH-format known-hosts entries (one or more lines)
            for host key verification.  When empty, host key verification
            is skipped (insecure — only suitable for isolated networks).
        remote_root: Optional base directory on the remote server to
            restrict all operations under (e.g. ``/var/www``).  When set,
            paths are resolved relative to this root and traversal outside
            it is refused.

    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    host: str = ""
    port: int = 22
    username: str = ""
    password: SecretStr = SecretStr("")
    private_key: SecretStr = SecretStr("")
    private_key_passphrase: SecretStr = SecretStr("")
    known_hosts: str = ""
    remote_root: str = ""
