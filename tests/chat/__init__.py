"""Tests for the chat SSE server."""

from __future__ import annotations


class _FakeCoro:
    """Stand-in for ``asyncio.Task[None]`` — no event loop required."""

    def add_done_callback(self, _cb: object) -> None:
        pass

    def cancel(self, _msg: object = None) -> bool:
        return False

    def done(self) -> bool:
        return False


def _fake_coro() -> _FakeCoro:
    """Return a stand-in for ``asyncio.Task[None]`` for non-async tests."""
    return _FakeCoro()
