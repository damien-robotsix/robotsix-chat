"""Tests for the periodic session scheduler.

The scheduler's contract: a due preset creates ONE fresh plain session under
the ``periodic`` owner and posts its initial prompt through the injected
submit path — nothing else. Skip-if-busy, interval accounting, and state
persistence are covered here; everything after the submit is ordinary
session behaviour owned by the chat turn path.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from robotsix_chat.config.periodic_models import PeriodicSessionDefinition
from robotsix_chat.periodic.prompts import PERIODIC_PREAMBLE
from robotsix_chat.periodic.scheduler import PERIODIC_OWNER, PeriodicScheduler


class _FakeStore:
    """Just enough ConversationStore for the scheduler."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.titles: dict[str, str] = {}
        self._n = 0

    def create_session(self, owner_id: str) -> dict[str, object]:
        assert owner_id == PERIODIC_OWNER
        self._n += 1
        sid = f"sess-{self._n}"
        self.created.append(sid)
        return {"session_id": sid, "title": "New chat"}

    def set_title(self, session_id: str, title: str) -> bool:
        self.titles[session_id] = title
        return True


def _make(
    tmp_path,
    *,
    definitions=None,
    busy=False,
    clock=None,
):
    store = _FakeStore()
    submitted: list[tuple[str, str, int | None]] = []

    async def submit_turn(session_id: str, message: str, model_level: int | None):
        submitted.append((session_id, message, model_level))

    now = {"t": 1000.0}
    scheduler = PeriodicScheduler(
        definitions=definitions
        or [
            PeriodicSessionDefinition(
                name="mail-triage",
                initial_prompt="Review the mail queue. READ-ONLY.",
                schedule_interval_seconds=3600,
                model_level=2,
            )
        ],
        conversation_store=store,
        submit_turn=submit_turn,
        is_busy=lambda sid: busy,
        persist_path=str(tmp_path / "state.json"),
        clock=clock or (lambda: now["t"]),
    )
    return scheduler, store, submitted, now


@pytest.mark.asyncio
async def test_fire_creates_titled_session_and_submits_prompt(tmp_path):
    scheduler, store, submitted, _ = _make(tmp_path)

    sid = await scheduler.fire("mail-triage")
    assert sid == "sess-1"
    assert store.created == ["sess-1"]
    assert store.titles["sess-1"].startswith("mail-triage — ")

    # Let the background turn task run.
    await asyncio.sleep(0)
    assert len(submitted) == 1
    got_sid, message, model_level = submitted[0]
    assert got_sid == "sess-1"
    assert message.startswith(PERIODIC_PREAMBLE)
    assert message.endswith("Review the mail queue. READ-ONLY.")
    assert model_level == 2


@pytest.mark.asyncio
async def test_fire_skips_while_previous_session_is_busy(tmp_path):
    scheduler, store, submitted, _ = _make(tmp_path, busy=True)
    # No previous session yet — busy check only applies once one exists.
    assert await scheduler.fire("mail-triage") == "sess-1"
    await asyncio.sleep(0)

    # The previous session now reports busy — the next firing is skipped.
    assert await scheduler.fire("mail-triage") is None
    assert store.created == ["sess-1"]
    assert len(submitted) == 1


@pytest.mark.asyncio
async def test_tick_fires_only_due_presets(tmp_path):
    defs = [
        PeriodicSessionDefinition(
            name="hourly", initial_prompt="a", schedule_interval_seconds=3600
        ),
        PeriodicSessionDefinition(
            name="daily", initial_prompt="b", schedule_interval_seconds=86400
        ),
    ]
    scheduler, store, submitted, now = _make(tmp_path, definitions=defs)

    await scheduler.tick()  # both never fired -> both due
    await asyncio.sleep(0)
    assert len(store.created) == 2

    now["t"] += 4000  # only the hourly preset is due again
    await scheduler.tick()
    await asyncio.sleep(0)
    assert len(store.created) == 3
    assert scheduler.state_for("hourly")["runs"] == 2
    assert scheduler.state_for("daily")["runs"] == 1


@pytest.mark.asyncio
async def test_disabled_presets_never_fire(tmp_path):
    defs = [PeriodicSessionDefinition(name="off", initial_prompt="x", enabled=False)]
    scheduler, store, _, _ = _make(tmp_path, definitions=defs)
    assert scheduler.definition_names == []
    await scheduler.tick()
    assert store.created == []


@pytest.mark.asyncio
async def test_state_persists_across_instances(tmp_path):
    scheduler, _, _, now = _make(tmp_path)
    await scheduler.fire("mail-triage")
    await asyncio.sleep(0)

    raw = json.loads((tmp_path / "state.json").read_text())
    assert raw["mail-triage"]["runs"] == 1
    assert raw["mail-triage"]["last_session_id"] == "sess-1"

    # A fresh instance (restart) reads the state back: the preset is NOT due
    # again until the interval elapses — restart-storm cannot re-fire it.
    scheduler2, store2, _, _ = _make(tmp_path, clock=lambda: now["t"] + 60)
    await scheduler2.tick()
    assert store2.created == []


@pytest.mark.asyncio
async def test_submit_failure_is_contained(tmp_path):
    """A failing turn is logged, never raised — and never blocks the next run."""
    store = _FakeStore()

    async def broken_submit(session_id, message, model_level):
        raise RuntimeError("turn exploded")

    scheduler = PeriodicScheduler(
        definitions=[PeriodicSessionDefinition(name="p", initial_prompt="x")],
        conversation_store=store,
        submit_turn=broken_submit,
        is_busy=lambda sid: False,
        persist_path=str(tmp_path / "state.json"),
    )
    assert await scheduler.fire("p") == "sess-1"
    await asyncio.sleep(0)
    # The failed run is recorded; a later fire proceeds normally.
    assert scheduler.state_for("p")["runs"] == 1
    assert await scheduler.fire("p") == "sess-2"


def _anchored_daily_def(
    anchor: datetime, name: str = "daily"
) -> PeriodicSessionDefinition:
    return PeriodicSessionDefinition(
        name=name,
        initial_prompt="Produce today's calendar agenda.",
        schedule_interval_seconds=86400,
        anchor_utc=anchor,
    )


@pytest.mark.asyncio
async def test_anchored_preset_fires_at_anchor_not_before(tmp_path):
    """An anchored preset is quiet before its anchor and fires at the anchor."""
    anchor = datetime(2026, 9, 3, 6, 0, 0, tzinfo=UTC)
    anchor_ts = anchor.timestamp()
    scheduler, store, _, now = _make(
        tmp_path, definitions=[_anchored_daily_def(anchor)]
    )

    # One hour before the anchor — not due (no end-of-day/registration fire).
    now["t"] = anchor_ts - 3600
    await scheduler.tick()
    assert store.created == []

    # Exactly at the anchor — due.
    now["t"] = anchor_ts
    await scheduler.tick()
    assert store.created == ["sess-1"]
    assert scheduler.state_for("daily")["runs"] == 1


@pytest.mark.asyncio
async def test_anchored_preset_fires_at_anchor_time_of_day_every_utc_day(tmp_path):
    """Anchored daily preset fires once per UTC day at the anchor's time-of-day.

    Over a 7-day window every fire lands at the anchor's UTC time-of-day and
    no run lands at or after end of day.
    """
    anchor = datetime(2026, 9, 3, 6, 0, 0, tzinfo=UTC)
    anchor_ts = anchor.timestamp()
    scheduler, store, _, now = _make(
        tmp_path, definitions=[_anchored_daily_def(anchor)]
    )

    # Fire day 0 at the anchor.
    now["t"] = anchor_ts
    await scheduler.tick()
    await asyncio.sleep(0)  # let the seeded turn complete so the next fire isn't "busy"
    assert store.created == ["sess-1"]

    for day in range(1, 7):
        # At the anchor's time-of-day on each subsequent UTC day: due.
        now["t"] = anchor_ts + day * 86400
        await scheduler.tick()
        await asyncio.sleep(0)
        assert len(store.created) == day + 1

    # Mid-day is never a fire time — no run lands at or after end of day.
    now["t"] = anchor_ts + 6 * 86400 + 12 * 3600
    await scheduler.tick()
    assert len(store.created) == 7


@pytest.mark.asyncio
async def test_anchored_preset_whose_anchor_passed_waits_for_next_occurrence(
    tmp_path,
):
    """A preset registered after its anchor waits for the next occurrence.

    It does not fire immediately off the cadence — the first run is the next
    occurrence on/after now.
    """
    anchor = datetime(2026, 9, 3, 6, 0, 0, tzinfo=UTC)
    anchor_ts = anchor.timestamp()
    scheduler, store, _, now = _make(
        tmp_path, definitions=[_anchored_daily_def(anchor)]
    )

    # Registered an hour after the anchor: no immediate fire.
    now["t"] = anchor_ts + 3600
    await scheduler.tick()
    assert store.created == []

    # Next UTC day at the anchor: fires.
    now["t"] = anchor_ts + 86400
    await scheduler.tick()
    await asyncio.sleep(0)
    assert store.created == ["sess-1"]


@pytest.mark.asyncio
async def test_unanchored_preset_regression(tmp_path):
    """Presets without an anchor keep the legacy cadence exactly.

    Never-fired fires promptly; later runs are spaced by the interval from the
    last fire.
    """
    defs = [
        PeriodicSessionDefinition(
            name="mail-triage",
            initial_prompt="Review the mail queue. READ-ONLY.",
            schedule_interval_seconds=3600,
            model_level=2,
        )
    ]
    scheduler, store, _, now = _make(tmp_path, definitions=defs)

    # Never-fired → due immediately (legacy behaviour).
    await scheduler.tick()
    await asyncio.sleep(0)
    assert store.created == ["sess-1"]

    # Just under one interval later → not due.
    now["t"] += 3599
    await scheduler.tick()
    assert store.created == ["sess-1"]

    # Past one full interval since the last fire → due again.
    now["t"] += 1
    await scheduler.tick()
    await asyncio.sleep(0)
    assert store.created == ["sess-1", "sess-2"]
