"""Unit tests for :mod:`robotsix_chat.memory.base` (NullMemory, ReadOnlyMemory)."""

from __future__ import annotations

from typing import Any

import pytest

from robotsix_chat.memory.base import ChatMemory, NullMemory, ReadOnlyMemory


class _FakeMemory:
    """A minimal ChatMemory that records every call (no ``recall_deep``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._status: dict[str, Any] = {"backend": "fake", "degraded": False}

    async def setup(self) -> None:
        self.calls.append(("setup", (), {}))

    async def recall(self, query: str, *, session_id: str | None = None) -> str:
        self.calls.append(("recall", (query,), {"session_id": session_id}))
        return f"recalled:{query}"

    async def remember(
        self,
        user_message: str,
        assistant_message: str,
        *,
        session_id: str | None = None,
    ) -> None:
        self.calls.append(
            ("remember", (user_message, assistant_message), {"session_id": session_id})
        )

    def status(self) -> dict[str, Any]:
        self.calls.append(("status", (), {}))
        return self._status

    def set_recovery_callback(self, callback: Any) -> None:
        self.calls.append(("set_recovery_callback", (callback,), {}))

    def set_notify_callback(self, callback: Any) -> None:
        self.calls.append(("set_notify_callback", (callback,), {}))

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


class _FakeDeepMemory(_FakeMemory):
    """A fake backend that also exposes ``recall_deep``."""

    async def recall_deep(self, query: str) -> str:
        self.calls.append(("recall_deep", (query,), {}))
        return f"deep:{query}"


async def _recover() -> str:
    return "recovered"


async def _notify(title: str, body: str) -> None:
    return None


# --- NullMemory -------------------------------------------------------------


def test_null_memory_satisfies_protocol() -> None:
    assert isinstance(NullMemory(), ChatMemory)


@pytest.mark.asyncio
async def test_null_memory_recall_returns_empty() -> None:
    mem = NullMemory()
    await mem.setup()
    assert await mem.recall("anything") == ""
    assert await mem.recall("anything", session_id="s1") == ""


@pytest.mark.asyncio
async def test_null_memory_remember_is_noop() -> None:
    mem = NullMemory()
    await mem.remember("u", "a", session_id="s1")
    assert await mem.recall("u") == ""


def test_null_memory_status() -> None:
    assert NullMemory().status() == {"backend": "null", "degraded": False}


def test_null_memory_callbacks_are_noops() -> None:
    mem = NullMemory()
    mem.set_recovery_callback(_recover)
    mem.set_recovery_callback(None)
    mem.set_notify_callback(_notify)
    mem.set_notify_callback(None)


# --- ReadOnlyMemory ---------------------------------------------------------


def test_read_only_memory_satisfies_protocol() -> None:
    assert isinstance(ReadOnlyMemory(_FakeMemory()), ChatMemory)


@pytest.mark.asyncio
async def test_read_only_remember_drops_writes() -> None:
    inner = _FakeMemory()
    mem = ReadOnlyMemory(inner)
    await mem.remember("u", "a", session_id="s1")
    assert "remember" not in inner.names()
    assert inner.calls == []


@pytest.mark.asyncio
async def test_read_only_setup_delegates() -> None:
    inner = _FakeMemory()
    await ReadOnlyMemory(inner).setup()
    assert inner.names() == ["setup"]


@pytest.mark.asyncio
async def test_read_only_recall_delegates_with_session_id() -> None:
    inner = _FakeMemory()
    mem = ReadOnlyMemory(inner)
    assert await mem.recall("q", session_id="s1") == "recalled:q"
    assert await mem.recall("q2") == "recalled:q2"
    assert inner.calls == [
        ("recall", ("q",), {"session_id": "s1"}),
        ("recall", ("q2",), {"session_id": None}),
    ]


@pytest.mark.asyncio
async def test_read_only_forwards_recall_deep_when_present() -> None:
    inner = _FakeDeepMemory()
    mem = ReadOnlyMemory(inner)
    assert hasattr(mem, "recall_deep")
    assert await mem.recall_deep("q") == "deep:q"
    assert inner.names() == ["recall_deep"]


def test_read_only_lacks_recall_deep_when_inner_lacks_it() -> None:
    mem = ReadOnlyMemory(_FakeMemory())
    assert not hasattr(mem, "recall_deep")
    with pytest.raises(AttributeError):
        _ = mem.recall_deep


def test_read_only_status_flags_read_only_without_mutating_inner() -> None:
    inner = _FakeMemory()
    mem = ReadOnlyMemory(inner)
    status = mem.status()
    assert status == {"backend": "fake", "degraded": False, "read_only": True}
    assert "read_only" not in inner._status
    assert inner._status == {"backend": "fake", "degraded": False}


def test_read_only_callbacks_are_noops() -> None:
    inner = _FakeMemory()
    mem = ReadOnlyMemory(inner)
    mem.set_recovery_callback(_recover)
    mem.set_notify_callback(_notify)
    mem.set_recovery_callback(None)
    mem.set_notify_callback(None)
    assert inner.calls == []
