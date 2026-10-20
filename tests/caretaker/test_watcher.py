"""Tests for the model-free caretaker-findings watcher loop."""

from __future__ import annotations

import types
from typing import Any

import pytest

from robotsix_chat.caretaker import watcher as watcher_mod
from robotsix_chat.caretaker.models import CaretakerFinding
from robotsix_chat.caretaker.watcher import watch_caretaker_findings
from robotsix_chat.config.deploy_models import (
    CaretakerSettings,
    CentralDeploySettings,
)


class _RecordingSink:
    """Captures broadcast and per-session frames for assertions."""

    def __init__(self) -> None:
        self.broadcasts: list[dict[str, Any]] = []
        self.targeted: list[tuple[str, dict[str, Any]]] = []

    def publish(self, session_id: str, frame: dict[str, object]) -> None:
        self.targeted.append((session_id, dict(frame)))

    def publish_all(self, frame: dict[str, object]) -> None:
        self.broadcasts.append(dict(frame))


class _StopLoopError(Exception):
    """Sentinel raised from the patched sleep to terminate the poll loop."""


def _make_env(
    *,
    enabled: bool = True,
    url: str = "https://deploy.robotsix.net",
    surface_kinds: list[str] | None = None,
) -> Any:
    caretaker = CaretakerSettings(
        enabled=enabled,
        poll_interval_seconds=1.0,
        surface_kinds=surface_kinds
        if surface_kinds is not None
        else ["claude_auth", "crash_loop", "health"],
    )
    settings = types.SimpleNamespace(
        caretaker=caretaker,
        central_deploy=CentralDeploySettings(url=url),
    )
    return types.SimpleNamespace(settings=settings, event_sink=_RecordingSink())


def _claude_auth_error() -> CaretakerFinding:
    return CaretakerFinding(
        kind="claude_auth",
        component="",
        severity="error",
        detail="Re-login from the Claude Auth panel.",
        title="Claude credential unusable - fleet-wide",
    )


def _patch_poll_sequence(
    monkeypatch: pytest.MonkeyPatch, sequence: list[list[CaretakerFinding]]
) -> dict[str, int]:
    state = {"i": 0}

    async def fake_fetch(
        central: CentralDeploySettings, *, timeout: float
    ) -> list[CaretakerFinding]:
        i = state["i"]
        return sequence[i] if i < len(sequence) else []

    async def fake_sleep(_seconds: float) -> None:
        state["i"] += 1
        if state["i"] >= len(sequence):
            raise _StopLoopError

    monkeypatch.setattr(watcher_mod, "fetch_caretaker_status", fake_fetch)
    monkeypatch.setattr(watcher_mod.asyncio, "sleep", fake_sleep)
    return state


@pytest.mark.asyncio
async def test_surface_then_autoresolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claude_auth error surfaces once, stays quiet, then resolves."""
    env = _make_env()
    finding = _claude_auth_error()
    _patch_poll_sequence(
        monkeypatch,
        [
            [finding],  # poll 1: new → notify
            [finding],  # poll 2: unchanged → silent
            [],  # poll 3: disappeared → resolution notice
        ],
    )

    with pytest.raises(_StopLoopError):
        await watch_caretaker_findings(env)

    broadcasts = env.event_sink.broadcasts
    assert len(broadcasts) == 2
    assert broadcasts[0]["title"] == "Claude credential unusable - fleet-wide"
    assert broadcasts[0]["type"] == "notification"
    assert "Re-login" in broadcasts[0]["body"]
    assert broadcasts[1]["title"].startswith("Resolved:")


@pytest.mark.asyncio
async def test_non_error_and_unsurfaced_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _make_env()
    warn = CaretakerFinding(
        kind="claude_auth",
        component="",
        severity="warning",
        detail="token expiring soon",
        title="t",
    )
    other = CaretakerFinding(
        kind="disk_space",
        component="robotsix-chat",
        severity="error",
        detail="low disk",
        title="t",
    )
    _patch_poll_sequence(monkeypatch, [[warn, other]])

    with pytest.raises(_StopLoopError):
        await watch_caretaker_findings(env)

    assert env.event_sink.broadcasts == []


@pytest.mark.asyncio
async def test_body_change_re_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _make_env()
    first = _claude_auth_error()
    changed = CaretakerFinding(
        kind="claude_auth",
        component="",
        severity="error",
        detail="access token also dead now",
        title="Claude credential unusable - fleet-wide",
    )
    _patch_poll_sequence(monkeypatch, [[first], [changed]])

    with pytest.raises(_StopLoopError):
        await watch_caretaker_findings(env)

    # Same (kind, component) but changed body → a second notification.
    assert len(env.event_sink.broadcasts) == 2
    assert "access token also dead" in env.event_sink.broadcasts[1]["body"]


@pytest.mark.asyncio
async def test_disabled_returns_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _make_env(enabled=False)

    async def _boom(*_a: object, **_k: object) -> list[CaretakerFinding]:
        raise AssertionError("fetch must not be called when disabled")

    monkeypatch.setattr(watcher_mod, "fetch_caretaker_status", _boom)

    await watch_caretaker_findings(env)
    assert env.event_sink.broadcasts == []


@pytest.mark.asyncio
async def test_no_url_returns_without_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _make_env(url="")

    async def _boom(*_a: object, **_k: object) -> list[CaretakerFinding]:
        raise AssertionError("fetch must not be called without a URL")

    monkeypatch.setattr(watcher_mod, "fetch_caretaker_status", _boom)

    await watch_caretaker_findings(env)
    assert env.event_sink.broadcasts == []
