"""Tests for the component roster fetch helpers."""


def test_fetch_roster_sync_works_inside_a_running_loop(monkeypatch) -> None:
    """fetch_roster_sync must work when an event loop is already running.

    The periodic scheduler builds agents lazily on its async tick; a bare
    asyncio.run() there died with 'cannot be called from a running event
    loop' (2026-09-01 incident: a junk session per tick).
    """
    import asyncio

    from robotsix_chat.component_access import roster as roster_mod

    async def fake_fetch(settings):
        return [{"name": "x"}]

    monkeypatch.setattr(roster_mod, "fetch_roster", fake_fetch)
    monkeypatch.setattr(roster_mod, "_cache", None)
    monkeypatch.setattr(roster_mod, "_last_non_empty_cache", None)

    async def call_from_loop():
        return roster_mod.fetch_roster_sync(object())

    assert asyncio.run(call_from_loop()) == [{"name": "x"}]


def test_fetch_roster_sync_inside_loop_serves_stale_cache_without_blocking(
    monkeypatch,
) -> None:
    """A stale cache is served at once and refreshed in the background.

    central-deploy builds the roster by probing every component's /chat-skill,
    chat's own included, so a loop parked on ``.result()`` deadlocks with the
    fetch until the 30 s timeouts expire (2026-10-13 06:00–06:04Z: chat
    unresponsive 4.5 min, docker health unhealthy). The sync helper must not
    block the running loop when anything is cached.
    """
    import asyncio
    import time
    from types import SimpleNamespace

    from robotsix_chat.component_access import roster as roster_mod

    stale = [{"id": "mill", "base_url": "http://mill:8077", "skill": ""}]
    old = (time.monotonic() - 3600.0, stale)
    monkeypatch.setattr(roster_mod, "_cache", old)
    monkeypatch.setattr(roster_mod, "_last_non_empty_cache", old)
    monkeypatch.setattr(roster_mod, "_refresh_task", None)
    refreshed = asyncio.Event()

    async def slow_fetch(settings):
        await asyncio.sleep(0.5)
        refreshed.set()
        return [{"id": "fresh", "base_url": "http://fresh", "skill": ""}]

    monkeypatch.setattr(roster_mod, "fetch_roster", slow_fetch)
    settings = SimpleNamespace(roster_cache_ttl=300.0, component_fallbacks={})

    async def call_from_loop():
        t0 = time.monotonic()
        out = roster_mod.fetch_roster_sync(settings)
        assert time.monotonic() - t0 < 0.3, "sync helper blocked the running loop"
        assert out == stale
        # a second call while the refresh is in flight does not start another
        assert roster_mod.fetch_roster_sync(settings) == stale
        task = roster_mod._refresh_task
        assert task is not None and not task.done()
        await asyncio.wait_for(refreshed.wait(), 2.0)
        await task

    asyncio.run(call_from_loop())


def test_fetch_roster_sync_inside_loop_serves_fresh_cache_without_refresh(
    monkeypatch,
) -> None:
    import asyncio
    import time
    from types import SimpleNamespace

    from robotsix_chat.component_access import roster as roster_mod

    fresh = [{"id": "mill", "base_url": "http://mill:8077", "skill": ""}]
    monkeypatch.setattr(roster_mod, "_cache", (time.monotonic(), fresh))
    monkeypatch.setattr(roster_mod, "_last_non_empty_cache", (time.monotonic(), fresh))
    monkeypatch.setattr(roster_mod, "_refresh_task", None)

    async def must_not_run(settings):  # pragma: no cover - guard
        raise AssertionError("fresh cache must not trigger a fetch")

    monkeypatch.setattr(roster_mod, "fetch_roster", must_not_run)
    settings = SimpleNamespace(roster_cache_ttl=300.0, component_fallbacks={})

    async def call_from_loop():
        return roster_mod.fetch_roster_sync(settings)

    assert asyncio.run(call_from_loop()) == fresh
    assert roster_mod._refresh_task is None
