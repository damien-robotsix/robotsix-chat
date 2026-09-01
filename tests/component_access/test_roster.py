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

    async def call_from_loop():
        return roster_mod.fetch_roster_sync(object())

    assert asyncio.run(call_from_loop()) == [{"name": "x"}]
