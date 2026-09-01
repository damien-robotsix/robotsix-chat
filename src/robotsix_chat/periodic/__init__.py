"""Periodic chat sessions — ordinary sessions started on a schedule.

A scheduler fires each configured preset on its interval: it creates a fresh
plain session under the ``periodic`` owner and posts the preset's initial
prompt through the same code path as an operator message. Nothing else is
special about these sessions.
"""

from .prompts import PERIODIC_PREAMBLE, build_initial_message
from .scheduler import (
    PERIODIC_OWNER,
    PERIODIC_SCHEDULER_PERSIST_PATH,
    PeriodicScheduler,
)

__all__ = [
    "PERIODIC_OWNER",
    "PERIODIC_PREAMBLE",
    "PERIODIC_SCHEDULER_PERSIST_PATH",
    "PeriodicScheduler",
    "build_initial_message",
]
