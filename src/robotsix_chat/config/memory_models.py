"""Memory Settings Models."""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .langfuse_models import PROJECT_MEMORY

_logger = logging.getLogger(__name__)


class MemoryLlmSettings(BaseModel):
    """Extraction-LLM config for cognee memory (OpenRouter via litellm).

    Defaults match the validated robotsix setup: ``gpt-5-nano`` (cognee's
    cheapest model, reliable json_mode structured output) through
    OpenRouter's ``custom`` provider.  The extraction-LLM API key no longer
    lives here — it is read from the canonical top-level ``openrouter.keys``
    block under ``memory.langfuse_project``'s alias.

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
            exponentially via :func:`robotsix_http.acall_with_retry`.
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
        recall_failure_degrade_threshold: Number of consecutive recall failures
            with an *unrecognised* error before the backend is marked degraded.
            Recognised store faults degrade on the first failure; this covers
            everything else, so a novel failure mode surfaces instead of being
            mistaken for the benign empty-store case (which stops as soon as
            the first exchange is written).  Default ``3``.
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
        maintenance_enabled: When ``True`` (default), a background task
            periodically compacts and prunes every table in the cognee
            LanceDB store (LanceDB's ``Table.optimize`` — merge fragments and
            drop old versions).  Every ``cognify`` write appends a fragment,
            a version and deletion files but nothing ever compacts them, so a
            vector search ends up scanning thousands of tiny fragments and
            applying tens of thousands of deletion vectors — which starves
            recall and saturates the host disk.  The pass runs under the
            cognee write lock and processes tables sequentially, so it never
            overlaps a live write or exhausts memory on a badly-fragmented
            store.
        maintenance_interval_seconds: Seconds between maintenance passes; the
            first pass runs at startup.  Default ``21600.0`` (6 h).
        maintenance_version_retention_seconds: Age (seconds) below which
            LanceDB dataset versions are kept during pruning — passed as
            ``cleanup_older_than`` to ``Table.optimize``.  Older versions are
            removed so the on-disk version count stays bounded.  Default
            ``3600.0`` (1 h).
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
    write_backlog_path: str = "/data/cognee/backlog.jsonl"
    datafusion_runtime_memory_limit: str = "256M"
    frozen_store_alert_minutes: float = 10.0
    recall_failure_degrade_threshold: int = 3
    auto_recovery_enabled: bool = True
    frozen_store_recovery_minutes: float = 15.0
    recovery_cooldown_minutes: float = 30.0
    write_throttle_seconds: float = 0.5
    maintenance_enabled: bool = True
    maintenance_interval_seconds: float = 21600.0
    maintenance_version_retention_seconds: float = 3600.0
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

    @model_validator(mode="before")
    @classmethod
    def _drop_legacy_remember_retry_backoff(cls, data: Any) -> Any:
        """Strip the removed ``memory.remember_retry_backoff_seconds`` key.

        The hand-rolled retry loop was replaced by
        :func:`robotsix_http.acall_with_retry`; the backoff is now
        hard-coded in the ``RetryConfig`` passed to that function.
        Deployed configs written before the migration may still carry
        this key; with ``extra="forbid"`` it would crash the container.
        """
        if isinstance(data, dict) and "remember_retry_backoff_seconds" in data:
            data = {
                k: v for k, v in data.items() if k != "remember_retry_backoff_seconds"
            }
            _logger.warning(
                "Ignoring legacy memory.remember_retry_backoff_seconds — "
                "retry backoff is now managed by robotsix_http.acall_with_retry"
            )
        return data
