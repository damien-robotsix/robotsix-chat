"""Tests for the absolute time-anchor scheduler (``schedule.py``)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from robotsix_chat.subsessions.models import SubsessionAnchorError
from robotsix_chat.subsessions.schedule import (
    ParsedAnchor,
    next_anchored_run_at,
    parse_anchor_time,
)

_DAY = 86400.0


def _epoch(iso: str, tz: str = "UTC") -> float:
    """Return the UNIX epoch for a naive ISO datetime interpreted in *tz*."""
    return datetime.fromisoformat(iso).replace(tzinfo=ZoneInfo(tz)).timestamp()


# -- parse_anchor_time ----------------------------------------------------


def test_parse_hh_mm_defaults_to_utc() -> None:
    assert parse_anchor_time("09:00") == ParsedAnchor(9, 0, 0, "UTC")


def test_parse_hh_mm_ss() -> None:
    assert parse_anchor_time("09:30:15") == ParsedAnchor(9, 30, 15, "UTC")


def test_parse_explicit_timezone() -> None:
    assert parse_anchor_time("09:00 Europe/Paris") == ParsedAnchor(
        9, 0, 0, "Europe/Paris"
    )


def test_parse_explicit_default_tz() -> None:
    assert parse_anchor_time("07:00", default_tz="Europe/Paris") == ParsedAnchor(
        7, 0, 0, "Europe/Paris"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "9",
        "9:00:00:00",
        "24:00",
        "09:60",
        "09:00:61",
        "ab:cd",
        "09:00 Not/AZone",
        "09:00 UTC extra",
    ],
)
def test_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(SubsessionAnchorError):
        parse_anchor_time(bad)


# -- next_anchored_run_at: daily ------------------------------------------


def test_daily_anchor_same_day_when_future() -> None:
    now = _epoch("2026-09-04T06:00:00")
    nxt = next_anchored_run_at(now, _DAY, "09:00")
    assert nxt == _epoch("2026-09-04T09:00:00")


def test_daily_anchor_next_day_when_past() -> None:
    now = _epoch("2026-09-04T14:00:00")
    nxt = next_anchored_run_at(now, _DAY, "09:00")
    assert nxt == _epoch("2026-09-05T09:00:00")


def test_daily_anchor_exactly_now_advances() -> None:
    now = _epoch("2026-09-04T09:00:00")
    nxt = next_anchored_run_at(now, _DAY, "09:00")
    # Strictly after now → tomorrow, never the current instant.
    assert nxt == _epoch("2026-09-05T09:00:00")


def test_daily_anchor_no_drift_across_runs() -> None:
    # Each run lands slightly late; the next fire must still be the exact
    # anchor, not anchor + accumulated lateness.
    anchor = "09:00"
    now = _epoch("2026-09-04T09:00:37")  # 37s late
    nxt = next_anchored_run_at(now, _DAY, anchor)
    assert nxt == _epoch("2026-09-05T09:00:00")


def test_weekly_anchor_steps_seven_days() -> None:
    now = _epoch("2026-09-04T10:00:00")
    nxt = next_anchored_run_at(now, 7 * _DAY, "09:00")
    assert nxt == _epoch("2026-09-11T09:00:00")


# -- next_anchored_run_at: sub-day ----------------------------------------


def test_sub_day_interval_phase_aligned_to_anchor() -> None:
    # 6h interval anchored at 00:00 → fires at 00:00, 06:00, 12:00, 18:00.
    now = _epoch("2026-09-04T13:30:00")
    nxt = next_anchored_run_at(now, 6 * 3600, "00:00")
    assert nxt == _epoch("2026-09-04T18:00:00")


def test_sub_day_interval_strictly_future() -> None:
    now = _epoch("2026-09-04T12:00:00")
    nxt = next_anchored_run_at(now, 6 * 3600, "00:00")
    assert nxt == _epoch("2026-09-04T18:00:00")


# -- DST ------------------------------------------------------------------


def test_daily_anchor_preserves_wall_clock_across_dst() -> None:
    # Europe/Paris springs forward 2026-03-29 (02:00 -> 03:00). A 09:00
    # daily anchor must stay at 09:00 local before and after the change.
    tz = "Europe/Paris"
    now = _epoch("2026-03-28T06:00:00", tz)  # before 09:00 on the 28th
    first = next_anchored_run_at(now, _DAY, "09:00 " + tz)
    assert first == _epoch("2026-03-28T09:00:00", tz)  # still CET
    second = next_anchored_run_at(first, _DAY, "09:00 " + tz)
    assert second == _epoch("2026-03-29T09:00:00", tz)  # now CEST, still 09:00
    # Wall clock held at 09:00 — so the real gap across the spring-forward
    # night is 23h, not 24h.
    assert (second - first) == 23 * 3600


def test_malformed_anchor_raises_in_next() -> None:
    with pytest.raises(SubsessionAnchorError):
        next_anchored_run_at(_epoch("2026-09-04T10:00:00"), _DAY, "nope")
