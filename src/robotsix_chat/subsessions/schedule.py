"""Absolute time-anchor scheduling for periodic subsessions.

Periodic subsessions default to a *relative* cadence: the first run fires
at spawn time and each subsequent run is scheduled ``interval_seconds``
after the previous one completes.  That leaves the landing time at the
mercy of spawn time and lets small per-turn delays accumulate into drift.

An **anchor** pins recurrences to an absolute wall-clock time
(e.g. "run every day at 09:00 UTC").  When an anchor is set the scheduler
computes the next fire time as the next occurrence of the anchored
time-of-day that is strictly in the future, phase-aligned to the interval,
instead of ``now + interval``.  Because every fire time is derived from a
fixed reference (the anchored time-of-day) rather than the previous fire
time, cumulative drift is eliminated.

DST behaviour
-------------
For whole-day intervals (``interval_seconds`` a positive multiple of
86400) the wall-clock time-of-day is held constant across daylight-saving
transitions — a 09:00 daily anchor stays at 09:00 *local* time before and
after the change, so the real elapsed time between two runs is 23h or 25h
across a transition rather than exactly 24h.

For sub-day intervals (or intervals that are not a whole-day multiple) the
schedule is phase-aligned on the epoch to the anchored time-of-day, so the
*duration* between runs is held constant; the local time-of-day may shift
by the DST offset across a transition.

A nonexistent local time — an anchor whose time-of-day falls inside a
spring-forward gap (e.g. ``"02:30 Europe/Paris"`` on the transition date) —
cannot be represented in that zone.  It is resolved silently via the tz
database's ``fold`` rules (the default ``fold=0`` uses the pre-transition
UTC offset) rather than raising, so the recurrence fires at the instant
that skipped wall time maps to; this matches Python's standard
``datetime`` behaviour for such times.

This module is pure (no asyncio, no registry) so it can be unit-tested in
isolation and imported by both the spawn-validation layer (``worker``) and
the post-turn scheduler (``worker_periodic``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import SubsessionAnchorError

__all__ = ["ParsedAnchor", "next_anchored_run_at", "parse_anchor_time"]

_SECONDS_PER_DAY = 86400


@dataclass(frozen=True)
class ParsedAnchor:
    """A validated anchor spec: a time-of-day plus an IANA timezone."""

    hour: int
    minute: int
    second: int
    tz: str  # IANA zone name (e.g. "UTC", "Europe/Paris")


def parse_anchor_time(anchor_time: str, *, default_tz: str = "UTC") -> ParsedAnchor:
    """Parse an anchor spec into hour/minute/second plus an IANA timezone.

    Accepted forms (surrounding whitespace tolerated):

    * ``"HH:MM"``               — time-of-day in *default_tz*
    * ``"HH:MM:SS"``            — with an explicit seconds component
    * ``"HH:MM Europe/Paris"``  — with an explicit IANA timezone
    * ``"HH:MM:SS UTC"``

    Raises :class:`SubsessionAnchorError` on any malformed spec (bad shape,
    out-of-range field, or unknown timezone) so the spawn layer can surface
    a polite refusal instead of crashing a worker later.
    """
    if not isinstance(anchor_time, str) or not anchor_time.strip():
        raise SubsessionAnchorError(
            "anchor_time must be a non-empty 'HH:MM[:SS] [timezone]' string"
        )
    parts = anchor_time.strip().split()
    if len(parts) > 2:
        raise SubsessionAnchorError(
            f"anchor_time {anchor_time!r} is not a valid 'HH:MM[:SS] [timezone]' spec"
        )
    time_part = parts[0]
    tz = parts[1] if len(parts) == 2 else default_tz
    fields = time_part.split(":")
    if len(fields) not in (2, 3):
        raise SubsessionAnchorError(
            f"anchor_time {anchor_time!r} must be 'HH:MM' or 'HH:MM:SS'"
        )
    try:
        hour = int(fields[0])
        minute = int(fields[1])
        second = int(fields[2]) if len(fields) == 3 else 0
    except ValueError:
        raise SubsessionAnchorError(
            f"anchor_time {anchor_time!r} has non-numeric time fields"
        ) from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise SubsessionAnchorError(
            f"anchor_time {anchor_time!r} is out of range "
            "(expected HH in 0..23, MM/SS in 0..59)"
        )
    try:
        ZoneInfo(tz)
    except ZoneInfoNotFoundError, ValueError, OSError:
        raise SubsessionAnchorError(
            f"anchor_time {anchor_time!r} names an unknown timezone {tz!r} "
            "(expected an IANA zone name such as 'UTC' or 'Europe/Paris')"
        ) from None
    return ParsedAnchor(hour=hour, minute=minute, second=second, tz=tz)


def next_anchored_run_at(
    now_epoch: float,
    interval_seconds: float,
    anchor_time: str,
    *,
    default_tz: str = "UTC",
) -> float:
    """Return the next fire epoch (UNIX seconds) for an anchored schedule.

    The result is the smallest instant strictly after *now_epoch* whose
    wall-clock time-of-day matches *anchor_time* and which is phase-aligned
    to *interval_seconds*.  See the module docstring for DST semantics.

    Raises :class:`SubsessionAnchorError` when *anchor_time* is malformed.
    """
    parsed = parse_anchor_time(anchor_time, default_tz=default_tz)
    tzinfo = ZoneInfo(parsed.tz)
    now_dt = datetime.fromtimestamp(now_epoch, tz=tzinfo)
    anchor_today = now_dt.replace(
        hour=parsed.hour,
        minute=parsed.minute,
        second=parsed.second,
        microsecond=0,
    )
    whole_day = (
        interval_seconds >= _SECONDS_PER_DAY
        and interval_seconds % _SECONDS_PER_DAY == 0
    )
    if whole_day:
        # Whole-day cadence: step by calendar days so the wall-clock
        # time-of-day is preserved across DST transitions.
        step_days = int(interval_seconds // _SECONDS_PER_DAY)
        candidate = anchor_today
        while candidate.timestamp() <= now_epoch:
            naive = candidate.replace(tzinfo=None) + timedelta(days=step_days)
            candidate = naive.replace(tzinfo=tzinfo)
        return candidate.timestamp()
    # Sub-day (or non-day-multiple) cadence: phase-align on the epoch to the
    # anchored time-of-day so successive runs keep a constant duration.
    base = anchor_today.timestamp()
    steps = math.ceil((now_epoch - base) / interval_seconds)
    nxt = base + steps * interval_seconds
    while nxt <= now_epoch:
        nxt += interval_seconds
    return nxt
