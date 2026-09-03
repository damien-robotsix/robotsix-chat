"""Cognee-backed :class:`~robotsix_chat.memory.base.ChatMemory`.

Wires the embedded `cognee` knowledge-graph memory to:

* an **OpenRouter** extraction LLM (``custom`` provider via litellm), and
* a remote **OpenAI-compatible embedding** server (self-hosted Ollama / ``bge-m3``).

Configuration is global to the cognee process, so it is applied exactly once
(guarded by :attr:`_setup_lock`). Every public method is wrapped so a
misconfigured or unreachable backend degrades to "no memory" rather than
breaking the chat reply.

cognee is imported lazily (it is a heavy optional dependency, the ``memory``
extra); this module is only imported at all when that extra is installed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from robotsix_http import RetryConfig, acall_with_retry

from robotsix_chat.memory.maintenance import (
    COGNEE_DB_RELATIVE_PATH,
    LANCEDB_RELATIVE_PATH,
    CogneeDbVacuumScheduler,
    LanceDbMaintenanceScheduler,
    StoreMetricsScheduler,
)

if TYPE_CHECKING:
    from robotsix_chat.config import (
        LangfuseSettings,
        MemorySettings,
        OpenRouterSettings,
    )
    from robotsix_chat.memory.base import NotifyCallback, RecoverCallback

_T = TypeVar("_T")

logger = logging.getLogger(__name__)

# Cap recalled context so a large graph can't blow up the prompt.
_MAX_RECALL_CHARS = 4000

# Throwaway query used to prime the read path on startup. Its result is
# discarded — only the lazy initialisation it forces matters.
_WARMUP_QUERY = "warm up"

# Fixed sample document for the ingestion-structure regression check.  It is
# intentionally entity- and relation-rich so a model/config change that thins
# extraction shows up as a drop in the counts returned by
# :meth:`CogneeMemory.ingest_structure_fixture`.
_INGESTION_FIXTURE_DOCUMENT = (
    "Alice Chen, a robotics engineer at Acme Robotics, leads the navigation "
    "team. Bob Patel, a data scientist at Acme Robotics, reports to Alice. "
    "Together they built the NavMesh localization pipeline in Q3 2025, which "
    "reduced docking failures by 40 percent."
)
_INGESTION_FIXTURE_DATASET = "ingestion_structure_check"

# First 16 bytes of every SQLite database file.
_SQLITE_MAGIC = b"SQLite format 3\x00"


# Signatures of the orphaned-lock freeze (sqlite relational store locked or the
# LanceDB worker subprocess wedged) — distinguished from the benign "empty
# store on the first-ever message" case so recall only reports *faults* as
# degraded, not an empty memory.
_LOCK_FREEZE_RE = re.compile(
    r"database is locked|OperationalError|LanceError|Deadlock|lock.*timed? ?out",
    re.IGNORECASE,
)


def _is_lock_freeze_error(exc: BaseException) -> bool:
    """Return True if *exc* looks like the orphaned-lock / frozen-store fault."""
    return bool(_LOCK_FREEZE_RE.search(str(exc)))


# Signatures of an *unopenable* store — the graph engine aborting during WAL
# replay, or refusing the file outright.  Distinct from _LOCK_FREEZE_RE, which
# means "busy, worth retrying": these mean the store is structurally unusable
# and every subsequent call will fail the same way until an operator
# intervenes.  Both mark the backend degraded; only the former drives retries.
_STORE_FAULT_RE = re.compile(
    r"UNREACHABLE_CODE|Assertion failed|wal_record|Could not set lock on file"
    r"|corrupt|malformed|not a valid database",
    re.IGNORECASE,
)


# Signature of the ladybug graph-store WAL corruption that auto-recovery
# acts on.  Observed twice (2026-08-26 and 2026-09-01) after interrupted deploys:
#     Assertion failed in ".../src/storage/wal/wal_record.cpp" line 76:
#     UNREACHABLE_CODE
# during WAL replay at open.  Narrower than _STORE_FAULT_RE on purpose: this is
# specifically the abort that warrants *moving the WAL sidecars aside* and
# reopening, whereas broader store faults ("corrupt", "malformed", lock
# errors) must NOT trigger the file-moving recovery without clear evidence.
_WAL_CORRUPTION_RE = re.compile(r"UNREACHABLE_CODE|wal_record", re.IGNORECASE)


def _is_wal_corruption_error(exc: BaseException) -> bool:
    """Return True if *exc* is the ladybug WAL-corruption abort signature."""
    return bool(_WAL_CORRUPTION_RE.search(str(exc)))


# Signature of a graph-store worker subprocess dying in *native* code while
# opening the store — the segfault (exit code -11 / SIGSEGV) observed
# 2026-09-03 when the ladybug WAL was left unreplayable after the main process
# died mid-ingest.  Unlike the WAL-replay abort (``_WAL_CORRUPTION_RE``), a
# segfault raises NO catchable assertion text: cognee's worker harness surfaces
# only "Subprocess exited unexpectedly (exit code -11)".  Matching that surface
# lets a sustained open-time crash-loop be recognised as a store fault instead
# of an unexplained recall-failure streak that never recovers or escalates.
_GRAPH_OPEN_SEGV_RE = re.compile(
    r"exit code -11\b|exited unexpectedly[^-\n]*-11\b|signal 11\b|SIGSEGV",
    re.IGNORECASE,
)

# Base filename of the ladybug graph store, next to its ``.wal`` sidecars under
# ``<data_dir>/system/databases``.
_LADYBUG_GRAPH_BASENAME = "cognee_graph_ladybug"

# How long to short-circuit recalls after a graph-open segfault crash-loop is
# detected, so the segfaulting worker is not respawned every turn (the tight
# ~2s respawn loop seen in the incident).  Recovery, when it succeeds, clears
# the window early.
_GRAPH_SEGV_BACKOFF_SECONDS = 300.0

# Wall-clock ceiling for the out-of-process graph-store open probe.
_GRAPH_PROBE_TIMEOUT_SECONDS = 60.0

# Child script for :func:`probe_graph_store_opens`.  Runs in a *separate*
# process because the open itself may segfault (the exact fault being
# diagnosed) — a segfault there takes down only the child (negative return
# code), never the chat server.  Exit 0 == clean open + count query.
_GRAPH_PROBE_SCRIPT = r"""
import sys

try:
    import ladybug
except Exception as exc:  # pragma: no cover - environment dependent
    sys.stderr.write("ladybug import failed: %r" % (exc,))
    sys.exit(4)

path = sys.argv[1]
try:
    try:
        db = ladybug.Database(path, read_only=True)
    except TypeError:
        db = ladybug.Database(path)
    conn = ladybug.Connection(db)
    conn.execute("MATCH (n) RETURN count(n)")
except Exception as exc:  # pragma: no cover - environment dependent
    sys.stderr.write("open/query failed: %r" % (exc,))
    sys.exit(3)
sys.exit(0)
"""


def probe_graph_store_opens(copy_path: Path) -> bool:
    """Open a *copy* of the graph store out-of-process and count its nodes.

    Runs the open + ``MATCH (n) RETURN count(n)`` in a subprocess because the
    open itself may segfault (the fault being diagnosed); a segfault surfaces
    as a negative return code, which is treated as "did not open cleanly".

    Returns True only when the child exits 0 — a clean open where the count
    query succeeded, proving the copied main file is at a consistent
    checkpoint.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, our own probe script
            [sys.executable, "-c", _GRAPH_PROBE_SCRIPT, str(copy_path)],
            capture_output=True,
            timeout=_GRAPH_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        logger.exception("graph-store open probe subprocess failed to run")
        return False
    if proc.returncode != 0:
        logger.warning(
            "graph-store open probe on %s did not open cleanly (rc=%s): %s",
            copy_path,
            proc.returncode,
            proc.stderr.decode("utf-8", "replace").strip()[:500],
        )
        return False
    return True


def _is_graph_open_segv_error(exc: BaseException) -> bool:
    """Return True if *exc* is a graph-store worker subprocess open segfault."""
    return bool(_GRAPH_OPEN_SEGV_RE.search(str(exc)))


def _is_store_fault_error(exc: BaseException) -> bool:
    """Return True if *exc* is a recognised store fault (lock freeze or worse).

    Used only to decide whether a recall failure marks the backend degraded.
    Anything it does not recognise is caught by the consecutive-failure
    threshold instead, so a novel fault cannot fail silently.
    """
    return _is_lock_freeze_error(exc) or bool(_STORE_FAULT_RE.search(str(exc)))


def _is_memory_write_transient(exc: Exception) -> bool:
    """Return True if *exc* warrants retrying a memory write attempt.

    Covers ``TimeoutError`` (from the per-attempt :func:`asyncio.timeout`)
    and lock-freeze signatures recognised by :func:`_is_lock_freeze_error`.
    """
    if isinstance(exc, TimeoutError):
        return True
    return _is_lock_freeze_error(exc)


# Process-wide recall gate, and the (limit, loop) it was built for.
_RECALL_GATE: asyncio.Semaphore | None = None
_RECALL_GATE_KEY: tuple[int, int] | None = None


def _recall_gate(limit: int) -> asyncio.Semaphore:
    """Return the **process-wide** recall semaphore, created on first use.

    Deliberately not per-instance. cognee's stores are process-global, but the
    server builds a separate ``CogneeMemory`` for every agent — the main chat
    agent plus one per background agent via ``ReadOnlyMemory(build_memory(…))``.
    Production runs six of them, so an instance-scoped bound of 4 would really
    admit 24 concurrent searches to the same contended SQLite layer, which is
    the exact pile-up the bound exists to prevent.

    Keyed by the running loop as well as the limit so tests (each with a fresh
    loop) cannot inherit a semaphore bound to a loop that has since closed.
    """
    global _RECALL_GATE, _RECALL_GATE_KEY
    key = (max(1, limit), id(asyncio.get_running_loop()))
    if _RECALL_GATE is None or key != _RECALL_GATE_KEY:
        _RECALL_GATE = asyncio.Semaphore(key[0])
        _RECALL_GATE_KEY = key
    return _RECALL_GATE


class CogneeMemory:
    """Long-term agent memory backed by cognee.

    One instance per *configuration*, shared by every agent — ``build_memory``
    memoizes construction because cognee's own state is process-global and the
    cold start is expensive, so the startup warm-up covers all agents,
    including subsessions spawned later. ``recall`` is
    on the request's latency path (kept to a vector/graph lookup), while
    ``remember`` runs the expensive consolidation and is expected to be called
    in the background by the agent.
    """

    def __init__(
        self,
        settings: MemorySettings,
        langfuse: LangfuseSettings | None = None,
        openrouter: OpenRouterSettings | None = None,
    ) -> None:
        """Store settings; actual cognee configuration is deferred to ``setup``.

        Args:
            settings: Memory configuration, including ``langfuse_project`` —
                the name of the Langfuse project cognee's own LLM traffic
                traces to.
            langfuse: The component's canonical Langfuse credential block,
                where that project's credentials live.  Omitted (or empty)
                means cognee LLM calls are not traced.
            openrouter: The component's canonical OpenRouter credential block.
                The extraction-LLM key is resolved from ``keys`` under
                ``settings.langfuse_project``'s alias.  Omitted (or empty)
                means cognee runs without an extraction-LLM key.

        """
        from robotsix_chat.config.models import LangfuseSettings as _LangfuseSettings
        from robotsix_chat.config.models import (
            OpenRouterSettings as _OpenRouterSettings,
        )

        self._settings = settings
        self._langfuse = langfuse if langfuse is not None else _LangfuseSettings()
        self._openrouter = (
            openrouter if openrouter is not None else _OpenRouterSettings()
        )
        self._setup_done = False
        self._setup_lock = asyncio.Lock()
        # The in-flight one-shot configuration task — see :meth:`setup`.
        self._setup_task: asyncio.Task[None] | None = None
        # Serialise writes: concurrent cognify() runs would contend on cognee's
        # shared stores.
        self._write_lock = asyncio.Lock()
        # Bound (rather than serialise) recalls. cognee already serialises
        # internally on its SQLite metadata store, so an unbounded fan-in does
        # not run faster — it just parks every caller on the same contended
        # resource until they all hit the deadline together. Admitting a few at
        # a time keeps each search in its uncontended 0.5-1.3 s range and lets
        # a queued burst drain well inside one recall timeout.
        # The gate itself is process-wide (see :func:`_recall_gate`) — it is
        # resolved lazily in ``_recall_core`` because it needs a running loop.
        self._recall_limit = max(1, settings.recall_max_concurrency)
        # Serialise backlog drains so overlapping drain calls cannot silently
        # drop entries or replay duplicates.
        self._drain_lock = asyncio.Lock()
        # Frozen-store detection: track when consecutive write failures began
        # (monotonic seconds since an arbitrary point) so we can alert when the
        # vector store has been failing for longer than the configured threshold.
        # This one is reset after each alert to rate-limit the log line.
        self._write_failure_start: float | None = None
        # Count of exchanges lost since the last successful write (for
        # diagnostics — only the last-failure timestamp drives the alert).
        self._consecutive_write_failures: int = 0
        # Start of the *current* freeze streak — set on the first failure and
        # NOT reset by the alert rate-limiter, so it measures the true freeze
        # duration that drives the degraded flag and auto-recovery threshold.
        # Cleared only on a successful write/recall.
        self._freeze_start: float | None = None
        # Externally-visible degraded state (surfaced on GET /health) and the
        # reason string.  A fault, not the benign empty-store case.
        self._degraded: bool = False
        self._degraded_reason: str | None = None
        # Consecutive recall failures whose exception matched no known fault
        # signature.  The benign empty-store case also raises, but stops as
        # soon as anything is written, so only a *sustained* streak is treated
        # as a fault.  This is the catch-all that makes an unrecognised
        # failure mode (e.g. a new graph-engine abort) surface instead of
        # being read as "memory is simply empty".
        self._consecutive_recall_failures: int = 0
        # Auto-recovery (guarded self-restart) wiring.
        self._recover_cb: RecoverCallback | None = None
        self._last_recovery_attempt: float | None = None
        self._recovery_in_flight: bool = False
        # Hold a reference to the in-flight recovery task so it isn't GC'd.
        self._recovery_task: asyncio.Task[None] | None = None
        # One-shot guard for the boot-time ladybug WAL auto-recovery.  Latched
        # the first time a WAL-corruption abort triggers a sidecar stash so a
        # store that stays broken cannot loop the stash+retry every turn.
        self._wal_heal_attempted: bool = False
        # Graph-store open-segfault (exit -11) tracking.  A worker that dies in
        # native code at ``_open_database`` leaves no catchable text, so the
        # WAL heal above cannot fire.  These count *consecutive* open segfaults
        # so a sustained crash-loop (a) degrades with a specific reason, (b)
        # backs off the tight respawn loop, and (c) drives the validated-copy
        # recovery below.  One-shot heal latch, like ``_wal_heal_attempted``.
        self._consecutive_graph_segv_failures: int = 0
        self._segv_backoff_until: float | None = None
        self._segv_heal_attempted: bool = False
        self._segv_recovery_task: asyncio.Task[None] | None = None
        # User-facing escalation transport (notify_user).  Injected by the
        # server; ``None`` falls back to an ERROR log only.
        self._notify_cb: NotifyCallback | None = None
        # Periodic LanceDB compaction/pruning scheduler (lazily created in
        # :meth:`start_maintenance`); ``None`` when disabled or not started.
        self._maintenance: LanceDbMaintenanceScheduler | None = None
        # Periodic cognee_db VACUUM scheduler (lazily created in
        # :meth:`start_maintenance`); ``None`` when disabled or not started.
        self._vacuum: CogneeDbVacuumScheduler | None = None
        # Periodic consolidated store-size metrics emitter (lazily created in
        # :meth:`start_maintenance`); ``None`` when disabled or not started.
        self._metrics: StoreMetricsScheduler | None = None

    # -- health & recovery wiring -----------------------------------------

    def set_recovery_callback(self, callback: RecoverCallback | None) -> None:
        """Register (or clear) the guarded self-restart used for auto-recovery.

        Injected by the server (wired to the deploy-lifecycle ``self_restart``
        endpoint) so this module stays decoupled from the HTTP client and is
        trivially testable with a fake callback.
        """
        self._recover_cb = callback

    def set_notify_callback(self, callback: NotifyCallback | None) -> None:
        """Register (or clear) the user-facing escalation (``notify_user``).

        Injected by the server so this module escalates a store fault that
        auto-recovery cannot safely heal without a hard dependency on the
        notification/EventBus layer.
        """
        self._notify_cb = callback

    def status(self) -> dict[str, Any]:
        """Return a small health snapshot for ``GET /health`` (never raises)."""
        return {
            "backend": "cognee",
            "degraded": self._degraded,
            "reason": self._degraded_reason,
            "consecutive_write_failures": self._consecutive_write_failures,
            "consecutive_recall_failures": self._consecutive_recall_failures,
            "consecutive_graph_segv_failures": self._consecutive_graph_segv_failures,
        }

    def _mark_degraded(self, reason: str) -> None:
        """Flag the store degraded (idempotent); logs the transition once."""
        if not self._degraded:
            logger.error("cognee memory entering DEGRADED state: %s", reason)
        self._degraded = True
        self._degraded_reason = reason

    def _clear_degraded(self) -> None:
        """Clear degraded state and freeze tracking after a successful op."""
        if self._degraded:
            logger.info("cognee memory recovered — leaving degraded state")
        self._degraded = False
        self._degraded_reason = None
        self._freeze_start = None
        self._write_failure_start = None
        self._consecutive_write_failures = 0
        self._consecutive_recall_failures = 0
        # A responsive store also ends any graph-open segfault crash-loop and
        # its backoff window (the one-shot ``_segv_heal_attempted`` latch is
        # deliberately NOT reset — it guards against re-looping the recovery).
        self._consecutive_graph_segv_failures = 0
        self._segv_backoff_until = None

    # -- lifecycle --------------------------------------------------------

    async def setup(self) -> None:
        """Apply cognee configuration once (idempotent, cancellation-safe).

        The work runs in a task that callers only ``shield``-await: a caller
        cancelled by its recall deadline abandons the *wait*, never the setup,
        so the next recall finds it finished (or still progressing) instead of
        restarting from zero. Observed live without this: cognee's cold start
        runs 47-105 s, so an instance whose first recall was cancelled at the
        recall deadline mid-``_configure`` kept ``_setup_done`` False and
        re-paid (and re-lost) the full cost on every subsequent recall,
        leaving it memory-less forever.

        ``_configure`` itself runs in a worker thread: it imports cognee,
        which blocks for seconds and would otherwise stall the event loop
        (health checks included) for the duration.
        """
        if self._setup_done:
            return
        task = self._setup_task
        if task is None or task.done():
            # First call — or the previous attempt itself failed. (A *caller*
            # being cancelled does not cancel the task, so a live task here
            # means setup is still progressing and is simply re-awaited.)
            task = asyncio.create_task(self._run_setup())
            self._setup_task = task
        await asyncio.shield(task)

    async def _run_setup(self) -> None:
        """Configure cognee exactly once (the body behind :meth:`setup`)."""
        async with self._setup_lock:
            if self._setup_done:
                return
            await asyncio.to_thread(self._configure)
            self._setup_done = True

    async def warm(self) -> None:
        """Pay cognee's cold-start cost off the request path (never raises).

        ``recall`` calls :meth:`setup` *inside* the caller's timeout, so
        without this the first turns after a restart are billed for cognee's
        import and configuration plus the lazy store opens that the very first
        search triggers. Live, that ran past the recall deadline and the
        opening turns of every restart proceeded memory-less: the boot window
        produced a burst of timeouts whose first one landed 17 ms after the
        "cognee memory configured" line — i.e. setup alone had consumed the
        whole budget.

        Intended to be fired as a background task at server startup: it must
        not block readiness, and a failure here only forfeits the head start.
        """
        start = time.monotonic()
        try:
            await self.setup()
            # Prime the read path too. Configuration alone is not enough —
            # the first search is what opens the vector tables, and that was
            # observed timing out on its own even after setup had completed.
            await self._recall_core(_WARMUP_QUERY)
        except Exception as exc:
            logger.warning(
                "cognee memory warm-up failed (%s) — the first recall will pay "
                "the cold-start cost instead",
                exc,
            )
            return
        logger.info("cognee memory warm-up complete in %.1fs", time.monotonic() - start)

    # -- clean shutdown ---------------------------------------------------

    async def shutdown(self) -> None:
        """Checkpoint and close the ladybug graph store before process exit.

        Deploys restart chat routinely; an interrupted WAL write is the
        suspected trigger of the ``wal_record``/``UNREACHABLE_CODE`` corruption
        :meth:`_stash_ladybug_wal_sidecars` auto-recovers from at boot.  A
        clean ``CHECKPOINT`` flushes the WAL into the main graph file so the
        next open replays nothing stale, then the adapter is closed.

        Best-effort and never raises — a fully-closed, corrupt, or unreachable
        store must not block process shutdown.  Only runs when setup actually
        happened (a never-opened store has nothing to checkpoint).
        """
        if not self._setup_done:
            return
        try:
            from cognee.infrastructure.databases.graph import get_graph_engine

            engine = await get_graph_engine()
            await engine.checkpoint()
            logger.info("cognee ladybug graph store checkpointed")
            await engine.close()
            logger.info("cognee ladybug graph store closed")
        except Exception:
            logger.exception(
                "cognee ladybug graph store shutdown failed — the next boot will "
                "auto-recover if the WAL was left inconsistent"
            )

    # -- periodic memory maintenance -------------------------------------

    def _lancedb_store_path(self) -> Path:
        """Absolute path of the cognee LanceDB store directory."""
        data_dir = Path(self._settings.data_dir).expanduser().resolve()
        return data_dir / LANCEDB_RELATIVE_PATH

    def _cognee_db_path(self) -> Path:
        """Absolute path of the cognee SQLite relational store file."""
        data_dir = Path(self._settings.data_dir).expanduser().resolve()
        return data_dir / COGNEE_DB_RELATIVE_PATH

    def start_maintenance(self) -> None:
        """Start the periodic memory maintenance schedulers.

        Idempotent and a no-op when maintenance is disabled or the interval is
        non-positive.  Intended to be called once at server startup.

        Starts two schedulers:

        * the LanceDB compaction/pruning scheduler — runs a pass immediately
          and then every ``maintenance_interval_seconds``;
        * the cognee_db VACUUM scheduler — runs inside the configured
          off-peak UTC window every ``maintenance_vacuum_interval_seconds``.

        Each pass runs under the write lock so it never overlaps a live
        ``cognify``.
        """
        s = self._settings
        if not s.maintenance_enabled:
            return
        if self._maintenance is None and s.maintenance_interval_seconds > 0:
            self._maintenance = LanceDbMaintenanceScheduler(
                store_path=self._lancedb_store_path(),
                write_lock=self._write_lock,
                interval_seconds=s.maintenance_interval_seconds,
                cleanup_older_than=timedelta(
                    seconds=s.maintenance_version_retention_seconds
                ),
            )
            self._maintenance.start()
        if (
            self._vacuum is None
            and s.maintenance_vacuum_enabled
            and s.maintenance_vacuum_interval_seconds > 0
        ):
            window = (
                (
                    int(s.maintenance_vacuum_off_peak_window[0]),
                    int(s.maintenance_vacuum_off_peak_window[1]),
                )
                if s.maintenance_vacuum_off_peak_window is not None
                else None
            )
            self._vacuum = CogneeDbVacuumScheduler(
                db_path=self._cognee_db_path(),
                write_lock=self._write_lock,
                interval_seconds=s.maintenance_vacuum_interval_seconds,
                mode=s.maintenance_vacuum_mode,
                off_peak_window=window,
            )
            self._vacuum.start()
        if (
            self._metrics is None
            and s.maintenance_metrics_enabled
            and s.maintenance_metrics_interval_seconds > 0
        ):
            self._metrics = StoreMetricsScheduler(
                data_dir=Path(s.data_dir).expanduser().resolve(),
                interval_seconds=s.maintenance_metrics_interval_seconds,
                graph_stats_provider=self._graph_stats,
            )
            self._metrics.start()

    async def _graph_stats(self) -> tuple[int, int] | None:
        """Best-effort ``(nodes, edges)`` count from the live graph engine.

        Read-only and never raises: returns ``None`` when the store is not yet
        set up, cognee is absent, or the engine cannot report counts, so a
        missing count degrades the metrics line rather than the scheduler.
        """
        if not self._setup_done:
            return None
        try:
            from cognee.infrastructure.databases.graph import get_graph_engine

            engine = await get_graph_engine()
            nodes, edges = await engine.get_graph_data()
            return len(nodes), len(edges)
        except Exception:
            logger.debug("cognee graph node/edge counts unavailable", exc_info=True)
            return None

    async def stop_maintenance(self) -> None:
        """Stop the memory maintenance schedulers if they are running."""
        if self._metrics is not None:
            await self._metrics.stop()
            self._metrics = None
        if self._vacuum is not None:
            await self._vacuum.stop()
            self._vacuum = None
        if self._maintenance is not None:
            await self._maintenance.stop()
            self._maintenance = None

    def _configure(self) -> None:
        """Configure cognee's global state from the stored settings.

        Sets environment variables for single-user posture, hides Langfuse
        credentials during cognee's import (to avoid a missing-SDK crash),
        resolves and creates data/system directories, and configures the
        extraction LLM and embedding providers.
        Must be called once before any recall/remember operations.
        """
        s = self._settings
        # Embedded, single-user posture (cognee defaults to multi-tenant auth).
        os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "false")
        os.environ.setdefault("TELEMETRY_DISABLED", "1")
        os.environ.setdefault("MONITORING_TOOL", "none")
        # cognee 1.4's session memory (SQL cache) writes session context on
        # every search/add.  Chat never reads it back, and on the shared-HDD
        # deploy host the cache sqlite hit a 626 MB un-checkpointable WAL —
        # every turn then stalled ~30 s in a "database is locked" busy-wait
        # inside the recall path (2026-08-07 incident, #1201).  Disabled:
        # SessionManager no-ops cleanly when caching is off.
        os.environ.setdefault("CACHING", "false")

        # Bound LanceDB's DataFusion memory pool so a single large merge_insert
        # cannot OOM the worker subprocess.  DataFusion reads
        # ``DATAFUSION_RUNTIME_MEMORY_LIMIT`` from the env at session init time
        # (before ``import cognee``, so set it now).
        if s.datafusion_runtime_memory_limit:
            os.environ.setdefault(
                "DATAFUSION_RUNTIME_MEMORY_LIMIT",
                s.datafusion_runtime_memory_limit,
            )

        # Cap the ingestion pipeline's spawn-worker fan-out.  cognee ingests
        # through ``dlt``, whose normalize stage scales a multiprocessing
        # process pool with the host CPU count; each worker holds a data batch
        # in memory, so a large cognify backlog fans out into several
        # multi-GB spawn workers with no aggregate bound — the RSS blow-up that
        # OOM-killed the container on 2026-09-03.  dlt reads these per-stage
        # worker counts from the env at pipeline-config time (before
        # ``import cognee``), so set them now.
        if s.cognify_max_workers >= 1:
            workers = str(s.cognify_max_workers)
            for _env in ("EXTRACT__WORKERS", "NORMALIZE__WORKERS", "LOAD__WORKERS"):
                os.environ.setdefault(_env, workers)

        # cognee force-selects Langfuse as its monitoring tool when LANGFUSE_*
        # creds are present in the env (a model validator, overriding
        # MONITORING_TOOL) and then `import cognee` does `from langfuse.decorators
        # import observe` — which crashes because the image ships no langfuse SDK.
        # Hide those creds for cognee's (one-time) import so it caches
        # monitoring=NONE; llmio's own Langfuse tracing was already configured at
        # server startup, so it is unaffected.
        saved_langfuse = {
            key: os.environ.pop(key)
            for key in (
                "LANGFUSE_PUBLIC_KEY",
                "LANGFUSE_SECRET_KEY",
                "LANGFUSE_HOST",
                "LANGFUSE_BASE_URL",
            )
            if key in os.environ
        }
        try:
            import cognee
        finally:
            os.environ.update(saved_langfuse)

        # cognee builds file:// URIs from these, so they MUST be absolute —
        # a relative data_dir raises "relative paths can't be expressed as file
        # URIs" deep in ingestion. Resolve against the working dir.
        data_dir = Path(s.data_dir).expanduser().resolve()
        data_root = data_dir / "data"
        system_root = data_dir / "system"
        data_root.mkdir(parents=True, exist_ok=True)
        system_root.mkdir(parents=True, exist_ok=True)

        # NOTE: no shadow/WAL "self-heal" here. That logic was written when
        # cognee's graph store was kuzu; since 1.4 it is cognee's own embedded
        # ladybug engine, whose ``.wal`` is its crash-recovery journal and is
        # replayed on open. Deleting it destroyed the live knowledge graph on
        # every startup. The graph engine's on-disk layout is not something we
        # touch, but the relational-store *schema* can still change when cognee
        # is upgraded under us (dependabot auto-merged 1.4.0->1.5.3 on
        # 2026-09-02, whose new schema then failed every recall with
        # ``OperationalError: table queries has no column named dataset_id``).
        # ``_run_migrations`` below brings the deployed schema up to head.
        cognee.config.data_root_directory(str(data_root))
        cognee.config.system_root_directory(str(system_root))

        # Reconcile the deployed relational-store schema with the installed
        # cognee version BEFORE the first recall/remember opens it — otherwise
        # a stale schema surfaces as per-query ``OperationalError`` failures.
        self._run_migrations()

        # Extraction LLM — OpenRouter via litellm's `custom` provider.
        from robotsix_chat.config import PROJECT_MEMORY

        alias = s.langfuse_project or PROJECT_MEMORY
        cognee.config.set_llm_provider(s.llm.provider)
        cognee.config.set_llm_model(s.llm.model)
        cognee.config.set_llm_endpoint(s.llm.endpoint)
        cognee.config.set_llm_api_key(self._openrouter.key(alias).get_secret_value())
        # Pin EVERY LLM pipeline stage to the configured model. cognee's
        # per-stage routing (extraction / summarization / query) and its BAML
        # fallback each carry their own built-in default model when
        # left empty; a pipeline step reading one of those directly would
        # otherwise bill the untraced default model instead of the configured
        # model. Setting them all to the same value guarantees no
        # cognee step can silently fall back to its internal default.
        cognee.config.set_llm_config(
            {
                "llm_max_completion_tokens": s.llm.max_completion_tokens,
                "llm_extraction_model": s.llm.model,
                "llm_summarization_model": s.llm.model,
                "llm_query_model": s.llm.model,
                "baml_llm_model": s.llm.model,
            }
        )
        self._flag_configured_llm_model_drift(s.llm.model)

        # Embeddings — remote OpenAI-compatible server (Ollama / bge-m3).
        cognee.config.set_embedding_config(
            {
                "embedding_provider": s.embedding.provider,
                "embedding_model": s.embedding.model,
                "embedding_endpoint": s.embedding.endpoint,
                "embedding_dimensions": s.embedding.dimensions,
                "embedding_api_key": s.embedding.api_key.get_secret_value(),
                "huggingface_tokenizer": s.embedding.huggingface_tokenizer,
            }
        )

        logger.info(
            "cognee memory configured (data_dir=%s, embed=%s@%s, llm=%s)",
            data_dir,
            s.embedding.model,
            s.embedding.endpoint,
            s.llm.model,
        )

        self._register_litellm_langfuse_callback()

    def _run_migrations(self) -> None:
        """Upgrade the cognee relational-store schema to head via alembic.

        cognee ships its own alembic migration tree (``alembic.ini`` +
        ``alembic/`` alongside the package). Since the package is no longer
        effectively pinned against us — dependabot auto-merged 1.4.0->1.5.3 on
        2026-09-02 (#1544) and the newer schema then failed *every* recall with
        ``OperationalError: table queries has no column named dataset_id`` — the
        deployed SQLite schema under ``data_dir`` can lag the installed cognee
        version. Running ``alembic upgrade head`` in-process here (after the
        data/system roots are configured, before the first store open)
        reconciles it, exactly as the operator did manually to recover the live
        DB.

        Called only from :meth:`_configure`, which runs once under
        :attr:`_setup_lock` — that lock is the startup gate serialising the
        migration, and callers must keep the process quiescent (no concurrent
        store access) while it runs, because SQLite requires quiescence to take
        the write lock.

        Raises:
            RuntimeError: if the migration fails, so setup surfaces a single
                clear error instead of degrading to per-query
                ``OperationalError`` failures on the recall path.

        """
        import cognee
        from alembic import command
        from alembic.config import Config

        package_dir = Path(cognee.__file__).resolve().parent
        cfg = Config(str(package_dir / "alembic.ini"))
        # The packaged script_location is relative to the ini's own directory;
        # pin it to the absolute path so the migration runs regardless of CWD.
        cfg.set_main_option("script_location", str(package_dir / "alembic"))
        # Do not let alembic reconfigure the root logger (it would clobber the
        # server's logging config); env.py honors this attribute.
        cfg.attributes["configure_logger"] = False
        try:
            command.upgrade(cfg, "head")
        except Exception as exc:
            raise RuntimeError(
                "cognee relational-store schema migration (alembic upgrade "
                "head) failed; the deployed schema does not match the installed "
                f"cognee version and recall/remember would fail per-query: {exc}"
            ) from exc
        logger.info("cognee relational-store schema migrated to head")

    def _register_litellm_langfuse_callback(self) -> None:
        """Wire litellm Langfuse OTLP tracing with dedicated cognee creds.

        Registers an explicitly-configured ``LangfuseOtelLogger`` *instance*
        (OTLP over HTTP) rather than the ``"langfuse_otel"`` string callback:
        the string form makes litellm build its config from the
        ``LANGFUSE_PUBLIC_KEY``/``LANGFUSE_SECRET_KEY``/``LANGFUSE_HOST``
        environment **lazily on the first LLM call** — which would pick up the
        main chat project's credentials (and, with ``LANGFUSE_HOST`` unset,
        default the exporter to Langfuse US cloud). An instance carries its
        own endpoint + Basic-auth header, so neither the process env nor
        llmio's already-initialized tracing is involved at all.

        Cognee's internal LLM traffic lands in its own Langfuse project —
        the one named by ``MemorySettings.langfuse_project``, resolved
        against the component's canonical ``langfuse`` block (per-standards:
        one Langfuse project per repo/function).  Graceful no-op when that
        project is absent or half-configured.
        """
        creds = self._langfuse.creds(self._settings.langfuse_project)
        if not creds.is_configured():
            logger.debug(
                "Langfuse project %r not configured; skipping litellm callback",
                self._settings.langfuse_project,
            )
            return

        lf_public = creds.public_key.get_secret_value()
        lf_secret = creds.secret_key.get_secret_value()

        # Host comes from the canonical block, so config.json is honored
        # rather than whatever the server CLI happened to export to env for
        # the *main* project.
        lf_host = self._langfuse.host
        if not lf_host:
            logger.warning(
                "cognee Langfuse creds set but no LANGFUSE_BASE_URL/LANGFUSE_HOST; "
                "skipping litellm Langfuse callback (exporter would default to "
                "Langfuse US cloud)"
            )
            return

        try:
            import litellm  # type: ignore[import-not-found]
            from litellm.integrations.langfuse.langfuse_otel import (  # type: ignore[import-not-found]
                LangfuseOtelLogger,
            )
            from litellm.integrations.opentelemetry import (  # type: ignore[import-not-found]
                OpenTelemetryConfig,
            )

            # opentelemetry-exporter-otlp-proto-http ships with the ``tracing``
            # extra; import-check so we fail fast with a warning, not at call-time.
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]  # noqa: F401
                OTLPSpanExporter,
            )
        except ImportError as exc:
            logger.warning(
                "litellm Langfuse OTEL tracing unavailable (%s); "
                "install the 'tracing' extra alongside 'memory' to enable it",
                exc,
            )
            return

        # Idempotent: setup() can run more than once per process.
        if any(isinstance(cb, LangfuseOtelLogger) for cb in litellm.callbacks):
            return

        if not lf_host.startswith("http"):
            lf_host = "https://" + lf_host
        # Langfuse's OTLP route needs the full signal path — the bare
        # /api/public/otel prefix 404s (verified against the live instance).
        endpoint = lf_host.rstrip("/") + "/api/public/otel/v1/traces"
        auth = base64.b64encode(f"{lf_public}:{lf_secret}".encode()).decode()
        otel_logger = LangfuseOtelLogger(
            config=OpenTelemetryConfig(
                exporter="otlp_http",
                endpoint=endpoint,
                headers=f"Authorization=Basic {auth}",
                # llmio registers the GLOBAL tracer provider (main-project
                # exporter) at server startup; without this flag litellm
                # attaches to it and cognee spans land in the MAIN Langfuse
                # project. Forces a private, isolated provider instead.
                skip_set_global=True,
            )
        )
        litellm.callbacks.append(otel_logger)

        # Stamp all cognee-issued litellm calls with a component tag so they
        # are distinguishable at a glance in the Langfuse project.
        tag = "component:cognee"
        if litellm.langfuse_default_tags is None:
            litellm.langfuse_default_tags = [tag]
        elif tag not in litellm.langfuse_default_tags:
            litellm.langfuse_default_tags = [*list(litellm.langfuse_default_tags), tag]

        logger.info(
            "litellm Langfuse OTLP tracing configured for cognee traffic (%s)",
            endpoint,
        )

    def _flag_configured_llm_model_drift(self, expected_model: str) -> None:
        """Log an error if any cognee LLM stage resolved to a non-configured model.

        ``set_llm_model``/``set_llm_config`` mutate cognee's process-global,
        cached ``LLMConfig``. Read it back and flag every model-bearing field
        that does not equal the configured value. This is the startup trip-wire
        for the "unconfigured cognee LLM model" failure mode: a config-setter
        no-op, or a stage silently falling back to cognee's internal default
        model, would otherwise burn OpenRouter credit
        invisibly. Deliberately a LOG, never a raise — memory keeps working,
        but the drift is impossible to miss.
        """
        try:
            from cognee.infrastructure.llm import get_llm_config
        except ImportError:
            return
        effective = get_llm_config()
        stage_models = {
            "llm_model": effective.llm_model,
            "llm_extraction_model": effective.llm_extraction_model,
            "llm_summarization_model": effective.llm_summarization_model,
            "llm_query_model": effective.llm_query_model,
            "baml_llm_model": effective.baml_llm_model,
        }
        drift = {
            name: model
            for name, model in stage_models.items()
            if model != expected_model
        }
        if drift:
            logger.error(
                "cognee LLM model drift detected — configured %r but %s resolved "
                "to %s; untraced default-model calls may be billed",
                expected_model,
                ", ".join(drift),
                drift,
            )

    # -- read -------------------------------------------------------------

    async def recall(self, query: str, *, session_id: str | None = None) -> str:
        """Return memory relevant to *query* (``""`` on any failure).

        *session_id* scopes the recall to one conversation, isolating
        session-level guidance across concurrent windows.

        Wrapped in :func:`asyncio.timeout` so a hang in the cognee stack
        (e.g. orphaned LanceDB adapter lock) degrades to "no memory"
        instead of freezing the caller forever.
        """
        if not query.strip():
            return ""
        if self._in_graph_segv_backoff():
            # The graph-store worker is crash-looping (segfault) at open.
            # Short-circuit rather than respawn the segfaulting worker again —
            # the backoff window stops the tight ~2s respawn loop.  The store
            # stays degraded (surfaced on GET /health) until recovery clears it.
            return ""
        try:
            async with asyncio.timeout(self._settings.recall_timeout_seconds):
                result = await self._recall_core(query, session_id=session_id)
            # A successful recall proves the store is responsive again.
            self._clear_degraded()
            return result
        except TimeoutError:
            logger.warning(
                "memory recall timed out after %.0fs; continuing without memory",
                self._settings.recall_timeout_seconds,
            )
            # A recall timeout is the orphaned-lock freeze signature — surface it
            # (visible on GET /health); the write path drives guarded recovery.
            self._mark_degraded(
                f"recall timed out after {self._settings.recall_timeout_seconds:.0f}s"
            )
            return ""
        except Exception as exc:
            # Best-effort: a recall failure (incl. the expected "empty store"
            # case on the first-ever message) must never break the reply, so
            # log it concisely — no ERROR-level traceback — and continue.
            logger.warning("memory recall failed (%s); continuing without memory", exc)
            self._record_recall_failure(exc)
            return ""

    def _record_recall_failure(self, exc: Exception) -> None:
        """Decide whether a failed recall means the backend is degraded.

        A recognised store fault degrades immediately.  Anything else only
        degrades once it has failed ``recall_failure_degrade_threshold`` times
        in a row: the benign empty-store error also lands here, but it stops
        as soon as the first exchange is written, whereas a real fault repeats
        on every turn.

        The previous version marked degraded *only* on a known signature, so
        an unrecognised fault (a graph-engine WAL abort, for one) left the
        backend reporting healthy while every recall failed.  Silence is the
        failure mode worth designing against here, so the default is now to
        report an unexplained streak rather than assume it is benign.
        """
        self._consecutive_recall_failures += 1
        # A graph-store worker subprocess segfault at open (exit -11) leaves no
        # catchable assertion text, so it matches neither the store-fault nor
        # the WAL-corruption signatures.  Recognise a *sustained* crash-loop as
        # its own fault: degrade with a specific reason, back off the tight
        # respawn loop, and drive the validated-copy recovery.
        if _is_graph_open_segv_error(exc):
            self._consecutive_graph_segv_failures += 1
            threshold = max(1, self._settings.recall_failure_degrade_threshold)
            if self._consecutive_graph_segv_failures >= threshold:
                self._enter_graph_segv_fault(exc)
            return
        # Any other failure ends a segfault crash-loop streak.
        self._consecutive_graph_segv_failures = 0
        if _is_store_fault_error(exc):
            self._mark_degraded(f"recall failed: {exc}")
            return
        threshold = max(1, self._settings.recall_failure_degrade_threshold)
        if self._consecutive_recall_failures >= threshold:
            self._mark_degraded(
                f"{self._consecutive_recall_failures} consecutive recall failures; "
                f"last error: {exc}"
            )

    def _in_graph_segv_backoff(self) -> bool:
        """Return True while the post-segfault recall backoff window is open."""
        until = self._segv_backoff_until
        return until is not None and time.monotonic() < until

    def _enter_graph_segv_fault(self, exc: Exception) -> None:
        """Handle a sustained graph-open segfault crash-loop.

        Marks the store degraded with a specific reason, opens the backoff
        window that stops the tight respawn loop, and schedules the guarded
        validated-copy recovery exactly once per process.
        """
        reason = (
            f"graph-store worker segfaulted (exit -11) at open "
            f"{self._consecutive_graph_segv_failures}x — {exc}"
        )
        self._mark_degraded(reason)
        self._segv_backoff_until = time.monotonic() + _GRAPH_SEGV_BACKOFF_SECONDS
        if not self._settings.auto_recovery_enabled:
            return
        if self._segv_heal_attempted:
            return
        if self._segv_recovery_task is not None and not self._segv_recovery_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._segv_recovery_task = loop.create_task(
            self._run_graph_segv_recovery(reason)
        )

    async def _recall_core(
        self,
        query: str,
        *,
        session_id: str | None = None,
        search_type_name: str | None = None,
    ) -> str:
        """Inner recall logic — separated so the timeout wrapper is clean.

        *search_type_name* overrides the configured automatic-recall search
        type; the deep on-demand path passes ``deep_recall_search_type``.
        """
        await self.setup()
        import cognee
        from cognee import SearchType

        requested = search_type_name or self._settings.recall_search_type
        # Fall back to GRAPH_COMPLETION for an unknown name — the one type
        # every cognee version ships.
        search_type = (
            getattr(SearchType, requested, None) or SearchType.GRAPH_COMPLETION
        )

        async def _search() -> str:
            results = await cognee.search(
                query_type=search_type,
                query_text=query,
                session_id=session_id,
            )
            return _format_results(results)

        # Acquired *inside* the caller's timeout, deliberately: queue time is
        # part of the recall's latency budget, so a pathological backlog still
        # degrades to "no memory" on schedule rather than stalling the turn
        # past the deadline the caller was promised.
        async with _recall_gate(self._recall_limit):
            try:
                return await _search()
            except Exception as exc:
                if self._try_ladybug_wal_recovery(exc):
                    # Stashed the (corrupt) WAL sidecars — retry the graph-store
                    # open exactly once.  A second failure propagates (degraded,
                    # surfaced on /health) rather than looping.
                    return await _search()
                raise

    async def recall_deep(self, query: str) -> str:
        """Run the expensive, LLM-mediated graph search on demand.

        Backs the agent's ``search_memory`` tool. Unlike the automatic
        :meth:`recall` — which runs on every message and must therefore be
        cheap (``recall_search_type``, default ``CHUNKS``) — this uses
        ``deep_recall_search_type`` (default ``GRAPH_COMPLETION``) under the
        more generous ``deep_recall_timeout_seconds``.

        Deliberately unscoped by session: the point of an explicit lookup is
        to reach across the whole memory, not just the current window.

        Returns a human/LLM-readable failure string rather than ``""`` on
        error — a tool result should tell the model *why* nothing came back,
        where the automatic path must stay silent.
        """
        if not query.strip():
            return "Empty query — nothing to search for."
        if self._in_graph_segv_backoff():
            return (
                "Memory is temporarily unavailable — the graph store is "
                "recovering from a fault. Continue without it."
            )
        timeout = self._settings.deep_recall_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                result = await self._recall_core(
                    query,
                    search_type_name=self._settings.deep_recall_search_type,
                )
            self._clear_degraded()
            return result or "No relevant memory found for that query."
        except TimeoutError:
            logger.warning("deep memory search timed out after %.0fs", timeout)
            self._mark_degraded(f"deep recall timed out after {timeout:.0f}s")
            return (
                f"Memory search timed out after {timeout:.0f}s — the store may "
                "be under load. Try a narrower query, or continue without it."
            )
        except Exception as exc:
            logger.warning("deep memory search failed (%s)", exc)
            if _is_lock_freeze_error(exc):
                self._mark_degraded(f"deep recall failed: {exc}")
            return f"Memory search failed ({exc}). Continue without it."

    # -- write ------------------------------------------------------------

    async def remember(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Persist one exchange into long-term memory (consolidates the graph).

        *session_id* scopes the write to one conversation, isolating
        session-level guidance across concurrent windows.

        Wrapped in :func:`asyncio.timeout` so a hang in cognee's
        consolidation pipeline (e.g. orphaned LanceDB adapter lock)
        skips the write instead of leaking a stuck background task.

        Attempted up to ``remember_max_attempts`` times with exponential
        backoff via :func:`robotsix_http.acall_with_retry`.  Only when every
        attempt fails is the exchange appended to a durable JSONL backlog so
        it is not silently lost — subsequent successful writes
        opportunistically drain the backlog.

        The retry is not decoration: cognify is a multi-minute LLM pipeline
        contending with recall for cognee's stores, so a single slow pass was
        routinely enough to lose the write.  Observed 2026-08-01, before this
        existed: 20 consecutive ``memory write timed out`` in one afternoon,
        every one of them a conversation that never reached memory, with the
        docstring already claiming "retries exhausted" for a code path that
        made exactly one attempt.
        """
        total_attempts = max(1, self._settings.remember_max_attempts)
        timeout = self._settings.remember_timeout_seconds

        def _on_retry(attempt: int, exc: Exception, delay: float) -> None:
            logger.warning(
                "memory write attempt %d/%d failed (%s: %s); retrying in %.0fs",
                attempt + 1,
                total_attempts,
                type(exc).__name__,
                exc,
                delay,
            )

        async def _attempt() -> None:
            async with asyncio.timeout(timeout):
                await self._remember_core(
                    user_message, assistant_message, session_id=session_id
                )

        try:
            await acall_with_retry(  # type: ignore[unused-coroutine]  # _attempt is async — the generic resolves T=Coroutine[...]
                _attempt,
                config=RetryConfig(
                    max_retries=total_attempts - 1,
                    backoff_base=30.0,
                    backoff_cap=120.0,
                    jitter_factor=0.0,
                    on_retry=_on_retry,
                ),
                is_transient_fn=_is_memory_write_transient,
                what="memory write",
            )
            # Write succeeded → clear freeze/degraded tracking, drain backlog.
            self._clear_degraded()
            try:
                await self._drain_backlog()
            except Exception:
                logger.exception(
                    "Backlog drain failed after successful write — "
                    "backlogged entries preserved for next drain"
                )
        except Exception:
            logger.warning(
                "memory write failed after %d attempts; queued to backlog",
                total_attempts,
                exc_info=True,
            )
            self._record_write_failure()
            self._append_to_backlog(user_message, assistant_message, session_id)

    async def _remember_core(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Inner remember logic — separated so the timeout wrapper is clean."""
        await self.setup()
        import cognee

        text = f"User: {user_message}\nAssistant: {assistant_message}"

        async def _remember() -> None:
            async with self._write_lock:
                await cognee.add(text, session_id=session_id)
                await cognee.cognify(session_id=session_id)
            # Throttle: give the LanceDB worker subprocess time to complete
            # its merge_insert before the next serialised write starts, so a
            # burst of rapid remembers does not collectively OOM the worker.
            if self._settings.write_throttle_seconds > 0:
                await asyncio.sleep(self._settings.write_throttle_seconds)

        await _remember()

    async def ingest_structure_fixture(self) -> dict[str, Any]:
        """Ingest the fixed sample document and return structural metrics.

        Regression-check hook for model/config changes: a fixed document is
        added to an isolated dataset, cognified, and the resulting graph is
        measured (entity count, relation count, and summary lengths).  The
        fixture dataset is dropped first, so every run starts from a clean,
        directly comparable graph and never touches the production memory
        dataset.
        """
        await self.setup()
        import cognee
        from cognee.modules.graph.methods import get_formatted_graph_data
        from cognee.modules.users.methods import get_default_user

        # Best-effort: remove a leftover fixture dataset from a previous run.
        # ``forget`` raises ValueError when the dataset does not exist yet.
        try:
            await cognee.forget(dataset=_INGESTION_FIXTURE_DATASET)
        except Exception:
            logger.debug(
                "ingestion structure check: no previous fixture dataset to drop",
                exc_info=True,
            )

        add_result = await cognee.add(
            _INGESTION_FIXTURE_DOCUMENT,
            dataset_name=_INGESTION_FIXTURE_DATASET,
        )
        dataset_id = getattr(add_result, "dataset_id", None)
        if dataset_id is None and isinstance(add_result, dict):
            dataset_id = next(iter(add_result), None)
        if dataset_id is None:
            raise RuntimeError("cognee.add returned no dataset id")

        await cognee.cognify(datasets=_INGESTION_FIXTURE_DATASET)

        user = await get_default_user()
        graph = await get_formatted_graph_data(dataset_id, user)
        nodes: list[dict[str, Any]] = graph.get("nodes", [])
        edges: list[dict[str, Any]] = graph.get("edges", [])

        entity_count = sum(1 for n in nodes if n.get("type") == "Entity")
        summaries = [n for n in nodes if n.get("type") == "TextSummary"]
        summary_lengths = [
            len(str((n.get("properties") or {}).get("text") or "")) for n in summaries
        ]
        # The graph engine fabricates ``SELF`` edges for isolated nodes, which
        # would mask a relation-extraction regression as a non-zero count.
        relation_count = sum(1 for e in edges if e.get("label") != "SELF")

        return {
            "status": "ok",
            "dataset_name": _INGESTION_FIXTURE_DATASET,
            "dataset_id": str(dataset_id),
            "entity_count": entity_count,
            "relation_count": relation_count,
            "summary_count": len(summaries),
            "summary_lengths": summary_lengths,
            "total_summary_length": sum(summary_lengths),
        }

    # -- write-failure tracking & self-heal -------------------------------

    def _record_write_failure(self) -> None:
        """Mark one write failure; alert, flag degraded, and trigger recovery.

        When the failure streak exceeds the alert threshold an ERROR is emitted
        (rate-limited) and the store is flagged ``degraded`` (visible on
        ``GET /health``).  When it exceeds the recovery threshold a guarded
        self-restart is triggered — the store cannot stay silently frozen.
        """
        now = time.monotonic()
        if self._write_failure_start is None:
            self._write_failure_start = now
        # True freeze clock — not reset by the alert rate-limiter below.
        if self._freeze_start is None:
            self._freeze_start = now
        self._consecutive_write_failures += 1

        elapsed_minutes = (now - self._write_failure_start) / 60.0
        threshold = self._settings.frozen_store_alert_minutes
        if elapsed_minutes >= threshold:
            logger.error(
                "Vector store appears FROZEN: %d consecutive write failures "
                "over the last %.1f minutes (alert threshold: %.1f min). "
                "No new memories are being persisted — check the LanceDB "
                "worker subprocess (cognee_db_workers/lancedb_worker.py) and "
                "container memory budget.",
                self._consecutive_write_failures,
                elapsed_minutes,
                threshold,
            )
            self._mark_degraded(
                f"{self._consecutive_write_failures} consecutive write failures "
                f"over {elapsed_minutes:.1f} min"
            )
            # Reset the *alert* start time so we do not spam the log on every
            # subsequent failure — re-alert only if the freeze persists through
            # another full threshold window.  Advance the reset by a full second
            # (negligible against the minutes-scale threshold) rather than a
            # sub-millisecond epsilon: the gap between two consecutive
            # ``time.monotonic()`` calls is not guaranteed to stay under 1 ms on
            # a loaded CI runner, so 0.001 s could still re-fire the alert.
            # (The freeze clock, ``_freeze_start``, is NOT reset here.)
            self._write_failure_start = now + 1.0

        # Guarded auto-recovery once the freeze has persisted long enough.
        freeze_minutes = (now - self._freeze_start) / 60.0
        if freeze_minutes >= self._settings.frozen_store_recovery_minutes:
            self._maybe_trigger_recovery(freeze_minutes)

    # -- auto-recovery (guarded self-restart) -----------------------------

    def _maybe_trigger_recovery(self, freeze_minutes: float) -> None:
        """Schedule a guarded self-restart if all recovery guards pass.

        Guards: recovery enabled, a callback is wired, none already in flight,
        and the per-attempt cooldown has elapsed (so a store that re-freezes
        right after a restart cannot restart-loop).
        """
        if not self._settings.auto_recovery_enabled:
            return
        if self._recover_cb is None:
            # No self-restart transport (lifecycle disabled) — stay degraded.
            return
        if self._recovery_in_flight:
            return
        now = time.monotonic()
        cooldown = self._settings.recovery_cooldown_minutes * 60.0
        if (
            self._last_recovery_attempt is not None
            and now - self._last_recovery_attempt < cooldown
        ):
            logger.error(
                "cognee frozen %.1f min but auto-recovery is in cooldown "
                "(%.0f min between attempts) — not restarting yet",
                freeze_minutes,
                self._settings.recovery_cooldown_minutes,
            )
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last_recovery_attempt = now
        self._recovery_in_flight = True
        self._recovery_task = loop.create_task(self._run_recovery(freeze_minutes))

    async def _run_recovery(self, freeze_minutes: float) -> None:
        """Invoke the recovery callback (self-restart); never raises."""
        cb = self._recover_cb
        try:
            logger.error(
                "cognee memory FROZEN for %.1f min — triggering guarded "
                "auto-recovery via self-restart",
                freeze_minutes,
            )
            if cb is not None:
                result = await cb()
                logger.error("cognee auto-recovery self-restart requested: %s", result)
        except Exception:
            logger.exception("cognee auto-recovery self-restart failed")
        finally:
            # If the restart did not actually take down the process, allow a
            # future attempt after the cooldown.
            self._recovery_in_flight = False

    # -- boot-time ladybug WAL auto-recovery ------------------------------

    def _ladybug_databases_dir(self) -> Path:
        """Directory holding the ladybug graph-store files.

        The graph engine's on-disk layout (ladybug, cognee-pinned) is a main
        database file ``cognee_graph_ladybug`` plus three WAL-adjacent
        sidecars next to it: ``<base>.wal`` (the crash-recovery journal), and
        ``<base>.wal.checkpoint`` / ``<base>.shadow`` (checkpoint bookkeeping).
        """
        return (
            Path(self._settings.data_dir).expanduser().resolve()
            / "system"
            / "databases"
        )

    def _try_ladybug_wal_recovery(self, exc: BaseException) -> bool:
        """Return True if the WAL-corruption sidecars were stashed (caller retries).

        Only the ladybug WAL-during-replay abort triggers the file-moving
        recovery; any other exception, or a store that already attempted
        healing this process, returns False so the original error propagates.
        """
        if not _is_wal_corruption_error(exc):
            return False
        if not self._settings.auto_recovery_enabled:
            return False
        return self._stash_ladybug_wal_sidecars()

    def _stash_ladybug_wal_sidecars(self) -> bool:
        """Move the corrupt ladybug WAL sidecars aside with a dated suffix.

        On a WAL-replay abort the graph engine cannot open its store because
        the checkpoint marker / shadow pre-image are inconsistent.  Stashing
        ``<base>.wal.checkpoint`` and ``<base>.shadow`` next to the database
        (renamed ``.corrupt-YYYYMMDD`` / ``.stale-YYYYMMDD``) lets the main
        graph file reopen on the retry, exactly as the manual recovery does.

        Scope is deliberately *exactly* those two WAL-adjacent files: the main
        database file and the ``.wal`` replay journal are never touched or
        deleted — over-eager healing of those once wiped the live knowledge
        graph (the kuzu-era self-heal, 2026-08-01).  One-shot per process
        (``_wal_heal_attempted``) so a store that stays broken cannot loop.

        Returns True if at least one sidecar was stashed (a retry is worth
        attempting); False otherwise (nothing to heal — propagate the error).
        """
        if self._wal_heal_attempted:
            return False
        self._wal_heal_attempted = True
        date_suffix = time.strftime("%Y%m%d")
        dbs = self._ladybug_databases_dir()
        if not dbs.is_dir():
            return False
        # Derive the graph base names from whatever ladybug sidecars exist so
        # the recovery stays in lock-step with the engine's actual layout.
        try:
            bases: set[str] = set()
            for entry in dbs.iterdir():
                name = entry.name
                if name.endswith(".wal.checkpoint"):
                    bases.add(name[: -len(".wal.checkpoint")])
                elif name.endswith(".shadow"):
                    bases.add(name[: -len(".shadow")])
        except OSError:
            logger.exception("Failed to scan %s for ladybug WAL sidecars", dbs)
            return False

        stashed: list[Path] = []
        try:
            for base in sorted(bases):
                checkpoint = dbs / f"{base}.wal.checkpoint"
                shadow = dbs / f"{base}.shadow"
                # `.corrupt-YYYYMMDD` for the checkpoint marker, `.stale-YYYYMMDD`
                # for the shadow pre-image, matching the manual recovery naming.
                for src, tag in (
                    (checkpoint, "corrupt"),
                    (shadow, "stale"),
                ):
                    if src.is_file():
                        dst = dbs / f"{src.name}-{tag}-{date_suffix}"
                        src.rename(dst)
                        stashed.append(dst)
        except OSError:
            logger.exception(
                "Failed to stash ladybug WAL sidecars in %s — manual recovery "
                "may be required",
                dbs,
            )
            return bool(stashed)
        if stashed:
            logger.warning(
                "Ladybug graph-store WAL corruption detected — stashed corrupt "
                "WAL sidecars: %s. Retrying the graph store open once; the main "
                "graph database file was NOT touched.",
                ", ".join(str(p) for p in sorted(stashed, key=str)),
            )
            return True
        return False

    # -- graph-open segfault auto-recovery (validated-copy) ---------------

    async def _run_graph_segv_recovery(self, reason: str) -> None:
        """Attempt the validated-copy recovery, then clear backoff or escalate.

        Never raises: a recovery that itself fails must not break the chat
        path.  On success the corrupt ``.wal`` has been stashed aside and the
        backoff is cleared so the next recall re-opens the (now consistent)
        store; on failure the fault is escalated via ``notify_user`` and the
        store stays degraded.
        """
        self._segv_heal_attempted = True
        try:
            healed = await asyncio.to_thread(self._attempt_graph_segv_recovery)
        except Exception:
            logger.exception("graph-store segfault recovery raised unexpectedly")
            healed = False
        if healed:
            # The main graph file opened cleanly once the corrupt .wal was
            # stashed — let the next recall retry against the healed store.
            self._segv_backoff_until = None
            self._consecutive_graph_segv_failures = 0
            logger.warning(
                "graph-store segfault recovery stashed the corrupt .wal; the "
                "next recall will retry the store open"
            )
        else:
            await self._escalate_graph_segv(reason)

    def _attempt_graph_segv_recovery(self) -> bool:
        """Validated-copy recovery for a graph-open segfault (runs in a thread).

        Copy the main graph file to scratch, probe-open the COPY *without* its
        ``.wal`` in a subprocess (the probe may itself segfault), and only if
        the copy opens cleanly and ``MATCH (n) RETURN count(n)`` succeeds,
        stash the LIVE ``.wal`` aside as ``<base>.wal.corrupt-segv-YYYYMMDD``.

        The main graph file and the ``.wal`` are never deleted; the ``.wal`` is
        only *moved*, and only after the copy proves the main file is at a
        consistent checkpoint (respecting the 2026-08-01 lesson: never
        blind-heal the main file/.wal).  The durable write backlog re-ingests
        the lost transaction afterwards.

        Returns True if the corrupt ``.wal`` was stashed (a retry is worth
        attempting); False otherwise (nothing was safely healed — escalate).
        """
        dbs = self._ladybug_databases_dir()
        if not dbs.is_dir():
            return False
        main = dbs / _LADYBUG_GRAPH_BASENAME
        wal = dbs / f"{_LADYBUG_GRAPH_BASENAME}.wal"
        if not main.is_file() or not wal.is_file():
            # No main file, or no live .wal to stash: not the unreplayable-WAL
            # case, so there is nothing safe to do here.
            return False
        scratch = dbs / f".segv-probe-{time.strftime('%Y%m%d%H%M%S')}"
        copy_path = scratch / main.name
        try:
            scratch.mkdir(parents=True, exist_ok=True)
            # Copy ONLY the main file — the probe must open it *without* the
            # .wal, exactly as the manual recovery validated the checkpoint.
            shutil.copy2(main, copy_path)
        except OSError:
            logger.exception(
                "graph-store segfault recovery: failed to copy the main graph "
                "file for validation"
            )
            shutil.rmtree(scratch, ignore_errors=True)
            return False
        try:
            opened = probe_graph_store_opens(copy_path)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if not opened:
            logger.error(
                "graph-store segfault recovery: the COPY did NOT open cleanly "
                "— the main graph file is not at a consistent checkpoint; not "
                "touching any files, escalating"
            )
            return False
        # The copy opened cleanly: the main file is consistent, so the live
        # .wal is the corrupt sidecar.  Move it aside (never delete).
        dst = dbs / f"{wal.name}.corrupt-segv-{time.strftime('%Y%m%d')}"
        try:
            wal.rename(dst)
        except OSError:
            logger.exception(
                "graph-store segfault recovery: failed to stash the corrupt .wal"
            )
            return False
        logger.warning(
            "graph-store segfault recovery: copy opened cleanly (MATCH (n) "
            "count succeeded); stashed the live corrupt .wal as %s. The main "
            "graph file was NOT touched; the write backlog will re-ingest the "
            "lost transaction.",
            dst,
        )
        return True

    async def _escalate_graph_segv(self, reason: str) -> None:
        """Escalate an unhealable graph-open segfault via ``notify_user``."""
        diagnosis = (
            "Long-term memory is down: the graph-store worker keeps "
            "segfaulting while opening the store (exit -11) and auto-recovery "
            "could not safely heal it — the on-disk copy did not open cleanly, "
            "so no files were touched. Manual recovery is required. "
            f"Diagnosis: {reason}"
        )
        logger.error(
            "graph-store segfault recovery could not heal the store: %s", diagnosis
        )
        cb = self._notify_cb
        if cb is None:
            return
        try:
            await cb("Memory store down (graph segfault)", diagnosis)
        except Exception:
            logger.exception("failed to escalate graph-store segfault via notify_user")

    # -- durable backlog --------------------------------------------------

    def _append_to_backlog(
        self,
        user_message: str,
        assistant_message: str,
        session_id: str | None,
    ) -> None:
        """Persist a failed exchange to the durable JSONL backlog.

        The entry is written atomically (append + fsync) so it survives a
        process crash.  On success the caller must invoke ``_drain_backlog``
        to re-process backlogged entries.
        """
        path = Path(self._settings.write_backlog_path)
        entry = json.dumps(
            {
                "user_message": user_message,
                "assistant_message": assistant_message,
                "session_id": session_id,
                "timestamp": time.time(),
            },
            ensure_ascii=False,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(entry + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError:
            logger.exception(
                "Failed to write backlog entry to %s — exchange lost", path
            )

    async def _drain_backlog(self) -> None:
        """Re-process backlogged exchanges opportunistically.

        Called after every successful write.  Reads the entire backlog,
        rewrites each entry through ``_remember_core``, and trims consumed
        entries.  If a backlog entry fails again it stays in the file (the
        drain is best-effort — a persistent-failure freeze is surfaced via
        ``_record_write_failure``).

        Serialised by ``_drain_lock`` so overlapping calls cannot silently
        drop entries or replay duplicates.  To eliminate a TOCTOU race with
        ``_append_to_backlog``, the backlog file is atomically *renamed* to a
        snapshot before processing; still-failing entries are then appended
        (not overwritten) to the original path, so entries queued by
        concurrent failing writes while this drain is in flight are preserved.

        Note: the ``write_throttle_seconds`` delay inside ``_remember_core``
        applies to every successful drain replay, so a large backlog can take
        minutes to drain.  This matches the opportunistic design intent:
        backlog recovery is paced so it does not overwhelm the worker.
        """
        async with self._drain_lock:
            path = Path(self._settings.write_backlog_path)
            snapshot = path.with_suffix(path.suffix + ".drain")

            if not path.exists():
                # Recover an orphaned .drain snapshot from a prior crash
                # mid-drain (the backlog was already renamed away).
                if not snapshot.exists():
                    return
                # Fall through to process the recovered snapshot — skip the
                # rename because the snapshot already exists.
            else:
                # Atomically consume the backlog so concurrent
                # _append_to_backlog calls never have their entries
                # clobbered by this drain's final write.
                try:
                    path.rename(snapshot)
                except OSError:
                    return

            try:
                lines = snapshot.read_text(encoding="utf-8").splitlines()
            except OSError:
                snapshot.unlink(missing_ok=True)
                return

            if not lines:
                snapshot.unlink(missing_ok=True)
                return

            remaining: list[str] = []
            drained = 0
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    async with asyncio.timeout(self._settings.remember_timeout_seconds):
                        await self._remember_core(
                            entry["user_message"],
                            entry["assistant_message"],
                            session_id=entry.get("session_id"),
                        )
                    drained += 1
                except Exception:
                    # Track the failure for frozen-store detection so
                    # permanently-unwritable backlog entries eventually
                    # trigger the alert (not just live write failures).
                    self._record_write_failure()
                    remaining.append(line)

            if drained:
                logger.info("Backlog drain: %d exchanges recovered", drained)

            # Append still-failing entries back to the (possibly recreated)
            # backlog file.  We append rather than overwrite so entries
            # queued by concurrent failing writes while this drain was in
            # flight are preserved.  The append+fsync is safe for line-
            # oriented JSONL.
            try:
                if remaining:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as fh:
                        for line in remaining:
                            fh.write(line + "\n")
                        fh.flush()
                        os.fsync(fh.fileno())
            except OSError:
                logger.exception("Failed to update backlog file %s", path)
            finally:
                snapshot.unlink(missing_ok=True)


def _result_text(item: Any) -> str:
    """Extract the recallable text from one cognee search result.

    CHUNKS-type searches return dicts whose payload is the ``text`` field
    wrapped in ~20 IndexSchema metadata keys (ids, timestamps, weights,
    document names). Dumping the raw dict repr into the recall block buries
    the memory under metadata noise and surfaces stale identifiers the
    prompt fencing then has to argue against (2026-09-01, trace f7749180:
    a 5-chunk recall weighed ~6KB, ~85% of it metadata, and the stale ids
    helped derail the turn). Unknown shapes still fall back to ``str`` so
    other search types lose nothing.
    """
    if isinstance(item, dict):
        for key in ("text", "content", "answer"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(item).strip()


def _format_results(results: Any) -> str:
    """Flatten cognee search results into a single bounded context string."""
    if not results:
        return ""
    if isinstance(results, str):
        text = results
    elif isinstance(results, list | tuple):
        text = "\n".join(_result_text(item) for item in results if item)
    else:
        text = _result_text(results)
    text = text.strip()
    if len(text) > _MAX_RECALL_CHARS:
        text = text[:_MAX_RECALL_CHARS].rstrip() + "…"
    return text
