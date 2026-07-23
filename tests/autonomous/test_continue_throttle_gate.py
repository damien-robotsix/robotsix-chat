"""Tests for the auto-continue throttle + pending-subsession gate."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from robotsix_chat.autonomous.runner import AutonomousRunner
from robotsix_chat.chat.conversation import ConversationStore


def _runner(
    registry: object | None, interval: float = 1.0, timeout: float = 5.0
) -> AutonomousRunner:
    settings = MagicMock()
    settings.autonomous.persist_path = "/tmp/does-not-exist-autonomous.json"  # noqa: S108
    settings.autonomous.continue_interval_seconds = interval
    settings.autonomous.pending_subsession_wait_timeout = timeout
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
    """An active subsession for the session is reported as pending."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [
        SimpleNamespace(is_active=False),
        SimpleNamespace(is_active=True),
    ]
    assert _runner(reg)._has_pending_subsessions("s1") is True


def test_has_pending_subsessions_all_terminal() -> None:
    """Only terminal subsessions → not pending."""
    reg = MagicMock()
    reg.list_for_owner.return_value = [SimpleNamespace(is_active=False)]
    assert _runner(reg)._has_pending_subsessions("s1") is False


def test_has_pending_subsessions_registry_error_is_safe() -> None:
    """A registry that raises must not break the loop (treated as not pending)."""
    reg = MagicMock()
    reg.list_for_owner.side_effect = RuntimeError("boom")
    assert _runner(reg)._has_pending_subsessions("s1") is False


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
        [SimpleNamespace(is_active=True)],
        [SimpleNamespace(is_active=True)],
        [SimpleNamespace(is_active=False)],
    ]
    monkeypatch.setattr("robotsix_chat.autonomous.runner.asyncio.sleep", fake_sleep)
    await _runner(reg, interval=1.0, timeout=100.0)._wait_before_continue("s1")
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

    reg = MagicMock()
    reg.list_for_owner.return_value = [SimpleNamespace(is_active=True)]  # never clears
    monkeypatch.setattr("robotsix_chat.autonomous.runner.asyncio.sleep", fake_sleep)
    await _runner(reg, interval=1.0, timeout=3.0)._wait_before_continue("s1")
    # throttle(1) + gate sleeps until waited >= timeout(3): total sleeps bounded
    assert sum(slept) >= 3.0
    assert len(slept) <= 5  # bounded, not infinite
