"""Data models for health-check results.

Kept lightweight (dataclasses) so they stay decoupled from the
Pydantic config cascade.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum


class CheckSeverity(StrEnum):
    """Severity of a single health check.

    ``OK``     — subsystem is healthy, no action needed.
    ``WARNING`` — degraded but still functional (e.g. elevated latency).
    ``ERROR``   — subsystem is unreachable or not producing expected output.
    """

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class CheckResult:
    """Result of one subsystem health check.

    Attributes:
        name: Human-readable check name (e.g. ``"memory"``).
        status: Severity — ``OK``, ``WARNING``, or ``ERROR``.
        message: One-line summary for logs / UIs.
        details: Optional dict of check-specific fields (e.g. degraded reason).
        timestamp: ``time.monotonic()`` when the check completed.

    """

    name: str
    status: CheckSeverity
    message: str = ""
    details: dict[str, object] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class HealthStatus:
    """Aggregate health snapshot, updated after every check cycle.

    Attributes:
        checks: Per-subsystem results from the most recent cycle.
        last_run: ``time.monotonic()`` of the last completed cycle.
        overall: Worst severity across all checks — drives alerting.

    """

    checks: list[CheckResult] = field(default_factory=list)
    last_run: float = 0.0
    overall: CheckSeverity = CheckSeverity.OK
