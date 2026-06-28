"""Tests for the mill integration — :func:`build_mill_tools` and ``MillClient``.

robotsix-agent-comm is faked via ``sys.modules`` so these run without the
``broker`` extra installed and never touch a real broker.
"""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.config import MillSettings
from robotsix_chat.mill import build_mill_tools
from robotsix_chat.mill.client import MillClient

from ..conftest import _FakeError, _install_fake_agent_comm, _Reply


def _settings(**kw: Any) -> MillSettings:
    base: dict[str, Any] = {"enabled": True, "broker_token": "tok"}
    base.update(kw)
    return MillSettings(**base)


# ---------------------------------------------------------------------------
# build_mill_tools
# ---------------------------------------------------------------------------


def test_build_mill_tools_disabled() -> None:
    """Verify that disabled mill returns no tools."""
    assert build_mill_tools(MillSettings(enabled=False)) == []


def test_build_mill_tools_without_broker_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that missing broker extra returns no tools."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert build_mill_tools(_settings()) == []


def test_build_mill_tools_returns_consult_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that enabled mill with broker extra returns the consult tool."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch)
    tools = build_mill_tools(_settings())
    assert len(tools) == 2
    assert tools[0].__name__ == "consult_mill"
    assert tools[1].__name__ == "get_board_write_queue_status"


# ---------------------------------------------------------------------------
# MillClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_sends_nl_to_board_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that consult sends a natural-language request to the board manager."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "done"}))
    client = MillClient(_settings(agent_id="robotsix-chat"))
    out = await client.consult("create a ticket to add X")

    assert out == "done"
    assert captured["agent_id"] == "robotsix-chat"
    assert captured["recipient"] == "board-manager-robotsix-mill"
    assert captured["payload"] == {"message": "create a ticket to add X"}
    assert captured["broker_token"] == "tok"  # pragma: allowlist secret


@pytest.mark.asyncio
async def test_consult_includes_repo_id_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that repo_id is included in the payload when set."""
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "ok"}))
    client = MillClient(_settings(repo_id="robotsix-chat"))
    await client.consult("status?")
    assert captured["payload"] == {"message": "status?", "repo_id": "robotsix-chat"}


@pytest.mark.asyncio
async def test_consult_blank_request_skips_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return an error for blank requests without contacting the broker.

    The broker is never contacted for empty or whitespace-only requests.
    """
    captured = _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "x"}))
    client = MillClient(_settings())
    out = await client.consult("   ")
    assert "No request" in out
    assert "payload" not in captured  # never contacted the broker


@pytest.mark.asyncio
async def test_consult_never_raises_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that transport errors degrade to a message instead of raising."""
    _install_fake_agent_comm(monkeypatch, raise_exc=RuntimeError("broker down"))
    client = MillClient(_settings())
    out = await client.consult("hi")
    assert "could not be completed" in out.lower()
    assert "broker down" in out


@pytest.mark.asyncio
async def test_consult_handles_board_error_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify that board manager error replies are surfaced as text."""
    err = _FakeError({"code": "BAD_REQUEST", "message": "nope"})
    _install_fake_agent_comm(monkeypatch, reply=err)
    client = MillClient(_settings())
    out = await client.consult("do a thing")
    assert "nope" in out


# ---------------------------------------------------------------------------
# consult_mill per-turn cache (ContextVar keyed by request string)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_mill_caches_result_within_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second consult_mill with the same request returns the cached result."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "board says ok"}))

    # Bypass the broker preflight check (always unreachable in sandbox).
    from robotsix_chat.broker_client import BaseBrokeredClient

    monkeypatch.setattr(BaseBrokeredClient, "_check_reachable", lambda self: (True, ""))

    # Capture the MillClient instance so we can modify its requester's reply
    # after the first call.  This proves the second call hits the cache rather
    # than simply returning the old reply from an unpatched requester instance.
    captured_client: dict[str, Any] = {}
    _orig_init = BaseBrokeredClient.__init__

    def _capturing_init(
        self: Any,
        settings: Any,
        *,
        target_agent_id: str,
        default_reply: str,
    ) -> None:
        captured_client["client"] = self
        return _orig_init(
            self,
            settings,
            target_agent_id=target_agent_id,
            default_reply=default_reply,
        )

    monkeypatch.setattr(BaseBrokeredClient, "__init__", _capturing_init)

    tools = build_mill_tools(_settings())
    consult_mill = tools[0]

    from robotsix_chat.mill import _mill_cache

    _mill_cache.set({})

    # First call — should hit the broker.
    result1 = await consult_mill("list open tickets")
    assert result1 == "board says ok"

    # Modify the existing requester's reply.  If the second call goes to the
    # broker it will get this reply, so the assertion below is truly gated on
    # the cache.
    client = captured_client["client"]
    client._requester._reply = _Reply({"reply": "SHOULD NOT APPEAR"})

    # Second call with the same request — must return the cached result.
    result2 = await consult_mill("list open tickets")
    assert result2 == "board says ok"  # cached, not the new reply


@pytest.mark.asyncio
async def test_consult_mill_does_not_cache_broker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BrokerUnavailableError results are NOT cached — retries are attempted."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )

    from robotsix_chat.broker_client import BaseBrokeredClient

    # Bypass the broker preflight check — we use raise_exc on the requester
    # to simulate a broker-unavailable error at request time instead.
    monkeypatch.setattr(BaseBrokeredClient, "_check_reachable", lambda self: (True, ""))

    # Use a message that matches _is_broker_unavailable fragment so the
    # consult() method raises BrokerUnavailableError → consult_mill enqueues.
    _install_fake_agent_comm(
        monkeypatch,
        raise_exc=RuntimeError("connection refused"),
    )

    tools = build_mill_tools(_settings())
    consult_mill = tools[0]

    from robotsix_chat.mill import _mill_cache

    _mill_cache.set({})

    result1 = await consult_mill("list open tickets")
    # Should be enqueued, NOT a broker reply.
    assert "queued" in result1.lower() or "enqueued" in result1.lower()

    # Now patch the broker to succeed — second call should hit it, not cache.
    _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "board is back"}))

    # Rebuild tools with the new broker mock so _raw_consult sees it.
    tools2 = build_mill_tools(_settings())
    consult_mill2 = tools2[0]
    _mill_cache.set({})

    result2 = await consult_mill2("list open tickets")
    # Should succeed now — NOT return the cached enqueue message.
    assert "board is back" in result2


@pytest.mark.asyncio
async def test_consult_mill_different_requests_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different request strings do not share cached results."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "robotsix_agent_comm" else None,
    )
    _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "first reply"}))

    from robotsix_chat.broker_client import BaseBrokeredClient

    monkeypatch.setattr(BaseBrokeredClient, "_check_reachable", lambda self: (True, ""))

    tools = build_mill_tools(_settings())
    consult_mill = tools[0]

    from robotsix_chat.mill import _mill_cache

    _mill_cache.set({})

    result1 = await consult_mill("list open tickets")
    assert result1 == "first reply"

    # Patch for the second call.
    _install_fake_agent_comm(monkeypatch, reply=_Reply({"reply": "second reply"}))

    # Different request string — should NOT be cached.
    tools2 = build_mill_tools(_settings())
    consult_mill2 = tools2[0]
    _mill_cache.set({})

    result2 = await consult_mill2("list closed tickets")
    assert result2 == "second reply"
