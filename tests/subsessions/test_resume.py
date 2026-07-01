"""Tests for the startup resume hook (``resume_subsessions``)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from robotsix_chat.subsessions import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionRegistry,
    SubsessionStatus,
    resume_subsessions,
)
from tests.common.subsession_fakes import FakeAgent, build_env, make_settings

OWNER = "sess-main"


def _persist_registry(store_path: Path) -> dict[str, str]:
    """Persist one active periodic, one active task, and one closed entry.

    Returns the ids keyed as ``periodic`` / ``task`` / ``closed``.
    """
    registry = SubsessionRegistry(store_path=store_path)
    periodic = registry.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch CI",
        prompt="check the build",
        model_level=3,
        interval_seconds=0.05,
        max_runs=5,
    )
    registry.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=2)
    task = registry.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="one shot",
        prompt="do it",
        model_level=3,
    )
    registry.append_transcript(task.id, "assistant", "half way there")
    closed = registry.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="old job",
        prompt="done already",
        model_level=3,
    )
    registry.mark_closed(closed.id, summary="finished earlier", reason="completed")
    return {"periodic": periodic.id, "task": task.id, "closed": closed.id}


@pytest.mark.asyncio
async def test_resume_subsessions_full_scenario(tmp_path: Path) -> None:
    """Periodic entries respawn; tasks are interrupted; terminal restore as-is."""
    store_path = tmp_path / "subsessions.json"
    ids = _persist_registry(store_path)

    # Fresh process: new registry + env on the same store path.  The
    # respawned periodic worker blocks on its first turn so the test can
    # inspect the live state deterministically.
    gate = asyncio.Event()
    agent = FakeAgent(["resumed"], gate=gate)
    registry = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=agent,
        registry=registry,
        settings=make_settings(min_interval_seconds=0.01),
    )

    resume_subsessions(env)

    # -- periodic: respawned under the same id with a worker attached ------
    periodic = registry.get(ids["periodic"])
    assert periodic is not None
    assert periodic.kind is SubsessionKind.PERIODIC
    assert periodic.status in (SubsessionStatus.RUNNING, SubsessionStatus.SLEEPING)
    assert periodic.interval_seconds == 0.05
    # Remaining run budget: max_runs(5) - runs(2) = 3.
    assert periodic.max_runs == 3
    worker = registry._running.get(ids["periodic"])
    assert worker is not None

    # -- task: cannot resume — marked INTERRUPTED, reported to the owner ---
    task = registry.get(ids["task"])
    assert task is not None
    assert task.status is SubsessionStatus.INTERRUPTED
    assert task.summary is not None
    assert task.summary.startswith("Interrupted by a server restart.")
    assert "Last state: half way there" in task.summary
    assert ids["task"] not in registry._running

    history = env.conversation_store.history(OWNER)
    labels = [label for label, _ in history]
    assert any(
        label.startswith(f"[Subsession {ids['task'][:8]} (task)")
        and "interrupted" in label
        for label in labels
    )

    # -- closed: restored as-is, no worker, no new report -------------------
    closed = registry.get(ids["closed"])
    assert closed is not None
    assert closed.status is SubsessionStatus.CLOSED
    assert closed.summary == "finished earlier"
    assert ids["closed"] not in registry._running
    assert not any(ids["closed"][:8] in label for label in labels)

    # Cleanup the live periodic worker.
    registry.cancel_and_close(ids["periodic"], reason="teardown", closed_by="system")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_periodic_run_budget_never_below_one(tmp_path: Path) -> None:
    """An exhausted persisted run budget still allows one resumed run."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="nearly done",
        prompt="check",
        model_level=3,
        interval_seconds=0.05,
        max_runs=3,
    )
    registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=3)

    gate = asyncio.Event()
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["last run"], gate=gate),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    resumed = registry2.get(periodic.id)
    assert resumed is not None
    assert resumed.max_runs == 1

    worker = registry2._running.get(periodic.id)
    registry2.cancel_and_close(periodic.id, reason="teardown", closed_by="system")
    if worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


def test_resume_skips_malformed_entries(tmp_path: Path) -> None:
    """Entries without id/owner are skipped without blocking the others."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    info = registry1.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="fine",
        prompt="p",
        model_level=3,
    )
    registry1.mark_closed(info.id, summary="ok", reason="completed")
    # Inject a malformed entry alongside the valid one.
    raw = store_path.read_text(encoding="utf-8")
    store_path.write_text(
        raw.replace("[", '[{"subsession_id": "", "owner_session_id": ""},', 1),
        encoding="utf-8",
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(registry=registry2)
    resume_subsessions(env)

    restored = registry2.get(info.id)
    assert restored is not None
    assert restored.status is SubsessionStatus.CLOSED
    assert isinstance(restored, SubsessionInfo)
