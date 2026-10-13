"""Periodic session scheduler — fire a preset, get an ordinary session.

The scheduler is deliberately small. On each tick it checks every enabled
preset; when one is due it

1. creates a NEW plain session under the ``periodic`` owner (title
   ``"<preset> — <UTC date>"``),
2. posts the preset's initial prompt (behind the shared preamble) through the
   SAME submit path an operator message takes, and
3. records the firing in its own state file.

That is all. There is no per-session execution state, no self-scheduled
continuation, no restart-resume: a chat restart mid-turn fails that turn the
way it would fail an operator's, and the next firing starts fresh. A human
typing into a periodic session later is just… using a session.

If a preset comes due while its previous session's turn is still being
processed, the firing is skipped with a log line (no queueing).

A run's session is closed once its turn COMPLETES (its report is delivered
and no live subsession is still working) through the injected
``close_completed`` callback — the same path as ``POST /sessions/{id}/close``
(subsessions cleaned up, feedback run, memory finalised) — so periodic runs
do not pile up as open sessions for the whole interval. A firing also
SUPERSEDES the preset's previous run: once the new session exists the
previous run's session is closed through the injected ``close_previous``
callback, a safety net for runs that crash without completing. Before that
close, the previous run's LIVE ``user_chat`` decision panels (questions the
operator has not answered yet) are handed over to the new session through
the injected ``carry_over_subsessions`` callback, so a pending decision
survives from firing to firing instead of being killed and re-asked.

Closed runs do not linger either: the scheduler remembers the last
``RETAINED_RUNS_PER_PRESET`` session ids of each preset and, after every
firing (and once at startup), hands every other closed periodic session to
the injected ``prune_closed_runs`` callback, which deletes it from the
conversation store. The sidebar therefore shows at most a handful of recent
runs per preset instead of every run since the install.

A run's session also gets a failsafe check to ensure a PARTIAL REPORT is
present in the transcript, even if the turn ended cleanly (without raising)
but the agent never emitted one — token exhaustion or a provider cutoff can
end a turn without an error. This ensures the next firing always has context
about what was attempted.

CRITICAL: Subsession visibility is now independent of main-task success.
If the periodic turn fails before the agent executes its subsession-check
operation, a fallback in the exception handler ensures any open user_chat
subsessions (operator decisions awaiting reply) are still surfaced in the
transcript, so the operator sees them even if the main task crashes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robotsix_chat.config.periodic_models import PeriodicSessionDefinition
from robotsix_chat.subsessions.registry import OWNER_CLOSED_REASON

from .prompts import build_initial_message

logger = logging.getLogger(__name__)

#: The single owner id every periodic session lives under.
PERIODIC_OWNER = "periodic"

#: Where the scheduler persists per-preset firing state.
PERIODIC_SCHEDULER_PERSIST_PATH = "/data/periodic_scheduler_state.json"

#: How often the scheduler loop checks for due presets.
_TICK_SECONDS = 30.0

#: How many runs (open or closed) of each preset survive pruning. The
#: current run plus the previous ones — enough to read back what the last
#: couple of firings did, while the memory component keeps the long tail.
RETAINED_RUNS_PER_PRESET = 3
