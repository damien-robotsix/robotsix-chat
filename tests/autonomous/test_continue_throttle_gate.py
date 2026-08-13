"""Tests for the auto-continue throttle + pending-subsession gate."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import ConversationStore


def _runner(registry: object | None, interval: float = 1.0) -> AutonomousRunner:
    settings = MagicMock()
    settings.autonomous.persist_path = "/does-not-exist-autonomous.json"
    settings.autonomous.continue_interval_seconds = interval
    return AutonomousRunner(
        settings=settings,
        conversation_store=ConversationStore(),
        agent_factory=MagicMock(),
        run_serializer=MagicMock(),
        subsession_registry=registry,
    )


def test_has_pending_subsessions_none_registry() -> None:
    """No registry wired → never reports pending (gate is a no-op)."""
    assert _runner(None)._has_pending_subsessions("s1") is False


def test_has_pending_subsessions_detects_active() -> None:
    """An active non-periodic subsession for the session is reported as pending."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=False, kind="task"),
        SimpleNamespace(is_active=True, kind="task"),
    ]
    assert _runner(reg)._has_pending_subsessions("s1") is True


def test_has_pending_subsessions_all_terminal() -> None:
    """Only terminal subsessions → not pending."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [SimpleNamespace(is_active=False, kind="task")]
    assert _runner(reg)._has_pending_subsessions("s1") is False


def test_has_pending_subsessions_registry_error_is_safe() -> None:
    """A registry that raises must not break the loop (treated as not pending)."""
    reg = MagicMock()
    reg.list_for_owner.side_effect = RuntimeError("boom")
    assert _runner(reg)._has_pending_subsessions("s1") is False


def test_has_pending_subsessions_excludes_periodic() -> None:
    """Active periodic subsessions are NOT reported as pending — they run forever."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=True, kind="periodic"),
        SimpleNamespace(is_active=True, kind="periodic"),
    ]
    assert _runner(reg)._has_pending_subsessions("s1") is False


def test_has_pending_subsessions_mixed_kinds() -> None:
    """Only the non-periodic active subsessions count as pending."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=True, kind="periodic"),
        SimpleNamespace(is_active=True, kind="task"),
        SimpleNamespace(is_active=True, kind="user_chat"),
        SimpleNamespace(is_active=False, kind="task"),
    ]
    assert _runner(reg)._has_pending_subsessions("s1") is True


def test_has_active_subsessions_none_registry() -> None:
    """No registry wired → never reports active (gate is a no-op)."""
    assert _runner(None)._has_active_subsessions("s1") is False


def test_has_active_subsessions_detects_active() -> None:
    """An active subsession of any kind (including periodic) is reported."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=False, kind="task"),
        SimpleNamespace(is_active=True, kind="task"),
    ]
    assert _runner(reg)._has_active_subsessions("s1") is True


def test_has_active_subsessions_all_terminal() -> None:
    """Only terminal subsessions → not active."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [SimpleNamespace(is_active=False, kind="task")]
    assert _runner(reg)._has_active_subsessions("s1") is False


def test_has_active_subsessions_registry_error_is_safe() -> None:
    """A registry that raises must not break the gate (treated as not active)."""
    reg = MagicMock()
    reg.list_for_owner.side_effect = RuntimeError("boom")
    assert _runner(reg)._has_active_subsessions("s1") is False


def test_has_active_subsessions_includes_periodic() -> None:
    """Active periodic subsessions ARE reported — unlike _has_pending_subsessions.

    This is the key difference from _has_pending_subsessions: periodic
    monitors run indefinitely, so they must block completion (the session
    is not "complete" while a periodic monitor is still running).
    """
    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=True, kind="periodic"),
    ]
    assert _runner(reg)._has_active_subsessions("s1") is True


def test_has_active_subsessions_mixed_kinds() -> None:
    """Any active subsession — periodic, task, or user_chat — counts."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=True, kind="periodic"),
        SimpleNamespace(is_active=False, kind="task"),
        SimpleNamespace(is_active=True, kind="user_chat"),
        SimpleNamespace(is_active=False, kind="periodic"),
    ]
    assert _runner(reg)._has_active_subsessions("s1") is True


@pytest.mark.asyncio
async def test_wait_before_continue_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Throttle: waits at least one interval even with no pending work."""
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    monkeypatch.setattr("robotsix_chat.autonomous.runner.asyncio.sleep", fake_sleep)
    await _runner(None, interval=2.0)._wait_before_continue("s1")
    assert slept == [2.0]


@pytest.mark.asyncio
async def test_wait_before_continue_gates_until_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate: keeps waiting while pending, then proceeds once cleared."""
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    reg = MagicMock()
    # pending for the first two checks after the throttle, then clear
    reg.list_for_owner.side_effect = [
        [SimpleNamespace(is_active=True, kind="task")],
        [SimpleNamespace(is_active=True, kind="task")],
        [SimpleNamespace(is_active=False, kind="task")],
    ]
    monkeypatch.setattr("robotsix_chat.autonomous.runner.asyncio.sleep", fake_sleep)
    await _runner(reg, interval=1.0)._wait_before_continue("s1")
    # 1 throttle sleep + 2 gate sleeps while pending
    assert len(slept) == 3


@pytest.mark.asyncio
async def test_wait_before_continue_bounded_by_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate is bounded: a never-clearing subsession cannot hang forever."""
    slept: list[float] = []

    async def fake_sleep(d: float) -> None:
        slept.append(d)

    monkeypatch.setattr(
        "robotsix_chat.autonomous.runner._PENDING_SUBSESSION_WAIT_TIMEOUT", 3.0
    )

    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=True, kind="task")  # never clears
    ]
    monkeypatch.setattr("robotsix_chat.autonomous.runner.asyncio.sleep", fake_sleep)
    await _runner(reg, interval=1.0)._wait_before_continue("s1")
    # throttle(1) + gate sleeps until waited >= timeout(3): total sleeps bounded
    assert sum(slept) >= 3.0
    assert len(slept) <= 5  # bounded, not infinite


@pytest.mark.asyncio
async def test_auto_continue_suppressed_for_active_periodic_subsession(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-continue skips the turn when a periodic subsession is sleeping.

    A periodic monitor between ticks (status SLEEPING) is still active,
    so the auto-continue loop must suppress the "Continue." prompt and
    wait for the subsession to clear before proceeding.
    """
    monkeypatch.setattr(
        "robotsix_chat.autonomous.runner._PENDING_SUBSESSION_WAIT_TIMEOUT", 0.0
    )
    store = ConversationStore()
    settings = MagicMock()
    settings.autonomous.max_auto_turns = 20
    settings.autonomous.continue_interval_seconds = 0
    settings.autonomous.pending_subsession_wait_timeout = 0
    settings.autonomous.completion_marker = "[COMPLETE]"
    settings.autonomous.auto_approve = False
    settings.autonomous.max_idle_auto_turns = 0

    run_serializer = MagicMock()
    run_serializer.for_owner.return_value.__aenter__ = AsyncMock()
    run_serializer.for_owner.return_value.__aexit__ = AsyncMock()

    agent = MagicMock()

    async def _stream(*args, **kwargs):
        yield "[COMPLETE]"  # exit loop via completion marker
        return

    agent.stream.side_effect = _stream

    reg = MagicMock()
    # First two calls: periodic active (suppression kicks in).
    # Next two calls: clear (loop proceeds to agent).
    reg.list_for_owner.side_effect = [
        [SimpleNamespace(is_active=True, kind="periodic")],
        [SimpleNamespace(is_active=True, kind="periodic")],
        [],
        [],
    ]

    runner = AutonomousRunner(
        settings=settings,
        conversation_store=store,
        agent_factory=lambda: agent,
        run_serializer=run_serializer,
        subsession_registry=reg,
    )
    aq = runner.create_session("owner1", schedule_kickoff=False)
    aq.state = AutonomousState.executing
    aq.auto_turn_count = 2  # non-zero so throttle gate + suppression apply
    runner._save_sessions = MagicMock()

    await runner._auto_continue(aq.session_id)

    # Agent must have been called exactly once (on the second iteration,
    # after the subsession list cleared).
    assert agent.stream.call_count == 1
