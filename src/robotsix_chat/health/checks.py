"""Individual health-check functions — one per critical subsystem.

Every check is an async callable that accepts the app state (or the
specific subsystem it needs) and returns a :class:`CheckResult`.

Checks MUST NOT raise — they catch and convert exceptions into
``CheckSeverity.ERROR`` results so one failing check never breaks the
aggregate cycle.
"""

from __future__ import annotations

import logging
import time
from typing import Any, cast

from robotsix_chat.health.models import CheckResult, CheckSeverity

logger = logging.getLogger(__name__)

# cgroup v2 memory accounting files (present in the deploy container).
# Module-level so tests can point them at fixtures.
_MEMORY_CURRENT_PATH = "/sys/fs/cgroup/memory.current"
_MEMORY_MAX_PATH = "/sys/fs/cgroup/memory.max"

# Fallback warning threshold when no HealthSettings are attached to state.
_DEFAULT_MEMORY_WARN_FRACTION = 0.85


async def check_container_memory(state: Any) -> CheckResult:
    """Warn *before* the container is OOM-killed by its cgroup limit.

    Reads the cgroup v2 memory accounting files and compares current
    usage against the container's hard limit.  Flips to ``WARNING`` once
    usage reaches ``health.memory_warn_fraction`` of the limit (default
    85 %) so the scheduler logs a pre-OOM alert and the operator is
    notified *before* the OOM killer fires, not after (incident
    2026-09-02: chat climbed from ~400 MiB to its 4 GiB limit and was
    OOM-killed with no prior signal).

    Returns ``OK`` — never raises — when accounting is unavailable
    (non-cgroup-v2 host) or no hard limit is set (``memory.max=max``).
    """
    warn_fraction = _DEFAULT_MEMORY_WARN_FRACTION
    settings = getattr(state, "health_settings", None)
    if settings is not None:
        warn_fraction = getattr(
            settings, "memory_warn_fraction", _DEFAULT_MEMORY_WARN_FRACTION
        )

    try:
        with open(_MEMORY_MAX_PATH, encoding="utf-8") as fh:
            raw_max = fh.read().strip()
        with open(_MEMORY_CURRENT_PATH, encoding="utf-8") as fh:
            raw_current = fh.read().strip()
    except OSError:
        return CheckResult(
            name="container_memory",
            status=CheckSeverity.OK,
            message="Container memory accounting unavailable (no cgroup v2)",
        )

    # "max" means the cgroup imposes no hard limit — nothing to warn about.
    if raw_max == "max":
        return CheckResult(
            name="container_memory",
            status=CheckSeverity.OK,
            message="No container memory limit set (cgroup memory.max=max)",
        )

    try:
        limit_bytes = int(raw_max)
        current_bytes = int(raw_current)
    except ValueError:
        return CheckResult(
            name="container_memory",
            status=CheckSeverity.OK,
            message="Container memory accounting unreadable",
        )

    if limit_bytes <= 0:
        return CheckResult(
            name="container_memory",
            status=CheckSeverity.OK,
            message="Container memory limit is non-positive; skipping check",
        )

    fraction = current_bytes / limit_bytes
    current_mib = current_bytes >> 20
    limit_mib = limit_bytes >> 20
    details: dict[str, object] = {
        "current_bytes": current_bytes,
        "limit_bytes": limit_bytes,
        "fraction": round(fraction, 4),
        "warn_fraction": warn_fraction,
    }

    if fraction >= warn_fraction:
        return CheckResult(
            name="container_memory",
            status=CheckSeverity.WARNING,
            message=(
                f"Container memory at {fraction * 100:.1f}% of limit "
                f"({current_mib} MiB / {limit_mib} MiB) — approaching OOM"
            ),
            details=details,
        )

    return CheckResult(
        name="container_memory",
        status=CheckSeverity.OK,
        message=(
            f"Container memory at {fraction * 100:.1f}% of limit "
            f"({current_mib} MiB / {limit_mib} MiB)"
        ),
        details=details,
    )


async def check_memory(state: Any) -> CheckResult:
    """Verify the cognee memory backend is not degraded.

    Uses the existing ``memory.status()`` protocol — every memory
    backend (cognee, null, future) must implement it.
    """
    memory = getattr(state, "memory", None)
    if memory is None:
        return CheckResult(
            name="memory",
            status=CheckSeverity.WARNING,
            message="No memory backend attached to app.state",
            details={"backend": "none"},
        )

    try:
        status_fn = getattr(memory, "status", None)
        if not callable(status_fn):
            return CheckResult(
                name="memory",
                status=CheckSeverity.WARNING,
                message="Memory backend has no status() method",
                details={"backend": type(memory).__name__},
            )

        snapshot = cast("dict[str, Any]", status_fn())
        if not isinstance(snapshot, dict):
            return CheckResult(
                name="memory",
                status=CheckSeverity.WARNING,
                message="Memory status() returned non-dict",
                details={"backend": type(memory).__name__},
            )

        degraded = snapshot.get("degraded", False)
        backend = snapshot.get("backend", type(memory).__name__)

        if degraded:
            return CheckResult(
                name="memory",
                status=CheckSeverity.ERROR,
                message=f"Memory backend {backend!r} is degraded",
                details=snapshot,
            )

        return CheckResult(
            name="memory",
            status=CheckSeverity.OK,
            message=f"Memory backend {backend!r} is healthy",
            details=snapshot,
        )
    except Exception:
        logger.debug("health check: memory status raised", exc_info=True)
        return CheckResult(
            name="memory",
            status=CheckSeverity.ERROR,
            message="Memory status check raised an exception",
            details={"backend": type(memory).__name__},
        )


async def check_knowledge_store(state: Any) -> CheckResult:
    """Verify the knowledge store is reachable and responsive.

    Performs a lightweight read (``list()``) — no mutation.
    """
    store = getattr(state, "knowledge_store", None)
    if store is None:
        return CheckResult(
            name="knowledge_store",
            status=CheckSeverity.WARNING,
            message="No knowledge store attached to app.state",
        )

    try:
        list_fn = getattr(store, "list", None)
        if not callable(list_fn):
            return CheckResult(
                name="knowledge_store",
                status=CheckSeverity.WARNING,
                message="Knowledge store has no list() method",
                details={"store_type": type(store).__name__},
            )

        start = time.monotonic()
        notes = list_fn()
        elapsed = time.monotonic() - start

        return CheckResult(
            name="knowledge_store",
            status=CheckSeverity.OK,
            message=f"Knowledge store responsive ({len(notes)} notes, {elapsed:.3f}s)",
            details={"note_count": len(notes), "elapsed_seconds": elapsed},
        )
    except Exception:
        logger.debug("health check: knowledge store raised", exc_info=True)
        return CheckResult(
            name="knowledge_store",
            status=CheckSeverity.ERROR,
            message="Knowledge store list() raised an exception",
            details={"store_type": type(store).__name__},
        )


async def check_feedback_runner(state: Any) -> CheckResult:
    """Verify the feedback runner is configured and reachable.

    A feedback runner that is disabled (``enabled=False``) is ``OK``,
    not ``WARNING`` — the operator deliberately turned it off.
    """
    runner = getattr(state, "feedback_runner", None)
    if runner is None:
        return CheckResult(
            name="feedback_runner",
            status=CheckSeverity.OK,
            message="No feedback runner attached (disabled or not configured)",
        )

    # If a runner is attached, it should have a board_url indicating
    # whether it is actually enabled.
    board_url = getattr(runner, "_board_url", "")
    if not board_url:
        return CheckResult(
            name="feedback_runner",
            status=CheckSeverity.OK,
            message="Feedback runner attached but disabled (no board_url)",
        )

    # Check whether the runner has been active recently by inspecting
    # its private dedup caches (best-effort — no public API yet).
    last_run_at = getattr(runner, "_last_run_at", {})
    last_filed_at = getattr(runner, "_last_filed_at", {})

    details: dict[str, object] = {
        "board_url": board_url,
        "run_count": len(last_run_at),
        "filed_count": len(last_filed_at),
    }

    # A healthy runner should have at least one entry in its dedup
    # cache — but a freshly started process has none, which is fine.
    if not last_run_at and not last_filed_at:
        return CheckResult(
            name="feedback_runner",
            status=CheckSeverity.OK,
            message="Feedback runner is configured and ready (no runs yet)",
            details=details,
        )

    return CheckResult(
        name="feedback_runner",
        status=CheckSeverity.OK,
        message="Feedback runner is configured and has been active",
        details=details,
    )


async def check_diagnostics_store(state: Any) -> CheckResult:
    """Verify the diagnostic event store is reachable.

    A missing diagnostics store is ``OK`` — it is optional.
    """
    store = getattr(state, "diagnostic_store", None)
    if store is None:
        return CheckResult(
            name="diagnostics_store",
            status=CheckSeverity.OK,
            message="No diagnostic store attached (optional)",
        )

    try:
        list_fn = getattr(store, "list_events", None)
        if not callable(list_fn):
            return CheckResult(
                name="diagnostics_store",
                status=CheckSeverity.WARNING,
                message="Diagnostic store has no list_events() method",
                details={"store_type": type(store).__name__},
            )

        start = time.monotonic()
        events = list_fn()
        elapsed = time.monotonic() - start

        return CheckResult(
            name="diagnostics_store",
            status=CheckSeverity.OK,
            message=(
                f"Diagnostic store responsive ({len(events)} events, {elapsed:.3f}s)"
            ),
            details={"event_count": len(events), "elapsed_seconds": elapsed},
        )
    except Exception:
        logger.debug("health check: diagnostics store raised", exc_info=True)
        return CheckResult(
            name="diagnostics_store",
            status=CheckSeverity.ERROR,
            message="Diagnostic store list_events() raised an exception",
            details={"store_type": type(store).__name__},
        )


# Ordered list of checks executed in every cycle.  Each entry is
# ``(check_name, check_fn)`` — the name is for logging / alerting.
CHECKS: list[tuple[str, Any]] = [
    ("container_memory", check_container_memory),
    ("memory", check_memory),
    ("knowledge_store", check_knowledge_store),
    ("feedback_runner", check_feedback_runner),
    ("diagnostics_store", check_diagnostics_store),
]
