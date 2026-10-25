"""Tests for the graceful self-restart drain gate.

``self_restart`` / ``restart_lifecycle_service(<own service>)`` must defer
while other agent turns are in flight (naming the count) and fire the
restart once they drain or the bounded drain timeout expires.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import LifecycleSettings
from robotsix_chat.lifecycle import build_lifecycle_tools, drain

_BASE = "http://lifecycle:9000"
_SELF_RESTART_URL = f"{_BASE}/chat/services/chat/restart"


def _settings() -> LifecycleSettings:
    return LifecycleSettings.model_validate(
        {
            "enabled": True,
            "api_key": "test-api-key",  # pragma: allowlist secret
            "service_name": "chat",
        }
    )


def _tool(name: str) -> Any:
    return next(
        t for t in build_lifecycle_tools(_settings(), _BASE) if t.__name__ == name
    )


@pytest.fixture(autouse=True)
def _clean_drain_state() -> Iterator[None]:
    drain.clear_inflight_probes()
    drain._deferred_task = None
    yield
    task = drain._deferred_task
    if task is not None and not task.done():
        task.cancel()
    drain._deferred_task = None
    drain.clear_inflight_probes()


def test_inflight_count_sums_probes_and_tolerates_failures() -> None:
    drain.register_inflight_probe("a", lambda: 2)
    drain.register_inflight_probe("b", lambda: 1)

    def _broken() -> int:
        raise RuntimeError("probe down")

    drain.register_inflight_probe("c", _broken)
    assert drain.inflight_count() == 3


@pytest.mark.asyncio
async def test_wait_for_drain_times_out() -> None:
    drain.register_inflight_probe("busy", lambda: 1)
    assert await drain.wait_for_drain(timeout=0.05, poll=0.01) is False


@pytest.mark.asyncio
async def test_self_restart_immediate_when_only_caller_in_flight(
    respx_mock: respx.MockRouter,
) -> None:
    """The calling turn alone does not defer the restart."""
    route = respx_mock.post(_SELF_RESTART_URL).mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )
    drain.register_inflight_probe("chat_turns", lambda: 1)
    out = await _tool("self_restart")()
    assert '"status": "restarting"' in out
    assert route.called


@pytest.mark.asyncio
async def test_self_restart_defers_while_other_turns_in_flight(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferral names the in-flight count; restart fires once they finish."""
    monkeypatch.setattr(drain, "RESTART_DRAIN_POLL_SECONDS", 0.01)
    route = respx_mock.post(_SELF_RESTART_URL).mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )
    inflight = {"n": 3}  # the caller + 2 other turns
    drain.register_inflight_probe("chat_turns", lambda: inflight["n"])

    out = await _tool("self_restart")()
    assert "Restart deferred" in out
    assert "2 other agent turn(s)" in out
    await asyncio.sleep(0.05)
    assert not route.called

    # A second call while deferred never stacks another restart.
    again = await _tool("self_restart")()
    assert "already deferred" in again

    inflight["n"] = 0  # every turn (caller included) finished
    task = drain._deferred_task
    assert task is not None
    await asyncio.wait_for(task, 2.0)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_deferred_restart_fires_after_drain_timeout(
    respx_mock: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(drain, "RESTART_DRAIN_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(drain, "RESTART_DRAIN_POLL_SECONDS", 0.01)
    route = respx_mock.post(_SELF_RESTART_URL).mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )
    drain.register_inflight_probe("stuck", lambda: 5)

    out = await _tool("self_restart")()
    assert "4 other agent turn(s)" in out
    task = drain._deferred_task
    assert task is not None
    await asyncio.wait_for(task, 2.0)
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_restart_lifecycle_service_own_service_is_drain_gated(
    respx_mock: respx.MockRouter,
) -> None:
    own = respx_mock.post(f"{_BASE}/services/chat/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )
    other = respx_mock.post(f"{_BASE}/services/file-hub/restart").mock(
        return_value=httpx.Response(200, json={"status": "restarting"})
    )
    drain.register_inflight_probe("subsessions", lambda: 2)
    tool = _tool("restart_lifecycle_service")

    out = await tool("chat")
    assert "Restart deferred" in out
    assert "1 other agent turn(s)" in out
    assert not own.called

    # Other services are never gated — restarting them does not kill us.
    await tool("file-hub")
    assert other.called
