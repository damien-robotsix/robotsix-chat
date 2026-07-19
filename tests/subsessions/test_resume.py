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
from robotsix_chat.subsessions.resume import (
    _entry_last_assistant_text,
)
from robotsix_chat.subsessions.worker import (
    _build_ancestor_context,
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
    # The budget and the executed-run counter both survive the restart —
    # the effective remaining budget (5 - 2 = 3) falls out of the
    # ``runs >= max_runs`` check instead of a rebudget.
    assert periodic.max_runs == 5
    assert periodic.runs == 2
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

    # -- restart notice: injected into the conversation ---------------------
    restart_notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(restart_notices) == 1, (
        "expected exactly one restart notice per affected conversation"
    )
    notice = restart_notices[0]
    assert f'Periodic "watch CI" ({ids["periodic"][:8]})' in notice
    assert "resumed" in notice
    assert f'Task "one shot" ({ids["task"][:8]})' in notice
    assert "interrupted" in notice
    # Terminal entries are not listed.
    assert ids["closed"][:8] not in notice

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
async def test_resume_periodic_restores_run_counter_and_budget(
    tmp_path: Path,
) -> None:
    """The run counter and original ``max_runs`` both survive a restart.

    An exhausted budget still allows exactly one resumed run: the
    ``runs >= max_runs`` check fires after that run completes.
    """
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
    assert resumed.max_runs == 3
    assert resumed.runs == 3

    worker = registry2._running.get(periodic.id)
    registry2.cancel_and_close(periodic.id, reason="teardown", closed_by="system")
    if worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_periodic_does_not_replay_completed_runs(
    tmp_path: Path,
) -> None:
    """A resumed periodic worker executes the NEXT run immediately.

    Regression: resume used to seed ``completed_runs`` but restart the
    counter at 0, so the worker collided with every historical run
    number and slept one full interval per collision — with a long
    interval and regular restarts the subsession never ran again.  The
    60 s interval here makes any such sleep fail the test's 2 s wait.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="long watch",
        prompt="check the board",
        model_level=3,
        interval_seconds=60.0,
        max_runs=10,
    )
    for run_n in (1, 2, 3):
        assert registry1.claim_run(periodic.id, run_n)
    registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=3)

    agent = FakeAgent(["run 4 result"], gate=asyncio.Event())
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=agent,
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    resumed = registry2.get(periodic.id)
    assert resumed is not None
    assert resumed.runs == 3
    assert resumed.completed_runs == {1, 2, 3}

    # The first turn (run 4) must start promptly — a worker that
    # replays runs 1..3 sleeps 60 s per replay and never gets here.
    for _ in range(200):
        if agent.calls:
            break
        await asyncio.sleep(0.01)
    assert agent.calls, "resumed worker never reached its next run"
    assert 4 in resumed.completed_runs

    worker = registry2._running.get(periodic.id)
    registry2.cancel_and_close(periodic.id, reason="teardown", closed_by="system")
    if worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_periodic_seeds_history_from_turn_history(
    tmp_path: Path,
) -> None:
    """A resumed periodic worker replays its persisted turn_history.

    Without this, a chat restart would blank a long-running periodic
    subsession's context on every resume — the agent would start its
    next run with no memory of anything it learned or decided in prior
    runs, and any nested subsession it spawns inherits that gap too.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch board",
        prompt="sweep the board",
        model_level=3,
        interval_seconds=0.05,
        max_runs=5,
    )
    registry1.append_turn_history(periodic.id, "sweep the board", "approved 3 MRs")
    registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=1)

    gate = asyncio.Event()
    agent = FakeAgent(["resumed with context"], gate=gate)
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=agent,
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )

    resume_subsessions(env)

    # FakeAgent records the call before blocking on the gate, so just
    # yielding to the event loop is enough for the resumed worker's
    # first turn to reach the agent.
    for _ in range(50):
        if agent.calls:
            break
        await asyncio.sleep(0.01)
    assert agent.calls, "resumed worker never called the agent"
    assert agent.calls[0]["history"] == [("sweep the board", "approved 3 MRs")]

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


# ---------------------------------------------------------------------------
# _build_ancestor_context
# ---------------------------------------------------------------------------


@pytest.fixture
def reg() -> SubsessionRegistry:
    """Provide a fresh in-memory registry for ancestor-context tests."""
    return SubsessionRegistry(store_path=None)


def test_build_ancestor_context_empty_chain(reg: SubsessionRegistry) -> None:
    """Return empty when parent_id points to non-existent or root."""
    result = _build_ancestor_context(reg, "nonexistent")
    assert result == ""


def test_build_ancestor_context_single_ancestor(reg: SubsessionRegistry) -> None:
    """Include one ancestor entry."""
    parent = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="root task",
        prompt="Monitor the build pipeline status",
        model_level=3,
    )
    result = _build_ancestor_context(reg, parent.id)
    assert "# Ancestor context (inherited from the subsession tree above you)" in result
    assert "## root task" in result
    assert "Monitor the build pipeline status" in result


def test_build_ancestor_context_chain_of_two(reg: SubsessionRegistry) -> None:
    """Order ancestors root-first, not leaf-first."""
    root = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="root",
        prompt="Root prompt",
        model_level=3,
    )
    child = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=root.id,
        depth=2,
        title="child",
        prompt="Child prompt",
        model_level=3,
    )
    result = _build_ancestor_context(reg, child.id)
    root_idx = result.index("## root")
    child_idx = result.index("## child")
    assert root_idx < child_idx
    assert "Root prompt" in result
    assert "Child prompt" in result


def test_build_ancestor_context_respects_budget(reg: SubsessionRegistry) -> None:
    """Drop entries exceeding the character budget."""
    parent = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="long title " + "x" * 200,
        prompt="p" * 2000,
        model_level=3,
    )
    result = _build_ancestor_context(reg, parent.id)
    assert "p" * 300 in result
    assert "p" * 301 not in result


def test_build_ancestor_context_all_exceed_budget(reg: SubsessionRegistry) -> None:
    """Return empty when the first ancestor already exceeds budget."""
    parent = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="x" * 2000,
        prompt="y" * 2000,
        model_level=3,
    )
    result = _build_ancestor_context(reg, parent.id)
    assert result == ""


def test_build_ancestor_context_three_generation(reg: SubsessionRegistry) -> None:
    """Include three generations when all fit within budget."""
    root = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="grandparent",
        prompt="Grandparent instructions",
        model_level=3,
    )
    mid = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=root.id,
        depth=2,
        title="parent",
        prompt="Parent instructions",
        model_level=3,
    )
    leaf = reg.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=mid.id,
        depth=3,
        title="child",
        prompt="Child instructions",
        model_level=3,
    )
    result = _build_ancestor_context(reg, leaf.id)
    assert "## grandparent" in result
    assert "## parent" in result
    assert "## child" in result
    assert (
        result.index("## grandparent")
        < result.index("## parent")
        < result.index("## child")
    )


# ---------------------------------------------------------------------------
# _entry_last_assistant_text
# ---------------------------------------------------------------------------


def test_last_assistant_text_uses_last_result_when_present() -> None:
    """When entry has last_result, it takes priority."""
    entry = {
        "last_result": "periodic reply",
        "transcript": [{"role": "assistant", "text": "ignored"}],
    }
    assert _entry_last_assistant_text(entry) == "periodic reply"


def test_last_assistant_text_falls_back_to_transcript() -> None:
    """Without last_result, the last assistant transcript entry is used."""
    entry = {
        "transcript": [
            {"role": "user", "text": "hello", "timestamp": 1.0},
            {"role": "assistant", "text": "hi there", "timestamp": 2.0},
            {"role": "user", "text": "tell me more", "timestamp": 3.0},
            {"role": "assistant", "text": "sure, here is the answer", "timestamp": 4.0},
        ]
    }
    assert _entry_last_assistant_text(entry) == "sure, here is the answer"


def test_last_assistant_text_empty_transcript() -> None:
    """When there are no assistant entries, returns empty string."""
    entry = {
        "transcript": [
            {"role": "user", "text": "hello", "timestamp": 1.0},
        ]
    }
    assert _entry_last_assistant_text(entry) == ""


def test_last_assistant_text_no_transcript_key() -> None:
    """When transcript key is missing entirely, returns empty string."""
    entry: dict[str, object] = {}
    assert _entry_last_assistant_text(entry) == ""


def test_last_assistant_text_non_list_transcript() -> None:
    """When transcript is not a list, returns empty string."""
    entry = {"transcript": "not a list"}
    assert _entry_last_assistant_text(entry) == ""


# ---------------------------------------------------------------------------
# user_chat resume with transcript
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_user_chat_uses_transcript_for_restart_note(
    tmp_path: Path,
) -> None:
    """Use transcript to provide restart note when last_result is unset.

    A user_chat entry's transcript provides the last assistant text for
    the restart note, even when last_result is None (which is the normal
    case — user_chat never writes last_result).
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    user_chat = registry1.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="decision chat",
        prompt="Ask the user about the deployment strategy",
        model_level=3,
    )
    # Simulate what a real user_chat does: set_status WAITING with no
    # last_result, and append assistant replies to the transcript.
    registry1.set_status(user_chat.id, SubsessionStatus.WAITING)
    registry1.append_transcript(user_chat.id, "assistant", "Hello! What environment?")
    registry1.append_transcript(
        user_chat.id, "assistant", "Sure, deploying to staging sounds good."
    )
    # last_result should be None (as with real user_chat set_status calls).
    raw = registry1.get(user_chat.id)
    assert raw is not None
    assert raw.last_result is None

    gate = asyncio.Event()
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["resumed"], gate=gate),
        registry=registry2,
        settings=make_settings(),
    )
    resume_subsessions(env)

    resumed = registry2.get(user_chat.id)
    assert resumed is not None
    assert resumed.status in (SubsessionStatus.RUNNING, SubsessionStatus.WAITING)
    # The prompt should include the last assistant text from the transcript.
    assert "Sure, deploying to staging sounds good." in resumed.prompt
    assert "restarted after a server restart" in resumed.prompt

    worker = registry2._running.get(user_chat.id)
    if worker is not None:
        registry2.cancel_and_close(user_chat.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_user_chat_no_augmentation_when_no_transcript(
    tmp_path: Path,
) -> None:
    """When the user_chat has no transcript, the prompt is not augmented."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    user_chat = registry1.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="fresh chat",
        prompt="Ask the user a question",
        model_level=3,
    )
    registry1.set_status(user_chat.id, SubsessionStatus.WAITING)

    gate = asyncio.Event()
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["resumed"], gate=gate),
        registry=registry2,
        settings=make_settings(),
    )
    resume_subsessions(env)

    resumed = registry2.get(user_chat.id)
    assert resumed is not None
    # No restart note should be added — prompt stays as-is.
    assert "restarted after a server restart" not in resumed.prompt
    assert resumed.prompt == "Ask the user a question"

    worker = registry2._running.get(user_chat.id)
    if worker is not None:
        registry2.cancel_and_close(user_chat.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


# ---------------------------------------------------------------------------
# restart notice injection
# ---------------------------------------------------------------------------


def test_restart_notice_not_injected_when_no_active_subsessions(
    tmp_path: Path,
) -> None:
    """No restart notice when all persisted entries are already terminal."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    closed = registry1.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="done job",
        prompt="already finished",
        model_level=3,
    )
    registry1.mark_closed(closed.id, summary="done", reason="completed")

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(registry=registry2)
    resume_subsessions(env)

    history = env.conversation_store.history(OWNER)
    restart_notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert restart_notices == []


@pytest.mark.asyncio
async def test_restart_notice_includes_user_chat_as_resumed(
    tmp_path: Path,
) -> None:
    """User_chat subsessions appear as 'resumed' in the restart notice."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    uc = registry1.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="side chat",
        prompt="discuss the plan",
        model_level=3,
    )
    registry1.set_status(uc.id, SubsessionStatus.WAITING)

    gate = asyncio.Event()
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["ok"], gate=gate),
        registry=registry2,
        settings=make_settings(),
    )
    resume_subsessions(env)

    history = env.conversation_store.history(OWNER)
    notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(notices) == 1
    notice = notices[0]
    assert f'User_chat "side chat" ({uc.id[:8]})' in notice
    assert "resumed" in notice

    worker = registry2._running.get(uc.id)
    if worker is not None:
        registry2.cancel_and_close(uc.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_restart_notice_deduplicates_identical_periodic_entries(
    tmp_path: Path,
) -> None:
    """Identical periodic entries for the same owner are collapsed into one line.

    When a monitor has multiple periodic subsessions with the same title,
    the restart notice should group them into a single line with a count
    instead of repeating the same message verbatim.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)

    # Create 5 periodic entries with the same title.
    ids = []
    for _ in range(5):
        periodic = registry1.create(
            kind=SubsessionKind.PERIODIC,
            owner_session_id=OWNER,
            parent_id=None,
            depth=1,
            title="Monitor 42e0",
            prompt="check the build",
            model_level=3,
            interval_seconds=0.05,
            max_runs=10,
        )
        registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=1)
        ids.append(periodic.id)

    # Also create one periodic with a *different* title.
    other = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="Monitor abc1",
        prompt="check the deploy",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
    )
    registry1.set_status(other.id, SubsessionStatus.SLEEPING, runs=1)

    gate = asyncio.Event()
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["ok"], gate=gate),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    history = env.conversation_store.history(OWNER)
    notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(notices) == 1
    notice = notices[0]

    # The 5 identical "Monitor 42e0" entries should be collapsed into one
    # line showing "5 instances".
    assert "5 instances" in notice
    assert "Monitor 42e0" in notice
    # The distinct "Monitor abc1" should still appear as a separate entry.
    assert "Monitor abc1" in notice
    assert other.id[:8] in notice

    # Cleanup.
    for sub_id in [*ids, other.id]:
        worker = registry2._running.get(sub_id)
        if worker is not None:
            registry2.cancel_and_close(sub_id, reason="teardown", closed_by="system")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2.0)


def test_restart_notice_multiple_owners_each_get_own_notice(
    tmp_path: Path,
) -> None:
    """Each affected owner gets a restart notice scoped to its subsessions."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    owner_a = "sess-a"
    owner_b = "sess-b"

    task_a = registry1.create(
        kind=SubsessionKind.TASK,
        owner_session_id=owner_a,
        parent_id=None,
        depth=1,
        title="task A",
        prompt="do A",
        model_level=3,
    )
    task_b = registry1.create(
        kind=SubsessionKind.TASK,
        owner_session_id=owner_b,
        parent_id=None,
        depth=1,
        title="task B",
        prompt="do B",
        model_level=3,
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(registry=registry2)
    resume_subsessions(env)

    # Owner A's notice mentions only task A.
    history_a = env.conversation_store.history(owner_a)
    notices_a = [
        label for label, _ in history_a if "the chat service was restarted" in label
    ]
    assert len(notices_a) == 1
    assert f'Task "task A" ({task_a.id[:8]})' in notices_a[0]
    assert task_b.id[:8] not in notices_a[0]

    # Owner B's notice mentions only task B.
    history_b = env.conversation_store.history(owner_b)
    notices_b = [
        label for label, _ in history_b if "the chat service was restarted" in label
    ]
    assert len(notices_b) == 1
    assert f'Task "task B" ({task_b.id[:8]})' in notices_b[0]
    assert task_a.id[:8] not in notices_b[0]


# ---------------------------------------------------------------------------
# _rebuild_checkpoint
# ---------------------------------------------------------------------------


def test_rebuild_checkpoint_valid_dict():
    """A valid dict checkpoint is reconstructed with string keys."""
    from robotsix_chat.subsessions.resume import _rebuild_checkpoint

    entry = {"checkpoint": {"ticket_id": "TICK-1", "last_known_state": "open"}}
    result = _rebuild_checkpoint(entry)
    assert result == {"ticket_id": "TICK-1", "last_known_state": "open"}


def test_rebuild_checkpoint_non_dict_returns_none():
    """Non-dict values (list, string, None) return None."""
    from robotsix_chat.subsessions.resume import _rebuild_checkpoint

    assert _rebuild_checkpoint({"checkpoint": [1, 2, 3]}) is None
    assert _rebuild_checkpoint({"checkpoint": "not a dict"}) is None
    assert _rebuild_checkpoint({"checkpoint": None}) is None


def test_rebuild_checkpoint_missing_field_returns_none():
    """A persisted entry without 'checkpoint' returns None."""
    from robotsix_chat.subsessions.resume import _rebuild_checkpoint

    assert _rebuild_checkpoint({}) is None


def test_rebuild_checkpoint_empty_dict():
    """An empty dict checkpoint is preserved as empty dict."""
    from robotsix_chat.subsessions.resume import _rebuild_checkpoint

    result = _rebuild_checkpoint({"checkpoint": {}})
    assert result == {}


def test_rebuild_checkpoint_coerces_keys_to_strings():
    """Integer keys (from loose JSON parsing) are coerced to strings."""
    from robotsix_chat.subsessions.resume import _rebuild_checkpoint

    # Python's json module never produces non-string keys, but _rebuild_checkpoint
    # defensively coerces them anyway.
    entry = {"checkpoint": {"ticket_id": "TICK-1", 42: "answer"}}
    result = _rebuild_checkpoint(entry)
    assert result == {"ticket_id": "TICK-1", "42": "answer"}
