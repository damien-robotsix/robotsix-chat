"""Periodic spawn, validation, escalation, and auto-stop tests.

Split out of test_worker.py (module_size split); the shared helpers and the
autouse transient-backoff fixture come from tests/subsessions/conftest.py.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE, SSE_SUBSESSION_RESULT_TYPE
from robotsix_chat.subsessions import (
    SubsessionAnchorError,
    SubsessionIntervalError,
    SubsessionKind,
    SubsessionNoChangeThresholdError,
    SubsessionRegistry,
    SubsessionStatus,
)
from robotsix_chat.subsessions.models import SubsessionInfo
from robotsix_chat.subsessions.worker_periodic import (
    _anchored_next_run_at,
    _build_periodic_input,
)
from tests.common.subsession_fakes import (
    FakeAgent,
    FakeClock,
    RecordingSink,
    build_env,
    make_settings,
    wait_until,
)
from tests.subsessions.conftest import OWNER, _await_worker, _spawn

# ---------------------------------------------------------------------------
# periodic kind
# ---------------------------------------------------------------------------


def test_periodic_interval_below_minimum_is_rejected() -> None:
    """A periodic interval below the configured minimum raises."""
    env = build_env(settings=make_settings(min_interval_seconds=1.0))

    with pytest.raises(SubsessionIntervalError):
        _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.5)
    with pytest.raises(SubsessionIntervalError):
        _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=None)

    assert env.registry.list_for_owner(OWNER) == []


@pytest.mark.asyncio
async def test_periodic_anchor_time_is_stored_on_spawn() -> None:
    """A valid anchor_time is persisted on the periodic subsession record."""
    agent = FakeAgent(["report 1"])
    settings = make_settings(min_interval_seconds=1.0, max_runs_progress_extension=0)
    env = build_env(agent=agent, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=86400.0,
        max_runs=1,
        anchor_time="09:00 Europe/Paris",
    )
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.anchor_time == "09:00 Europe/Paris"
    await _await_worker(env, sub_id)


def _periodic_info(**overrides: object) -> SubsessionInfo:
    """Minimal periodic SubsessionInfo for scheduler-helper tests."""
    defaults: dict[str, object] = {
        "id": "sub-x",
        "kind": SubsessionKind.PERIODIC,
        "owner_session_id": OWNER,
        "parent_id": None,
        "depth": 1,
        "title": "watch",
        "prompt": "check",
        "model_level": 1,
        "status": SubsessionStatus.SLEEPING,
        "created_at": 0.0,
        "last_activity_at": 0.0,
        "interval_seconds": 86400.0,
    }
    defaults.update(overrides)
    return SubsessionInfo(**defaults)  # type: ignore[arg-type]


def test_anchored_next_run_at_uses_anchor_when_set() -> None:
    """With an anchor, the next fire is the anchored time, not now+interval."""
    tz = ZoneInfo("UTC")
    now = datetime(2026, 9, 4, 14, 0, tzinfo=tz).timestamp()  # 14:00 UTC
    info = _periodic_info(anchor_time="09:00")

    nxt = _anchored_next_run_at(now, info)

    assert nxt == datetime(2026, 9, 5, 9, 0, tzinfo=tz).timestamp()


def test_anchored_next_run_at_falls_back_without_anchor() -> None:
    """Without an anchor, the legacy now+interval cadence is preserved."""
    info = _periodic_info(interval_seconds=60.0, anchor_time=None)

    assert _anchored_next_run_at(1000.0, info) == 1060.0


def test_anchored_next_run_at_degrades_on_malformed_anchor() -> None:
    """A malformed persisted anchor degrades to now+interval instead of crashing."""
    info = _periodic_info(interval_seconds=60.0, anchor_time="nonsense")

    assert _anchored_next_run_at(1000.0, info) == 1060.0


def test_periodic_malformed_anchor_time_is_rejected() -> None:
    """A malformed anchor_time raises SubsessionAnchorError and spawns nothing."""
    env = build_env(settings=make_settings(min_interval_seconds=1.0))

    with pytest.raises(SubsessionAnchorError):
        _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=86400.0,
            anchor_time="25:00",
        )
    assert env.registry.list_for_owner(OWNER) == []


@pytest.mark.asyncio
async def test_non_periodic_anchor_time_is_ignored() -> None:
    """anchor_time on a non-periodic kind is nulled, not stored or validated."""
    agent = FakeAgent(["done"])
    env = build_env(agent=agent)

    # Even a malformed anchor is harmless on a task — it is discarded.
    sub_id = _spawn(env, kind=SubsessionKind.TASK, anchor_time="not-a-time")
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.anchor_time is None
    await _await_worker(env, sub_id)


def test_periodic_no_change_threshold_below_one_is_rejected() -> None:
    """A per-spawn auto_stop_no_change_runs below 1 raises."""
    env = build_env()

    with pytest.raises(SubsessionNoChangeThresholdError):
        _spawn(
            env,
            kind=SubsessionKind.PERIODIC,
            interval_seconds=0.5,
            auto_stop_no_change_runs=0,
        )

    assert env.registry.list_for_owner(OWNER) == []


def test_periodic_no_change_threshold_non_int_is_rejected() -> None:
    """A non-int auto_stop_no_change_runs is rejected (bool is an int in Python)."""
    env = build_env()

    for bad_value in (2.5, "50", True):
        with pytest.raises(SubsessionNoChangeThresholdError):
            _spawn(
                env,
                kind=SubsessionKind.PERIODIC,
                interval_seconds=60.0,
                auto_stop_no_change_runs=bad_value,
            )

    assert env.registry.list_for_owner(OWNER) == []


def test_build_periodic_input_includes_loop_guard_instructions() -> None:
    """The periodic turn input includes CI workflow loop guard instructions."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-x",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch ticket 9d6a",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "abc123"},
    )

    result = _build_periodic_input(info, previous_result=None, steering=[])

    # The output must include the loop guard instructions.
    assert "LOOP GUARD" in result
    assert "CI workflow verification" in result
    assert "GitHub Actions API" in result
    assert "publish/deploy workflow" in result
    # Programmatic gate language.
    assert "PROGRAMMATIC GATE" in result
    assert "will REJECT any summary" in result
    # Deploy-complete config/status check for opt-in feature flags.
    assert "config/status check" in result
    assert "continuation.enabled" in result


def test_build_periodic_input_includes_redundant_fix_ticket_detection() -> None:
    """The periodic turn input includes redundant fix ticket detection."""
    from robotsix_chat.subsessions.models import SubsessionInfo, SubsessionKind

    info = SubsessionInfo(
        id="sub-y",
        kind=SubsessionKind.PERIODIC,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="monitor",
        prompt="watch fix ticket 3120",
        model_level=3,
        status="active",  # type: ignore[arg-type]
        created_at=0.0,
        last_activity_at=0.0,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "3120"},
    )

    result = _build_periodic_input(info, previous_result=None, steering=[])

    # The output must include the redundant fix ticket detection instructions.
    assert "REDUNDANT FIX TICKET DETECTION" in result
    assert "alternative path" in result
    assert "baseline ticket" in result
    assert "complete_subsession" in result
    assert "redundant" in result


@pytest.mark.asyncio
async def test_periodic_run_delivers_result_frame_only() -> None:
    """Each non-suppressed run publishes a result frame to the event sink.

    Intermediate runs are NOT delivered to the parent conversation store —
    only the terminal summary (via complete_subsession or auto-close) arrives.
    """
    sink = RecordingSink()
    agent = FakeAgent(["report 1", "report 2"])
    settings = make_settings(max_runs_progress_extension=0)
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=2,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"
    assert info.runs == 2
    assert info.last_result == "report 2"
    assert info.summary == "Reached the 2-run limit. Last: report 2"

    result_frames = sink.of_type(SSE_SUBSESSION_RESULT_TYPE)
    assert [(s, f["run"], f["text"]) for s, f in result_frames] == [
        (OWNER, 1, "report 1"),
        (OWNER, 2, "report 2"),
    ]

    history = env.conversation_store.history(OWNER)
    # Only the terminal summary is delivered — intermediate runs stay silent.
    assert len(history) == 1
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_max_runs_escalation_threshold_reached() -> None:
    """Monitor closes with 'max_runs_escalated' when threshold is reached."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=2, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
        checkpoint={
            "ticket_id": "ticket-1",
            "max_runs_exhausted_count": 1,
        },
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs_escalated"
    assert info.runs == 1
    assert "consecutive time" in (info.summary or "")


@pytest.mark.asyncio
async def test_periodic_max_runs_below_escalation_threshold() -> None:
    """Below threshold, closes with 'max_runs' and persists incremented count."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=3, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
        checkpoint={
            "ticket_id": "ticket-1",
            "max_runs_exhausted_count": 1,
        },
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"
    assert info.runs == 1
    # The checkpoint should have been updated with the incremented count.
    assert info.checkpoint is not None
    assert info.checkpoint.get("max_runs_exhausted_count") == 2


@pytest.mark.asyncio
async def test_periodic_max_runs_escalation_disabled_when_threshold_zero() -> None:
    """A threshold of 0 disables escalation entirely."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=0, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
        checkpoint={
            "ticket_id": "ticket-1",
            "max_runs_exhausted_count": 5,
        },
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"


@pytest.mark.asyncio
async def test_periodic_max_runs_escalation_no_checkpoint() -> None:
    """No checkpoint: count starts at 1, below threshold."""
    sink = RecordingSink()
    agent = FakeAgent(["report 1"])
    settings = make_settings(
        max_runs_escalation_threshold=2, max_runs_progress_extension=0
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    # No checkpoint → count starts at 0 → incremented to 1 < threshold.
    assert info.close_reason == "max_runs"
    assert info.checkpoint is not None
    assert info.checkpoint.get("max_runs_exhausted_count") == 1


@pytest.mark.asyncio
async def test_periodic_max_runs_extends_on_progress_then_auto_stops() -> None:
    """A monitor making progress extends its budget instead of closing at cap.

    When a non-suppressed reply (progress) is observed within the window,
    the max_runs cap is raised; the monitor then continues and only stops
    once it accumulates enough consecutive NO_CHANGE runs.
    """
    sink = RecordingSink()
    agent = FakeAgent(["report 1", "report 2", "NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    settings = make_settings(
        max_runs_progress_extension=20,
        max_runs_progress_window=5,
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=2,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    assert info.runs == 5
    assert info.max_runs == 22


@pytest.mark.asyncio
async def test_periodic_max_runs_no_progress_does_not_extend() -> None:
    """Without recent progress the monitor still closes at the hard cap."""
    sink = RecordingSink()
    agent = FakeAgent(["NO_CHANGE"])
    settings = make_settings(
        max_runs_progress_extension=20,
        max_runs_progress_window=5,
    )
    env = build_env(agent=agent, event_sink=sink, settings=settings)

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        max_runs=1,
        title="watch",
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "max_runs"
    assert info.max_runs == 1


@pytest.mark.asyncio
async def test_periodic_no_change_reply_is_suppressed() -> None:
    """A NO_CHANGE run produces no delivery and no result frame."""
    sink = RecordingSink()
    agent = FakeAgent(["NO_CHANGE"])
    env = build_env(agent=agent, event_sink=sink)

    sub_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02, max_runs=1
    )
    await _await_worker(env, sub_id)

    assert sink.of_type(SSE_SUBSESSION_RESULT_TYPE) == []
    history = env.conversation_store.history(OWNER)
    # Only the terminal summary is delivered — no per-run turn.
    assert len(history) == 1
    assert "run 1" not in history[0][0]
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_auto_stops_after_consecutive_no_change_runs() -> None:
    """N consecutive NO_CHANGE runs close the subsession automatically."""
    agent = FakeAgent(["NO_CHANGE", "no_change again"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    assert "Auto-stopped after 2 consecutive no-change runs" in (info.summary or "")
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_periodic_auto_stop_uses_per_spawn_no_change_threshold() -> None:
    """A per-spawn auto_stop_no_change_runs override wins over the global cap."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        auto_stop_no_change_runs=3,
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    assert "Auto-stopped after 3 consecutive no-change runs" in (info.summary or "")
    # The global threshold (2) must not have fired first.
    assert len(agent.calls) == 3


@pytest.mark.asyncio
async def test_periodic_ignores_bool_no_change_threshold_in_checkpoint() -> None:
    """A bool ``auto_stop_no_change_runs`` in the checkpoint is not a valid override.

    Spawn validation rejects bools, but an agent ``set_checkpoint`` call
    could still write one — the periodic turn loop must ignore it and fall
    back to the global threshold rather than treating ``True`` as ``1``.
    """
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    # Seed a bool override directly into the checkpoint before the worker's
    # first turn runs (spawn_subsession is synchronous, so the task has not
    # been scheduled yet).
    assert env.registry.update_checkpoint(sub_id, {"auto_stop_no_change_runs": True})
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    # The global threshold (2) applied — True was ignored, not treated as 1.
    assert "Auto-stopped after 2 consecutive no-change runs" in (info.summary or "")
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_periodic_max_idle_runs_pauses_after_consecutive_no_change() -> None:
    """max_idle_runs pauses the subsession and emits a notification."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    sink = RecordingSink()
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=3, auto_stop_no_change_runs=10),
        event_sink=sink,
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    # The worker enters a PAUSED wait loop and never finishes — wait for
    # it to consume the 3 NO_CHANGE replies and reach the paused state
    # (polling rather than a fixed sleep, which is racy under CI load),
    # then cancel it.
    await wait_until(lambda: len(agent.calls) >= 3)

    def _is_paused() -> bool:
        info = env.registry.get(sub_id)
        return info is not None and info.status is SubsessionStatus.PAUSED

    await wait_until(_is_paused)
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    # Let cancellation propagate.
    await asyncio.sleep(0.05)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.PAUSED
    assert info.close_reason == "paused"
    assert info.summary == (
        "Auto-paused after 3 consecutive no-change runs "
        "(no-change pause 1/3; the monitor will auto-close after 3 "
        "such pauses if the ticket never changes). The monitor will "
        "resume when the ticket's state changes, or you can resume it "
        "now by sending a message to this subsession via "
        "message_subsession."
    )
    assert len(agent.calls) == 3

    # Assert an SSE notification was published for the auto-pause.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 1
    _sid, frame = notifications[0]
    assert frame["title"] == f"Monitor auto-paused: {info.title}"
    assert f" — {info.summary}" in str(frame["body"])
    assert frame["urgency"] == "low"


@pytest.mark.asyncio
async def test_periodic_auto_closes_after_repeated_no_change_pauses() -> None:
    """Repeated no-change pauses auto-close the monitor instead of pausing again."""
    # 6 NO_CHANGE replies: 3 to auto-pause the first time, then 3 more
    # after the auto-resume timeout to reach the second pause — which
    # hits the pause limit and closes the monitor.
    agent = FakeAgent(["NO_CHANGE"] * 6)
    sink = RecordingSink()
    env = build_env(
        agent=agent,
        settings=make_settings(
            max_idle_runs=3,
            auto_stop_no_change_runs=10,
            max_no_change_pauses=2,
            paused_monitor_auto_resume_seconds=0.05,
        ),
        event_sink=sink,
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_pause_limit"
    assert "2 consecutive pauses" in (info.summary or "")
    assert "reassess" in (info.summary or "").lower()
    assert len(agent.calls) == 6

    # Auto-pause, auto-resume (timeout), then auto-close notifications.
    notifications = sink.of_type(SSE_NOTIFICATION_TYPE)
    assert len(notifications) == 3
    _sid, frame = notifications[-1]
    assert frame["title"] == f"Monitor auto-closed: {info.title}"
    assert frame["urgency"] == "low"


@pytest.mark.asyncio
async def test_paused_periodic_resumes_on_parent_message() -> None:
    """A parent message via enqueue_message wakes a paused periodic worker."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=3, auto_stop_no_change_runs=10),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # Wait for auto-pause — the worker consumes the 3 NO_CHANGE replies
    # and enters the paused wait loop.
    await wait_until(lambda: len(agent.calls) >= 3)
    await asyncio.sleep(0.15)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.PAUSED

    # Send a parent message — the paused worker should wake and resume.
    env.registry.enqueue_message(sub_id, "parent", "please resume monitoring")

    # The worker resumes and runs at least one more turn.
    await wait_until(lambda: len(agent.calls) >= 4)

    # Clean up — cancel the worker so it doesn't loop forever.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)

    # Verify the subsession was resumed (status no longer PAUSED).
    info = env.registry.get(sub_id)
    assert info is not None
    assert len(agent.calls) >= 4


@pytest.mark.asyncio
async def test_paused_periodic_resume_resets_no_change_counter() -> None:
    """Resume resets consecutive_no_change; monitor not immediately re-paused."""
    # 5 NO_CHANGE replies: 3 to trigger auto-pause, then 2 more after
    # resume to prove the counter was reset (otherwise the 4th would
    # re-pause and the 5th would never happen).
    agent = FakeAgent(["NO_CHANGE"] * 5)
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=3, auto_stop_no_change_runs=10),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # Wait for the worker to auto-pause after 3 consecutive NO_CHANGE runs.
    await wait_until(lambda: len(agent.calls) >= 3)
    await asyncio.sleep(0.15)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.PAUSED

    # Send a parent message to resume.
    env.registry.enqueue_message(sub_id, "parent", "resume please")

    # The worker resumes.  The 4th call is the resumed turn; the 5th
    # proves the counter was reset — without the fix the 4th NO_CHANGE
    # would re-pause the monitor immediately and the 5th would never run.
    await wait_until(lambda: len(agent.calls) >= 5, timeout=3.0)

    # Clean up.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)

    info = env.registry.get(sub_id)
    assert info is not None
    assert len(agent.calls) >= 5


@pytest.mark.asyncio
async def test_paused_periodic_auto_resumes_on_timeout() -> None:
    """A paused periodic monitor auto-resumes after the configured timeout."""
    agent = FakeAgent(
        ["NO_CHANGE", "NO_CHANGE", "NO_CHANGE", "CHANGE_DETECTED", "NO_CHANGE"]
    )
    env = build_env(
        agent=agent,
        settings=make_settings(
            max_idle_runs=3,
            auto_stop_no_change_runs=10,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # The worker will: run 3 NO_CHANGE turns → auto-pause → auto-resume
    # after 0.05s → run at least one more turn (the 4th call).
    # We verify that the monitor resumed and ran >= 4 turns.
    await wait_until(lambda: len(agent.calls) >= 4)
    await asyncio.sleep(0.1)

    info = env.registry.get(sub_id)
    assert info is not None
    # After auto-resume, the monitor is back in its normal periodic cycle
    # (SLEEPING between runs, or RUNNING during a turn).
    assert info.status in (SubsessionStatus.SLEEPING, SubsessionStatus.RUNNING)
    assert len(agent.calls) >= 4

    # Clean up — cancel the worker so it doesn't loop forever.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_periodic_max_idle_runs_zero_disables_pause() -> None:
    """max_idle_runs=0 disables pausing; falls through to auto_stop."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(max_idle_runs=0, auto_stop_no_change_runs=2),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    # Falls through to auto_stop, not paused.
    assert info.close_reason == "no_change_auto_stop"
    assert len(agent.calls) == 2


@pytest.mark.asyncio
async def test_periodic_turn_budget_resets_each_run() -> None:
    """The turn budget is per-run-burst, not a lifetime ceiling, for monitors.

    With hard_stop_turns=2 a lifetime counter would force-close the monitor
    after two cumulative turns.  A per-run reset lets a long-lived monitor
    keep cycling through many single-turn runs without ``turn_budget_exceeded``.
    """
    turn_budget = SimpleNamespace(
        task=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
        periodic=SimpleNamespace(soft_warn_turns=0, hard_stop_turns=2),
        user_chat=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
        on_close=SimpleNamespace(soft_warn_turns=25, hard_stop_turns=40),
    )
    agent = FakeAgent(["NO_CHANGE"] * 10)
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=100,
            max_idle_runs=0,
            turn_budget=turn_budget,
        ),
    )

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)

    # The monitor should run many single-turn cycles without being
    # force-closed for exceeding a lifetime turn ceiling.
    await wait_until(lambda: len(agent.calls) >= 4)
    await asyncio.sleep(0.1)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status in (SubsessionStatus.SLEEPING, SubsessionStatus.RUNNING)
    assert info.close_reason != "turn_budget_exceeded"
    assert len(agent.calls) >= 4

    # Clean up — cancel the worker so it doesn't loop forever.
    task = env.registry._running.get(sub_id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_periodic_human_approval_switches_to_event_driven() -> None:
    """human_issue_approval checkpoint triggers event-driven wait instead of close.

    The monitor must stay alive and wait for the ticket to leave
    human_issue_approval, not auto-close with human_approval_timeout.
    """
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=5,
            human_approval_timeout_runs=3,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
        },
    )
    # Wait until the agent has been called 3 times (human_approval_timeout_runs)
    # — the monitor will then switch to event-driven waiting.
    await wait_until(lambda: len(agent.calls) >= 3)

    info = env.registry.get(sub_id)
    assert info is not None
    # The monitor should still be alive — it switched to event-driven
    # waiting after the human_approval_timeout_runs threshold was reached.
    assert info.status is not SubsessionStatus.CLOSED
    # The agent was called exactly human_approval_timeout_runs times
    # before switching to event-driven wait.
    assert len(agent.calls) == 3

    # Clean up the worker task.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_human_approval_wakes_parent_with_durable_artifact() -> None:
    """A pending operator decision surfaces durably and wakes the parent.

    When a periodic monitor detects the tracked ticket is blocked awaiting a
    human decision and switches to event-driven waiting, it must not rely on
    the ephemeral SSE notification alone: it must wake the parent session via
    ``deliver_summary`` so a durable artifact (a record in the owning
    conversation's history) reaches the operator even with no browser
    connected.  The surfacing is guarded so it fires once per approval
    episode, not on every event-driven resume.
    """
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=2,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
            "ticket_id": "TICKET-42",
        },
    )
    # Wait until the durable operator-decision artifact lands in the owning
    # session's conversation history (delivered via deliver_summary → the
    # passive-record fallback when no live agent is wired).
    await wait_until(
        lambda: any(
            "awaiting an operator decision" in reply
            for _label, reply in env.conversation_store.history(OWNER)
        )
    )

    matches = [
        (label, reply)
        for label, reply in env.conversation_store.history(OWNER)
        if "awaiting an operator decision" in reply
    ]
    # Exactly one durable surfacing — the checkpoint guard prevents the
    # decision from being re-woken on every event-driven resume.
    assert len(matches) == 1
    label, reply = matches[0]
    assert "TICKET-42" in reply
    assert label.startswith(f"[Subsession {sub_id[:8]} (periodic)")

    info = env.registry.get(sub_id)
    assert info is not None
    # Monitor stays alive (event-driven wait), and the guard flag is set.
    assert info.status is not SubsessionStatus.CLOSED
    assert info.checkpoint is not None
    assert info.checkpoint.get("operator_decision_surfaced") is True

    # Clean up the worker task.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_human_approval_timeout_ignored_without_checkpoint() -> None:
    """Without human_issue_approval checkpoint, generic auto_stop applies."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=3,
            human_approval_timeout_runs=2,
        ),
    )

    # Checkpoint has no last_known_state — human-approval timeout should
    # not trigger.
    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={"other_field": "value"},
    )
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    # Falls through to the generic auto-stop, not human_approval_timeout.
    assert info.close_reason == "no_change_auto_stop"
    assert len(agent.calls) == 3


@pytest.mark.asyncio
async def test_periodic_human_approval_timeout_uses_own_threshold() -> None:
    """human_approval_timeout_runs switches to event-driven wait independently."""
    agent = FakeAgent(["NO_CHANGE", "NO_CHANGE"])
    env = build_env(
        agent=agent,
        settings=make_settings(
            auto_stop_no_change_runs=10,
            human_approval_timeout_runs=2,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
        },
    )
    # Wait for the 2 human_approval_timeout_runs calls.
    await wait_until(lambda: len(agent.calls) >= 2)

    info = env.registry.get(sub_id)
    assert info is not None
    # Monitor should still be alive — switched to event-driven wait.
    assert info.status is not SubsessionStatus.CLOSED

    # Clean up the worker task.
    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")


@pytest.mark.asyncio
async def test_periodic_human_approval_wall_clock_switches_to_event_driven() -> None:
    """Wall-clock timeout switches to event-driven wait even without NO_CHANGE.

    The wall-clock backstop catches the case where the agent follows the
    prompt (producing non-NO_CHANGE output each run) but the ticket is
    still stuck at human_issue_approval.  The monitor stays alive.
    """
    clock = FakeClock(start=1000.0)
    agent = FakeAgent(["still waiting for approval", "still waiting for approval"] * 5)
    registry = SubsessionRegistry(
        event_sink=RecordingSink(),
        store_path=None,
        clock=clock,
    )
    env = build_env(
        agent=agent,
        registry=registry,
        settings=make_settings(
            auto_stop_no_change_runs=999,
            human_approval_timeout_runs=999,
            human_approval_timeout_seconds=300.0,
            paused_monitor_auto_resume_seconds=0.05,
        ),
    )

    sub_id = _spawn(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=0.02,
        checkpoint={
            "last_known_state": "human_issue_approval",
            "ticket_id": "TICKET-1",
        },
    )

    # Let the first run complete — it sets human_approval_since in the
    # checkpoint and then the worker sleeps for the inter-run interval.
    await wait_until(lambda: env.registry.get(sub_id).runs >= 1)  # type: ignore[union-attr]

    # Advance the clock past the wall-clock timeout so the *next* run
    # sees the ticket has been stuck too long.
    clock.advance(301.0)

    # Wait for the second run (the wall-clock check triggers on this run).
    await wait_until(lambda: env.registry.get(sub_id).runs >= 2)  # type: ignore[union-attr]

    info = env.registry.get(sub_id)
    assert info is not None
    # Monitor stays alive — switched to event-driven wait by wall-clock.
    assert info.status is not SubsessionStatus.CLOSED

    env.registry.cancel_and_close(sub_id, reason="teardown", closed_by="system")
