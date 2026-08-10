"""Periodic health checks for critical subsystems.

Runs at session start and on a configurable interval to verify that
emitters (feedback runner), memory layers (cognee recall + knowledge
store), and diagnostic stores are active and producing expected output.
"""

from __future__ import annotations

from robotsix_chat.health.models import CheckResult, CheckSeverity, HealthStatus
from robotsix_chat.health.scheduler import HealthScheduler

__all__ = [
    "CheckResult",
    "CheckSeverity",
    "HealthScheduler",
    "HealthStatus",
]
