"""Tests for the startup resume hook (``resume_subsessions``)."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.subsessions import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionRegistry,
    SubsessionStatus,
    resume_subsessions,
)
from robotsix_chat.subsessions.resume import (
    _entry_last_assistant_text,
    _entry_recent_user_texts,
)
from robotsix_chat.subsessions.worker import (
    _build_ancestor_context,
)
from tests.common.subsession_fakes import (
    FakeAgent,
    build_env,
    make_settings,
    wait_until,
)

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

    # -- task: re-spawned — task worker re-launched with restart-augmented
    #    prompt; the worker is alive and the prompt carries the restart note.
    task = registry.get(ids["task"])
    assert task is not None
    assert task.kind is SubsessionKind.TASK
    assert task.status in (SubsessionStatus.RUNNING, SubsessionStatus.CLOSED)
    assert "one-shot task was interrupted by a server restart" in task.prompt
    assert ids["task"] in registry._running
    task_worker = registry._running.get(ids["task"])

    history = env.conversation_store.history(OWNER)
    labels = [label for label, _ in history]
    # No longer marked as interrupted — the task is re-spawned.
    assert not any(
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
    # Periodic monitors are autonomous and resume silently — they are
    # excluded from the restart notice to avoid parent-agent noise.
    assert f'Periodic "watch CI" ({ids["periodic"][:8]})' not in notice
    assert f'Task "one shot" ({ids["task"][:8]})' in notice
    assert "resumed" in notice
    # Terminal entries are not listed.
    assert ids["closed"][:8] not in notice

    # -- closed: restored as-is, no worker, no new report -------------------
    closed = registry.get(ids["closed"])
    assert closed is not None
    assert closed.status is SubsessionStatus.CLOSED
    assert closed.summary == "finished earlier"
    assert ids["closed"] not in registry._running
    assert not any(ids["closed"][:8] in label for label in labels)

    # Cleanup the live workers.
    registry.cancel_and_close(ids["periodic"], reason="teardown", closed_by="system")
    registry.cancel_and_close(ids["task"], reason="teardown", closed_by="system")
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)
    if task_worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task_worker, 2.0)


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
async def test_resume_user_chat_waiting_does_not_redrive_agent(
    tmp_path: Path,
) -> None:
    """A user_chat waiting for the operator resumes straight into WAITING.

    The question was already delivered before the restart, so no agent
    turn runs (it would only re-ask the same question at frontier-tier
    cost) and the prompt is left untouched.  The operator's eventual
    reply is then handled as a normal turn with the question in history.
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
    raw = registry1.get(user_chat.id)
    assert raw is not None
    assert raw.last_result is None

    agent = FakeAgent(["Staging it is."])
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(agent=agent, registry=registry2, settings=make_settings())
    resume_subsessions(env)

    await wait_until(
        lambda: (registry2.get(user_chat.id) or raw).status is SubsessionStatus.WAITING
    )
    resumed = registry2.get(user_chat.id)
    assert resumed is not None
    # No agent turn ran and the prompt carries no restart note.
    assert agent.calls == []
    assert resumed.prompt == "Ask the user about the deployment strategy"
    assert "restarted after a server restart" not in resumed.prompt

    # The operator answers — that is the first (and only) agent turn, and
    # the agent sees its own question as history.
    assert registry2.enqueue_message(user_chat.id, "user", "Staging, please.")
    await wait_until(lambda: len(agent.calls) == 1)
    assert "Staging, please." in agent.calls[0]["message"]
    assert any(
        reply == "Hello! What environment?" for _, reply in agent.calls[0]["history"]
    )

    worker = registry2._running.get(user_chat.id)
    if worker is not None:
        registry2.cancel_and_close(user_chat.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_user_chat_waiting_survives_consecutive_restarts(
    tmp_path: Path,
) -> None:
    """A second restart with no turn in between must not re-ask either.

    A resume re-creates the registry entry without its transcript; since the
    waiting path runs no agent turn, nothing repopulates it.  The question is
    still recoverable from ``turn_history``, which does survive resumes —
    observed live 2026-08-28: restart #1 skipped the turn, restart #2 re-asked.
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
    registry1.set_status(user_chat.id, SubsessionStatus.WAITING)
    registry1.append_turn_history(
        user_chat.id, "Ask the user about the deployment strategy", "Which env?"
    )
    registry1.append_transcript(user_chat.id, "assistant", "Which env?")

    async def _restart(agent: FakeAgent) -> SubsessionRegistry:
        registry = SubsessionRegistry(store_path=store_path)
        env = build_env(agent=agent, registry=registry, settings=make_settings())
        resume_subsessions(env)
        await wait_until(
            lambda: (
                (registry.get(user_chat.id) or user_chat).status
                is SubsessionStatus.WAITING
            )
        )
        # Persist the resumed state (as a live server does continuously),
        # then stop the worker as a shutdown would.
        registry.persist()
        worker = registry._running.get(user_chat.id)
        if worker is not None:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2.0)
        return registry

    agent1 = FakeAgent()
    registry2 = await _restart(agent1)
    assert agent1.calls == []
    resumed = registry2.get(user_chat.id)
    assert resumed is not None and resumed.transcript == []  # the trigger

    agent2 = FakeAgent()
    registry3 = await _restart(agent2)
    assert agent2.calls == []
    resumed3 = registry3.get(user_chat.id)
    assert resumed3 is not None
    assert resumed3.prompt == "Ask the user about the deployment strategy"
    assert [reply for _, reply in resumed3.turn_history] == ["Which env?"]


@pytest.mark.asyncio
async def test_resume_user_chat_strips_stacked_restart_notes(
    tmp_path: Path,
) -> None:
    """Notes appended by earlier resumes are stripped before augmenting again.

    Previously each restart appended another restart note to the persisted
    prompt, so a user_chat that lived through N restarts carried N copies.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    original = "Ask the user about the deployment strategy"
    stale = (
        f"{original}\n\n"
        "[System note: this subsession was restarted after a server restart. "
        "The assistant's last delivered state was:]\n\nWhich environment?\n\n"
        "[System note: this subsession was restarted after a server restart. "
        "The assistant's last delivered state was:]\n\nWhich environment?"
    )
    user_chat = registry1.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="decision chat",
        prompt=stale,
        model_level=3,
    )
    registry1.set_status(user_chat.id, SubsessionStatus.WAITING)
    registry1.append_transcript(user_chat.id, "assistant", "Which environment?")
    # An answer that arrived just before the restart forces the augment path.
    registry1.append_transcript(user_chat.id, "user", "Production.")

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
    assert resumed.prompt.startswith(original)
    assert resumed.prompt.count("restarted after a server restart") == 1
    assert resumed.prompt.count("may not have been seen by the assistant") == 1
    assert "Production." in resumed.prompt

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


def test_entry_recent_user_texts_after_last_completed_turn() -> None:
    """Only user/parent transcript entries after the last turn are returned."""
    turn_history = [("Which environment?", "Staging, please.")]
    entry = {
        "transcript": [
            {"role": "assistant", "text": "Staging, please.", "timestamp": 1.0},
            {
                "role": "parent",
                "text": "No, deploy to production instead.",
                "timestamp": 2.0,
            },
        ]
    }
    assert _entry_recent_user_texts(entry, turn_history) == [
        ("parent", "No, deploy to production instead.")
    ]


def test_entry_recent_user_texts_skips_messages_from_completed_turns() -> None:
    """User/parent messages whose turn already completed are not re-injected."""
    turn_history = [
        ("Which environment?", "Staging, please."),
        ("No, deploy to production instead.", "Understood, deploying."),
    ]
    entry = {
        "transcript": [
            {"role": "assistant", "text": "Staging, please.", "timestamp": 1.0},
            {
                "role": "parent",
                "text": "No, deploy to production instead.",
                "timestamp": 2.0,
            },
            {"role": "assistant", "text": "Understood, deploying.", "timestamp": 3.0},
        ]
    }
    assert _entry_recent_user_texts(entry, turn_history) == []


def test_entry_recent_user_texts_without_turn_history_includes_all() -> None:
    """With no completed turns yet, every user/parent message is recent."""
    entry = {
        "transcript": [
            {"role": "parent", "text": "Use production.", "timestamp": 1.0},
        ]
    }
    assert _entry_recent_user_texts(entry, []) == [("parent", "Use production.")]


@pytest.mark.asyncio
async def test_resume_user_chat_injects_operator_answer_into_first_turn(
    tmp_path: Path,
) -> None:
    """A user_chat resumed after restart sees an answer given just before it.

    The operator's answer was transcripted at enqueue time but never became
    a completed turn, so it is absent from ``turn_history``.  The resume
    hook must retain the prior turn history and inject the undelivered
    answer into the first resumed turn's input.
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
    # A completed turn whose reply is recorded in turn_history + transcript.
    registry1.append_turn_history(
        user_chat.id, "Which environment?", "Staging, please."
    )
    registry1.append_transcript(user_chat.id, "assistant", "Staging, please.")
    # The operator's answer lands just before restart and is only
    # transcripted — no matching turn_history entry exists yet.
    registry1.append_transcript(
        user_chat.id, "parent", "No, deploy to production instead."
    )
    registry1.set_status(user_chat.id, SubsessionStatus.WAITING)

    gate = asyncio.Event()
    agent = FakeAgent(["Understood — deploying to production."], gate=gate)
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(agent=agent, registry=registry2, settings=make_settings())
    resume_subsessions(env)

    # The worker seeds its agent-visible history from info.turn_history and
    # blocks on the gate after recording its first call.
    for _ in range(200):
        if agent.calls:
            break
        await asyncio.sleep(0.01)
    assert agent.calls, "resumed worker never reached its first agent turn"

    first_call = agent.calls[0]
    assert "No, deploy to production instead." in first_call["message"]
    assert first_call["history"] == [("Which environment?", "Staging, please.")]

    resumed = registry2.get(user_chat.id)
    assert resumed is not None
    assert resumed.turn_history == [("Which environment?", "Staging, please.")]

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
async def test_restart_notice_deduplicates_identical_task_entries(
    tmp_path: Path,
) -> None:
    """Identical task entries for the same owner are collapsed into one line.

    When a session has multiple task subsessions with the same title,
    the restart notice should group them into a single line with a count
    instead of repeating the same message verbatim.

    (Periodic monitors are autonomous and excluded from the restart
    notice — only task and user_chat entries trigger it.)
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)

    # Create 5 task entries with the same title.
    ids = []
    for _ in range(5):
        task = registry1.create(
            kind=SubsessionKind.TASK,
            owner_session_id=OWNER,
            parent_id=None,
            depth=1,
            title="Run 42e0",
            prompt="run the build",
            model_level=3,
        )
        ids.append(task.id)

    # Also create one task with a *different* title.
    other = registry1.create(
        kind=SubsessionKind.TASK,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="Run abc1",
        prompt="run the deploy",
        model_level=3,
    )

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

    # The 5 identical "Run 42e0" entries should be collapsed into one
    # line showing "5 instances".
    assert "5 instances" in notice
    assert "Run 42e0" in notice
    # The distinct "Run abc1" should still appear as a separate entry.
    assert "Run abc1" in notice
    assert other.id[:8] in notice

    # Cleanup.
    for sub_id in [*ids, other.id]:
        worker = registry2._running.get(sub_id)
        if worker is not None:
            registry2.cancel_and_close(sub_id, reason="teardown", closed_by="system")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_restart_notice_multiple_owners_each_get_own_notice(
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

    # Tasks are now re-spawned — workers need a gate so they don't
    # complete (and close themselves) before the assertions run.
    gate = asyncio.Event()
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["ok"], gate=gate),
        registry=registry2,
    )
    resume_subsessions(env)

    # Owner A's notice mentions only task A.
    history_a = env.conversation_store.history(owner_a)
    notices_a = [
        label for label, _ in history_a if "the chat service was restarted" in label
    ]
    assert len(notices_a) == 1
    assert f'Task "task A" ({task_a.id[:8]})' in notices_a[0]
    assert "resumed" in notices_a[0]
    assert task_b.id[:8] not in notices_a[0]

    # Owner B's notice mentions only task B.
    history_b = env.conversation_store.history(owner_b)
    notices_b = [
        label for label, _ in history_b if "the chat service was restarted" in label
    ]
    assert len(notices_b) == 1
    assert f'Task "task B" ({task_b.id[:8]})' in notices_b[0]
    assert "resumed" in notices_b[0]
    assert task_a.id[:8] not in notices_b[0]

    # Cleanup workers.
    for sub_id in (task_a.id, task_b.id):
        worker = registry2._running.get(sub_id)
        registry2.cancel_and_close(sub_id, reason="teardown", closed_by="system")
        if worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2.0)


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


# ---------------------------------------------------------------------------
# periodic resume with terminal last_known_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_periodic_skips_when_last_known_state_is_terminal(
    tmp_path: Path,
) -> None:
    """A periodic monitor whose last_known_state is terminal is NOT resumed.

    When a ticket monitor was cleanly stopped before a restart (e.g. the
    ticket reached a terminal state and the monitor called
    complete_subsession), the persisted checkpoint records the terminal
    state.  On restart the resume hook must close the subsession without
    spawning a worker — otherwise the monitor would poll a ticket that
    no longer needs monitoring.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket ff0a",
        prompt="monitor the ticket",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={"ticket_id": "ff0a", "last_known_state": "closed"},
    )
    registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=3)

    gate = asyncio.Event()
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["should not run"], gate=gate),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    # The subsession must be CLOSED — not active, not running.
    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "ticket_terminal_on_resume"
    assert periodic.id not in registry2._running

    # No restart notice should mention this subsession.
    history = env.conversation_store.history(OWNER)
    restart_notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    # There may be zero notices (no other active subsessions) or notices
    # that don't mention this one.
    for notice in restart_notices:
        assert periodic.id[:8] not in notice
        assert "watch ticket ff0a" not in notice


@pytest.mark.asyncio
async def test_resume_periodic_still_resumes_when_last_known_state_is_open(
    tmp_path: Path,
) -> None:
    """A periodic monitor with a non-terminal last_known_state resumes normally."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket abc1",
        prompt="monitor the ticket",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={"ticket_id": "abc1", "last_known_state": "in_progress"},
    )
    registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=1)

    gate = asyncio.Event()
    agent = FakeAgent(["still watching"], gate=gate)
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=agent,
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status in (SubsessionStatus.RUNNING, SubsessionStatus.SLEEPING)
    assert periodic.id in registry2._running

    # Cleanup.
    worker = registry2._running.get(periodic.id)
    if worker is not None:
        registry2.cancel_and_close(periodic.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_periodic_skips_when_last_known_state_is_done(
    tmp_path: Path,
) -> None:
    """A periodic monitor whose last_known_state is 'done' is also terminal."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch done ticket",
        prompt="monitor the ticket",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={"ticket_id": "abc2", "last_known_state": "done"},
    )
    registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=2)

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["should not run"]),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert periodic.id not in registry2._running


@pytest.mark.asyncio
async def test_resume_periodic_skips_when_last_known_state_case_insensitive(
    tmp_path: Path,
) -> None:
    """Case of last_known_state does not matter — 'Closed' is still terminal."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch mixed case",
        prompt="monitor",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={"ticket_id": "abc3", "last_known_state": "Closed"},
    )
    registry1.set_status(periodic.id, SubsessionStatus.SLEEPING, runs=1)

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["should not run"]),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert periodic.id not in registry2._running


# -- duplicate consecutive restart notice suppression ----------------------


def test_restart_notice_suppressed_when_identical_to_previous(
    tmp_path: Path,
) -> None:
    """A duplicate consecutive restart notice is suppressed.

    When the chat service restarts and the background-task state is
    unchanged, the new restart notice is identical to the one already
    present in the conversation.  The second injection must be a no-op
    — the transcript must still contain exactly one restart notice.
    """
    from robotsix_chat.subsessions.resume import _inject_restart_notice, _ResumeFate

    store = ConversationStore()
    registry = SubsessionRegistry(store_path=tmp_path / "subsessions.json")
    env = build_env(store=store, registry=registry)

    owner_id = "sess-dup-test"
    fates: list[_ResumeFate] = [
        _ResumeFate(
            owner_session_id=owner_id,
            sub_id="abc12345-1111-2222-3333-444444444444",
            kind="periodic",
            title="watch CI",
            fate="resumed",
            detail="Will continue ticking on its normal schedule.",
        ),
    ]

    # First injection — the notice is written.
    _inject_restart_notice(env, owner_id, fates)
    history = env.conversation_store.history(owner_id)
    notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(notices) == 1
    assert "watch CI" in notices[0]

    # Second injection with identical fates — must be suppressed.
    _inject_restart_notice(env, owner_id, fates)
    history = env.conversation_store.history(owner_id)
    notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(notices) == 1, "duplicate identical restart notice was not suppressed"


def test_restart_notice_not_suppressed_when_different_from_previous(
    tmp_path: Path,
) -> None:
    """A restart notice with new information is NOT suppressed.

    When the background-task state changes between restarts (e.g. a new
    subsession appeared), the new notice carries different content and
    must be written — the transcript should contain both notices.
    """
    from robotsix_chat.subsessions.resume import _inject_restart_notice, _ResumeFate

    store = ConversationStore()
    registry = SubsessionRegistry(store_path=tmp_path / "subsessions.json")
    env = build_env(store=store, registry=registry)

    owner_id = "sess-diff-test"

    # First restart: one periodic subsession.
    fates_v1: list[_ResumeFate] = [
        _ResumeFate(
            owner_session_id=owner_id,
            sub_id="abc12345-1111-2222-3333-444444444444",
            kind="periodic",
            title="watch CI",
            fate="resumed",
            detail="Will continue ticking on its normal schedule.",
        ),
    ]
    _inject_restart_notice(env, owner_id, fates_v1)

    # Second restart: a new task subsession appeared.
    fates_v2: list[_ResumeFate] = [
        _ResumeFate(
            owner_session_id=owner_id,
            sub_id="abc12345-1111-2222-3333-444444444444",
            kind="periodic",
            title="watch CI",
            fate="resumed",
            detail="Will continue ticking on its normal schedule.",
        ),
        _ResumeFate(
            owner_session_id=owner_id,
            sub_id="def67890-aaaa-bbbb-cccc-dddddddddddd",
            kind="task",
            title="new task",
            fate="resumed",
            detail="Re-enqueued — the task will restart from its original prompt.",
        ),
    ]
    _inject_restart_notice(env, owner_id, fates_v2)

    history = env.conversation_store.history(owner_id)
    notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(notices) == 2, (
        "a different restart notice should NOT have been suppressed"
    )
    assert "new task" in notices[1]
    assert "watch CI" in notices[0]


def test_restart_notice_suppressed_with_intervening_messages(
    tmp_path: Path,
) -> None:
    """Duplicate restart notices are suppressed even with intervening messages.

    When the server restarts, a restart notice is injected.  If the user
    then sends messages and the server restarts again with the same
    background-task state, the second notice must still be suppressed —
    the previous turn-only check missed duplicates separated by normal
    conversation turns.
    """
    from robotsix_chat.subsessions.resume import _inject_restart_notice, _ResumeFate

    store = ConversationStore()
    registry = SubsessionRegistry(store_path=tmp_path / "subsessions.json")
    env = build_env(store=store, registry=registry)

    owner_id = "sess-intervening-test"
    fates: list[_ResumeFate] = [
        _ResumeFate(
            owner_session_id=owner_id,
            sub_id="abc12345-1111-2222-3333-444444444444",
            kind="periodic",
            title="watch CI",
            fate="resumed",
            detail="Will continue ticking on its normal schedule.",
        ),
    ]

    # First restart — notice is injected.
    _inject_restart_notice(env, owner_id, fates)
    history = env.conversation_store.history(owner_id)
    notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(notices) == 1

    # User sends a message and gets a reply — intervening turns.
    env.conversation_store.record_for_session(
        owner_id, "Hello, are you there?", "Yes, I'm here!"
    )
    env.conversation_store.record_for_session(
        owner_id, "What's the status?", "All systems operational."
    )

    # Second restart with identical fates — must still be suppressed.
    _inject_restart_notice(env, owner_id, fates)
    history = env.conversation_store.history(owner_id)
    notices = [
        label for label, _ in history if "the chat service was restarted" in label
    ]
    assert len(notices) == 1, (
        "duplicate restart notice was not suppressed — intervening messages "
        "should not defeat dedup"
    )


# -- auto-close re-spawn on restart ---------------------------------------


@pytest.mark.asyncio
async def test_resume_periodic_respawns_auto_closed_no_change(
    tmp_path: Path,
) -> None:
    """Re-spawn auto-closed periodic monitors on restart.

    A periodic monitor auto-closed with 'no_change_auto_stop' is re-spawned
    so the worker can re-verify the ticket state.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket a1b2",
        prompt="monitor the ticket",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={"ticket_id": "a1b2", "last_known_state": "in_progress"},
    )
    registry1.mark_closed(
        periodic.id,
        summary="auto-stopped",
        reason="no_change_auto_stop",
        closed_by="system",
    )

    gate = asyncio.Event()
    agent = FakeAgent(["resumed after auto-stop"], gate=gate)
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=agent,
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status in (SubsessionStatus.RUNNING, SubsessionStatus.SLEEPING)
    assert periodic.id in registry2._running

    # Cleanup.
    worker = registry2._running.get(periodic.id)
    if worker is not None:
        registry2.cancel_and_close(periodic.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_periodic_respawns_auto_closed_paused(
    tmp_path: Path,
) -> None:
    """Re-spawn auto-closed periodics with 'paused' reason on restart.

    A periodic monitor auto-closed with 'paused' (max_idle_runs) is
    re-spawned on restart.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket c3d4",
        prompt="monitor",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={"ticket_id": "c3d4", "last_known_state": "open"},
    )
    registry1.mark_closed(
        periodic.id,
        summary="paused after idle",
        reason="paused",
        closed_by="system",
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["woke up"]),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status in (SubsessionStatus.RUNNING, SubsessionStatus.SLEEPING)
    assert periodic.id in registry2._running

    worker = registry2._running.get(periodic.id)
    if worker is not None:
        registry2.cancel_and_close(periodic.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_periodic_respawns_auto_closed_human_approval_timeout(
    tmp_path: Path,
) -> None:
    """Re-spawn auto-closed periodics with human_approval_timeout.

    A periodic monitor auto-closed with 'human_approval_timeout' is
    re-spawned on restart.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket e5f6",
        prompt="monitor",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={
            "ticket_id": "e5f6",
            "last_known_state": "human_issue_approval",
        },
    )
    registry1.mark_closed(
        periodic.id,
        summary="human approval timeout",
        reason="human_approval_timeout",
        closed_by="system",
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["approval resolved"]),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status in (SubsessionStatus.RUNNING, SubsessionStatus.SLEEPING)
    assert periodic.id in registry2._running

    worker = registry2._running.get(periodic.id)
    if worker is not None:
        registry2.cancel_and_close(periodic.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_periodic_does_not_respawn_explicitly_closed(
    tmp_path: Path,
) -> None:
    """Do NOT re-spawn periodics closed explicitly by the agent.

    A periodic monitor closed explicitly by the agent (reason='completed')
    is NOT re-spawned on restart — the agent intentionally finished it.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch done ticket",
        prompt="monitor",
        model_level=3,
        interval_seconds=0.05,
        max_runs=10,
        checkpoint={"ticket_id": "done1", "last_known_state": "closed"},
    )
    registry1.mark_closed(
        periodic.id,
        summary="ticket completed",
        reason="completed",
        closed_by="agent",
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["should not run"]),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert periodic.id not in registry2._running


@pytest.mark.asyncio
async def test_resume_periodic_does_not_respawn_max_runs(
    tmp_path: Path,
) -> None:
    """Do NOT re-spawn periodics closed due to max_runs.

    A periodic monitor closed due to max_runs is NOT re-spawned —
    the user deliberately capped its run budget.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    periodic = registry1.create(
        kind=SubsessionKind.PERIODIC,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch limited ticket",
        prompt="monitor",
        model_level=3,
        interval_seconds=0.05,
        max_runs=3,
        checkpoint={"ticket_id": "lim1", "last_known_state": "open"},
    )
    registry1.mark_closed(
        periodic.id,
        summary="max runs reached",
        reason="max_runs",
        closed_by="system",
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["should not run"]),
        registry=registry2,
        settings=make_settings(min_interval_seconds=0.01),
    )
    resume_subsessions(env)

    info = registry2.get(periodic.id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert periodic.id not in registry2._running


@pytest.mark.asyncio
async def test_resume_wait_for_event_respawns_auto_closed_human_approval_timeout(
    tmp_path: Path,
) -> None:
    """Re-spawn auto-closed wait_for_event monitors on restart.

    A wait_for_event monitor auto-closed with 'human_approval_timeout'
    is re-spawned so the worker can re-verify the live ticket state.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    wfe = registry1.create(
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch ticket a1b2",
        prompt="monitor the ticket",
        model_level=3,
        checkpoint={"ticket_id": "a1b2", "last_known_state": "human_issue_approval"},
        dedup_key="a1b2",
        event_timeout_seconds=3600.0,
    )
    registry1.mark_closed(
        wfe.id,
        summary="auto-escalated after timeout",
        reason="human_approval_timeout",
        closed_by="system",
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["resumed after auto-close"]),
        registry=registry2,
    )
    resume_subsessions(env)

    info = registry2.get(wfe.id)
    assert info is not None
    assert info.status in (SubsessionStatus.RUNNING, SubsessionStatus.SLEEPING)
    assert wfe.id in registry2._running

    worker = registry2._running.get(wfe.id)
    if worker is not None:
        registry2.cancel_and_close(wfe.id, reason="teardown", closed_by="system")
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_resume_wait_for_event_does_not_respawn_explicitly_closed(
    tmp_path: Path,
) -> None:
    """Do NOT re-spawn wait_for_event monitors closed explicitly.

    A wait_for_event monitor closed explicitly by the agent
    (reason='completed') is NOT re-spawned on restart.
    """
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    wfe = registry1.create(
        kind=SubsessionKind.WAIT_FOR_EVENT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="watch done ticket",
        prompt="monitor",
        model_level=3,
        checkpoint={"ticket_id": "done1", "last_known_state": "closed"},
        dedup_key="done1",
        event_timeout_seconds=3600.0,
    )
    registry1.mark_closed(
        wfe.id,
        summary="ticket completed",
        reason="completed",
        closed_by="agent",
    )

    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(
        agent=FakeAgent(["should not run"]),
        registry=registry2,
    )
    resume_subsessions(env)

    info = registry2.get(wfe.id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert wfe.id not in registry2._running


@pytest.mark.asyncio
async def test_resume_restores_undelivered_user_chat_message_once(
    tmp_path: Path,
) -> None:
    """A message enqueued before restart is delivered exactly once on resume."""
    store_path = tmp_path / "subsessions.json"
    registry1 = SubsessionRegistry(store_path=store_path)
    chat = registry1.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id=OWNER,
        parent_id=None,
        depth=1,
        title="side chat",
        prompt="what should we do?",
        model_level=3,
    )
    registry1.set_status(chat.id, SubsessionStatus.WAITING)
    assert registry1.enqueue_message(chat.id, "user", "deploy the fix") is True
    registry1.persist()

    agent = FakeAgent(["sure, deploying", "acknowledged"])
    registry2 = SubsessionRegistry(store_path=store_path)
    env = build_env(agent=agent, registry=registry2, settings=make_settings())

    resume_subsessions(env)

    await wait_until(lambda: any(c["message"] == "deploy the fix" for c in agent.calls))
    assert sum(1 for c in agent.calls if c["message"] == "deploy the fix") == 1

    worker = registry2._running.get(chat.id)
    registry2.cancel_and_close(chat.id, reason="teardown", closed_by="system")
    if worker is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker, 2.0)
