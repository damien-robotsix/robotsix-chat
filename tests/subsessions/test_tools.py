"""Tests for the agent-facing subsession tool factory."""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.subsessions import (
    CloseState,
    SubsessionContext,
    SubsessionKind,
    SubsessionStatus,
    build_subsession_tools,
)
from tests.common.subsession_fakes import (
    FakeAgent,
    build_env,
    make_settings,
    wait_until,
)

OWNER = "sess-main"


def _ctx(*, subsession_id: str | None = None, depth: int = 0) -> SubsessionContext:
    """Build a ``SubsessionContext`` for the owning test session."""
    return SubsessionContext(
        owner_session_id=OWNER, subsession_id=subsession_id, depth=depth
    )


def _tool_names(tools: list[Any]) -> list[str]:
    """Return the ``__name__`` of each tool callable."""
    return [t.__name__ for t in tools]


def _by_name(tools: list[Any], name: str) -> Any:
    """Return the tool callable named *name*."""
    return next(t for t in tools if t.__name__ == name)


def _register(env: Any, *, owner: str = OWNER, **overrides: object) -> Any:
    """Register a subsession record directly (no worker)."""
    defaults: dict[str, object] = {
        "kind": SubsessionKind.TASK,
        "owner_session_id": owner,
        "parent_id": None,
        "depth": 1,
        "title": "job",
        "prompt": "p",
        "model_level": 3,
    }
    defaults.update(overrides)
    return env.registry.create(**defaults)


# ---------------------------------------------------------------------------
# tool-set composition
# ---------------------------------------------------------------------------


def test_main_agent_gets_spawn_control_tools_only() -> None:
    """Depth 0 without a close state → spawn/message/close/list, no self-close."""
    env = build_env()

    tools = build_subsession_tools(env, ctx=_ctx())

    assert _tool_names(tools) == [
        "spawn_subsession",
        "message_subsession",
        "close_subsession",
        "list_subsessions",
    ]


def test_subsession_agent_gets_complete_tool_too() -> None:
    """A subsession agent below max depth gets all five tools."""
    env = build_env()

    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-1", depth=1), close_state=CloseState()
    )

    assert _tool_names(tools) == [
        "spawn_subsession",
        "message_subsession",
        "close_subsession",
        "list_subsessions",
        "complete_subsession",
        "set_checkpoint",
        "self_update_subsession",
    ]


def test_agent_at_max_depth_gets_only_complete_tool() -> None:
    """At ``max_depth`` the agent cannot spawn — only ``complete_subsession``."""
    env = build_env(settings=make_settings(max_depth=2))

    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-1", depth=2), close_state=CloseState()
    )

    assert _tool_names(tools) == [
        "complete_subsession",
        "set_checkpoint",
        "self_update_subsession",
    ]


def test_no_close_state_at_max_depth_yields_no_tools() -> None:
    """No spawn room and no close state → an empty tool list."""
    env = build_env(settings=make_settings(max_depth=1))

    tools = build_subsession_tools(env, ctx=_ctx(subsession_id="sub-1", depth=1))

    assert tools == []


# ---------------------------------------------------------------------------
# spawn tool refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_tool_unknown_kind_polite_refusal() -> None:
    """An unknown kind string returns a refusal, not an exception."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn("banana", "t", "do it")

    assert "Unknown kind 'banana'" in result
    assert "task" in result
    assert env.registry.list_for_owner(OWNER) == []


@pytest.mark.asyncio
async def test_spawn_tool_capacity_refusal() -> None:
    """A full registry maps ``SubsessionCapacityError`` to a refusal string."""
    env = build_env(settings=make_settings(max_concurrent=1))
    _register(env)  # occupies the single slot (active, no worker)
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn("task", "t", "do it")

    assert result.startswith("Could not start the subsession:")
    assert "capacity" in result
    assert "list_subsessions" in result
    assert "poll the ticket/subject manually" in result
    assert "Do NOT retry in a tight loop" in result


@pytest.mark.asyncio
async def test_spawn_tool_depth_refusal() -> None:
    """A depth overflow maps ``SubsessionDepthError`` to a refusal string."""
    env = build_env(settings=make_settings(max_depth=2))
    tools = build_subsession_tools(env, ctx=_ctx(subsession_id="sub-1", depth=1))
    spawn = _by_name(tools, "spawn_subsession")
    # Tighten the depth limit after tool construction so the spawn
    # (depth 2) now exceeds it — exercising the refusal mapping.
    env.settings.subsessions.max_depth = 1

    result = await spawn("task", "t", "do it")

    assert result.startswith("Could not start the subsession:")
    assert "depth" in result


@pytest.mark.asyncio
async def test_spawn_tool_invalid_level_refusal() -> None:
    """Model level 5 maps ``SubsessionLevelError`` to a refusal string."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn("task", "t", "do it", model_level=5)

    assert result.startswith("Could not start the subsession:")
    assert "model_level" in result


@pytest.mark.asyncio
async def test_spawn_tool_keyless_level_refusal() -> None:
    """Level 1 without an API key is refused with a level-3/4 hint."""
    env = build_env(settings=make_settings(llmio_api_key=""))
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn("task", "t", "do it", model_level=1)

    assert result.startswith("Could not start the subsession:")
    assert "API key" in result


@pytest.mark.asyncio
async def test_spawn_tool_periodic_parent_periodic_child_refusal() -> None:
    """A periodic subsession spawning a periodic child is refused politely."""
    env = build_env()
    # Register a periodic parent and build tools from its context.
    parent = _register(env, kind=SubsessionKind.PERIODIC, interval_seconds=10.0)
    tools = build_subsession_tools(env, ctx=_ctx(subsession_id=parent.id, depth=1))
    spawn = _by_name(tools, "spawn_subsession")

    result = await spawn(
        "periodic",
        "child periodic",
        "do monitoring",
        interval_seconds=5.0,
        model_level=3,
    )

    assert "cannot spawn periodic" in result


@pytest.mark.asyncio
async def test_spawn_tool_periodic_parent_task_child_allowed() -> None:
    """A periodic subsession can spawn a task as a sibling (remediation)."""
    agent = FakeAgent(["done quickly"])
    env = build_env(agent=agent)
    parent = _register(env, kind=SubsessionKind.PERIODIC, interval_seconds=10.0)
    tools = build_subsession_tools(env, ctx=_ctx(subsession_id=parent.id, depth=1))
    spawn = _by_name(tools, "spawn_subsession")

    result = await spawn(
        "task",
        "remediation task",
        "fix the issue",
        model_level=3,
    )

    assert result.startswith("Started task subsession ")
    assert "'remediation task'" in result
    # Verify it's a sibling: same depth as the periodic, parent is
    # the periodic's parent (None in this case).
    infos = env.registry.list_for_owner(OWNER)
    tasks = [i for i in infos if i.kind is SubsessionKind.TASK]
    assert len(tasks) == 1
    assert tasks[0].depth == parent.depth
    assert tasks[0].parent_id == parent.parent_id
    await wait_until(lambda: not tasks[0].is_active)


@pytest.mark.asyncio
async def test_spawn_tool_user_chat_parent_user_chat_child_refusal() -> None:
    """A user_chat subsession spawning a user_chat child is refused politely."""
    env = build_env()
    parent = _register(env, kind=SubsessionKind.USER_CHAT)
    tools = build_subsession_tools(env, ctx=_ctx(subsession_id=parent.id, depth=1))
    spawn = _by_name(tools, "spawn_subsession")

    result = await spawn(
        "user_chat",
        "child user_chat",
        "ask more questions",
        model_level=3,
    )

    assert result.startswith("Could not start the subsession:")
    assert "user_chat" in result


@pytest.mark.asyncio
async def test_spawn_tool_refused_for_closed_session() -> None:
    """A closed chat session cannot spawn new subsessions."""
    store = ConversationStore()
    sid = str(store.create_session("owner-1")["session_id"])
    store.close_session("owner-1", sid)
    env = build_env(store=store)
    ctx = SubsessionContext(owner_session_id=sid, subsession_id=None, depth=0)
    spawn = _by_name(build_subsession_tools(env, ctx=ctx), "spawn_subsession")

    result = await spawn("task", "t", "do it")

    assert result == "This session is closed — no new subsessions can be started."
    assert env.registry.list_for_owner(sid) == []


@pytest.mark.asyncio
async def test_spawn_tool_starts_a_worker() -> None:
    """A valid spawn starts the worker and reports the new id."""
    agent = FakeAgent(["done quickly"])
    env = build_env(agent=agent)
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn("task", "quick job", "do it", model_level=3)

    assert result.startswith("Started task subsession ")
    assert "'quick job'" in result
    infos = env.registry.list_for_owner(OWNER)
    assert len(infos) == 1
    await wait_until(lambda: not infos[0].is_active)
    assert infos[0].summary == "done quickly"


@pytest.mark.asyncio
async def test_spawn_tool_forwards_auto_stop_no_change_runs_override() -> None:
    """The spawn tool persists the per-monitor no-change threshold."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn(
        "periodic",
        "watch ticket",
        "monitor ticket state",
        model_level=3,
        interval_seconds=60.0,
        dedup_key="ticket-123",
        auto_stop_no_change_runs=7,
    )

    assert result.startswith("Started periodic subsession ")
    info = env.registry.list_for_owner(OWNER)[0]
    assert info.checkpoint == {
        "ticket_id": "ticket-123",
        "auto_stop_no_change_runs": 7,
    }

    task = env.registry._running.get(info.id)
    if task is not None and not task.done():
        task.cancel()
    await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# message / close scope guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_tool_scope_guard_rejects_foreign_subsession() -> None:
    """Another owner's subsession cannot be messaged from this session."""
    env = build_env()
    foreign = _register(env, owner="sess-other")
    tools = build_subsession_tools(env, ctx=_ctx())
    message = _by_name(tools, "message_subsession")

    result = await message(foreign.id, "psst")

    assert result == (f"No subsession {foreign.id!r} in this conversation's tree.")
    assert env.registry.drain_inbox(foreign.id) == []


@pytest.mark.asyncio
async def test_close_tool_scope_guard_rejects_foreign_subsession() -> None:
    """Another owner's subsession cannot be closed from this session."""
    env = build_env()
    foreign = _register(env, owner="sess-other")
    close = _by_name(build_subsession_tools(env, ctx=_ctx()), "close_subsession")

    result = await close(foreign.id)

    assert result == (f"No subsession {foreign.id!r} in this conversation's tree.")
    assert foreign.is_active


@pytest.mark.asyncio
async def test_subsession_agent_scope_is_descendants_only() -> None:
    """A subsession agent cannot steer its siblings — only descendants."""
    env = build_env()
    me = _register(env, title="me")
    sibling = _register(env, title="sibling")
    child = _register(env, parent_id=me.id, depth=2, title="child")
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=me.id, depth=1), close_state=CloseState()
    )
    message = _by_name(tools, "message_subsession")

    refused = await message(sibling.id, "hello sibling")
    accepted = await message(child.id, "hello child")

    assert "No subsession" in refused
    assert "Message queued" in accepted


@pytest.mark.asyncio
async def test_message_tool_queues_for_own_subsession() -> None:
    """Messaging an owned subsession enqueues with role ``parent``."""
    env = build_env()
    info = _register(env)
    message = _by_name(build_subsession_tools(env, ctx=_ctx()), "message_subsession")

    result = await message(info.id, "please also check X")

    assert f"Message queued for subsession {info.id}" in result
    queued = env.registry.drain_inbox(info.id)
    assert [(m.role, m.text) for m in queued] == [("parent", "please also check X")]


@pytest.mark.asyncio
async def test_message_tool_reports_inactive_subsession() -> None:
    """Messaging a terminal (but in-tree) subsession reports it inactive."""
    env = build_env()
    info = _register(env)
    env.registry.mark_closed(info.id, summary="done", reason="completed")
    message = _by_name(build_subsession_tools(env, ctx=_ctx()), "message_subsession")

    result = await message(info.id, "too late")

    assert result == f"Subsession {info.id} is no longer active."


@pytest.mark.asyncio
async def test_message_tool_accepts_prefix_id() -> None:
    """Messaging by the 8-char prefix (as ``list_subsessions`` displays) works."""
    env = build_env()
    info = _register(env, title="my sub")
    message = _by_name(build_subsession_tools(env, ctx=_ctx()), "message_subsession")

    result = await message(info.id[:8], "steering via prefix")

    assert f"Message queued for subsession {info.id[:8]}" in result
    queued = env.registry.drain_inbox(info.id)
    assert [(m.role, m.text) for m in queued] == [("parent", "steering via prefix")]


# ---------------------------------------------------------------------------
# close tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_tool_cancels_worker_and_delivers_summary() -> None:
    """``close_subsession`` cancels the worker and reports the summary."""
    gate = asyncio.Event()  # never set — the worker stays mid-turn
    agent = FakeAgent(["never"], gate=gate)
    env = build_env(agent=agent)
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")
    close = _by_name(build_subsession_tools(env, ctx=_ctx()), "close_subsession")

    await spawn("task", "long job", "work forever", model_level=3)
    await wait_until(lambda: len(agent.calls) == 1)
    info = env.registry.list_for_owner(OWNER)[0]
    worker = env.registry._running[info.id]

    result = await close(info.id, "no longer needed")

    assert result.startswith(f"Closed subsession {info.id}.")
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no longer needed"
    # The summary was delivered to the owning session's history via a
    # background task (fire-and-forget) — yield so it can complete.
    await asyncio.sleep(0)
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "no longer needed" in history[0][0]
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)


@pytest.mark.asyncio
async def test_close_tool_already_closed_reports_it() -> None:
    """Closing a terminal subsession is a polite no-op."""
    env = build_env()
    info = _register(env)
    env.registry.mark_closed(info.id, summary="done", reason="completed")
    close = _by_name(build_subsession_tools(env, ctx=_ctx()), "close_subsession")

    result = await close(info.id)

    assert result == f"Subsession {info.id} is already closed."


@pytest.mark.asyncio
async def test_close_tool_accepts_prefix_id() -> None:
    """Closing by the 8-char prefix (as ``list_subsessions`` displays) works."""
    env = build_env()
    info = _register(env)
    close = _by_name(build_subsession_tools(env, ctx=_ctx()), "close_subsession")

    result = await close(info.id[:8], "done via prefix")

    assert result.startswith(f"Closed subsession {info.id[:8]}.")
    assert info.status is SubsessionStatus.CLOSED


@pytest.mark.asyncio
async def test_message_tool_rejects_ambiguous_prefix() -> None:
    """A prefix matching multiple owned subsessions is rejected."""
    env = build_env()
    # Two subsessions whose first 8 chars are the same.
    shared_prefix = "b1a2c3d4"
    _register(env, sub_id=shared_prefix + "e5f6g7h8", title="first")
    _register(env, sub_id=shared_prefix + "i9j0k1l2", title="second")
    message = _by_name(build_subsession_tools(env, ctx=_ctx()), "message_subsession")

    result = await message(shared_prefix, "ambiguous")

    assert "No subsession" in result or "in this conversation" in result


@pytest.mark.asyncio
async def test_message_tool_rejects_unknown_prefix() -> None:
    """A prefix that matches nothing is rejected."""
    env = build_env()
    _register(env)
    message = _by_name(build_subsession_tools(env, ctx=_ctx()), "message_subsession")

    result = await message("deadbeef", "nope")

    assert "No subsession" in result


# ---------------------------------------------------------------------------
# list tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_subsessions_formats_entries() -> None:
    """The listing shows id prefix, kind, status, level, and title."""
    env = build_env()
    task = _register(env, title="fetch data", model_level=2)
    periodic = _register(
        env,
        kind=SubsessionKind.PERIODIC,
        title="watch CI",
        interval_seconds=60.0,
    )
    list_tool = _by_name(build_subsession_tools(env, ctx=_ctx()), "list_subsessions")

    listing = await list_tool()

    lines = listing.splitlines()
    assert len(lines) == 2
    assert task.id[:8] in lines[0]
    assert "[task]" in lines[0]
    assert "L2" in lines[0]
    assert "'fetch data'" in lines[0]
    assert periodic.id[:8] in lines[1]
    assert "[periodic]" in lines[1]
    assert "L3" in lines[1]
    assert "every 60s" in lines[1]


@pytest.mark.asyncio
async def test_list_subsessions_empty_message() -> None:
    """An empty tree yields a human-readable message."""
    env = build_env()
    list_tool = _by_name(build_subsession_tools(env, ctx=_ctx()), "list_subsessions")

    assert await list_tool() == "No subsessions in this conversation."


# ---------------------------------------------------------------------------
# complete tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complete_subsession_sets_close_state() -> None:
    """``complete_subsession`` flips the shared close state with the summary."""
    env = build_env()
    _register(env, sub_id="sub-1", kind=SubsessionKind.TASK)
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-1", depth=1), close_state=close_state
    )
    complete = _by_name(tools, "complete_subsession")

    result = await complete("all objectives met")

    assert close_state.requested is True
    assert close_state.summary == "all objectives met"
    assert "Close requested" in result


@pytest.mark.asyncio
async def test_complete_subsession_rejects_periodic_without_ci_evidence() -> None:
    """A periodic monitor with a ticket_id checkpoint must include CI evidence."""
    env = build_env()
    _register(
        env,
        sub_id="sub-ci-1",
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "abc123"},
        runs=1,
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-ci-1", depth=1), close_state=close_state
    )
    complete = _by_name(tools, "complete_subsession")

    # Summary with no CI evidence → rejected.
    result = await complete("Ticket abc123 is now closed. All checks passed.")
    assert "REJECTED" in result
    assert "CI workflow verification required" in result
    assert close_state.requested is False


@pytest.mark.asyncio
async def test_complete_subsession_accepts_periodic_with_ci_evidence() -> None:
    """A periodic monitor with ticket_id passes when CI evidence is present."""
    env = build_env()
    _register(
        env,
        sub_id="sub-ci-2",
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "abc123"},
        runs=1,
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-ci-2", depth=1), close_state=close_state
    )
    complete = _by_name(tools, "complete_subsession")

    # Summary with CI evidence → accepted.
    result = await complete(
        "Ticket abc123 closed. CI workflow run #42 passed — pipeline is green."
    )
    assert "REJECTED" not in result
    assert "Close requested" in result
    assert close_state.requested is True


@pytest.mark.asyncio
async def test_complete_subsession_accepts_periodic_with_unreachable_ci() -> None:
    """A periodic monitor passes when CI was unreachable (acknowledged)."""
    env = build_env()
    _register(
        env,
        sub_id="sub-ci-3",
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "abc123"},
        runs=1,
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-ci-3", depth=1), close_state=close_state
    )
    complete = _by_name(tools, "complete_subsession")

    # Summary acknowledging CI was unreachable → accepted.
    result = await complete(
        "Ticket abc123 closed. CI workflow status could not be verified "
        "(API unreachable after 2 attempts)."
    )
    assert "REJECTED" not in result
    assert "Close requested" in result
    assert close_state.requested is True


@pytest.mark.asyncio
async def test_complete_subsession_no_guard_without_ticket_id() -> None:
    """Periodic without a ticket_id checkpoint skips the CI guard entirely."""
    env = build_env()
    _register(
        env,
        sub_id="sub-ci-4",
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"last_known_state": "blocked"},
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-ci-4", depth=1), close_state=close_state
    )
    complete = _by_name(tools, "complete_subsession")

    result = await complete("Monitoring complete — ticket resolved.")
    assert "REJECTED" not in result
    assert "Close requested" in result


@pytest.mark.asyncio
async def test_complete_subsession_no_guard_for_task_subsession() -> None:
    """Task subsessions with a ticket_id checkpoint are not affected."""
    env = build_env()
    _register(
        env,
        sub_id="sub-ci-5",
        kind=SubsessionKind.TASK,
        checkpoint={"ticket_id": "abc123"},
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-ci-5", depth=1), close_state=close_state
    )
    complete = _by_name(tools, "complete_subsession")

    result = await complete("Ticket closed.")
    assert "REJECTED" not in result
    assert "Close requested" in result


@pytest.mark.asyncio
async def test_complete_subsession_rejects_below_min_runs() -> None:
    """A periodic ticket monitor with zero completed runs is rejected."""
    env = build_env()
    _register(
        env,
        sub_id="sub-min-runs-1",
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        checkpoint={"ticket_id": "abc123"},
        runs=0,
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env,
        ctx=_ctx(subsession_id="sub-min-runs-1", depth=1),
        close_state=close_state,
    )
    complete = _by_name(tools, "complete_subsession")

    result = await complete(
        "Ticket abc123 closed. CI workflow run #1 passed — pipeline is green."
    )
    assert "REJECTED" in result
    assert "minimum runs not met" in result
    assert close_state.requested is False


# ---------------------------------------------------------------------------
# set_checkpoint tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_checkpoint_stores_data():
    """A valid dict is persisted in the registry checkpoint."""
    env = build_env()
    sub_id = _register(env, kind=SubsessionKind.TASK, sub_id="cp-1").id
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=sub_id, depth=1), close_state=close_state
    )
    set_cp = _by_name(tools, "set_checkpoint")

    result = await set_cp({"ticket_id": "TICK-42", "last_known_state": "open"})

    assert "Checkpoint updated" in result
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint == {"ticket_id": "TICK-42", "last_known_state": "open"}


@pytest.mark.asyncio
async def test_set_checkpoint_invalid_data_rejected():
    """Passing a non-dict value returns an error message."""
    env = build_env()
    sub_id = _register(env, kind=SubsessionKind.TASK, sub_id="cp-2").id
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=sub_id, depth=1), close_state=close_state
    )
    set_cp = _by_name(tools, "set_checkpoint")

    result = await set_cp("not a dict")  # type: ignore[arg-type]

    assert "must be a dict" in result


@pytest.mark.asyncio
async def test_set_checkpoint_inactive_subsession_returns_error():
    """Calling set_checkpoint on a non-existent subsession returns an error."""
    env = build_env()
    close_state = CloseState()
    tools = build_subsession_tools(
        env,
        ctx=_ctx(subsession_id="nonexistent-id", depth=1),
        close_state=close_state,
    )
    set_cp = _by_name(tools, "set_checkpoint")

    result = await set_cp({"x": 1})
    assert "no longer active" in result


@pytest.mark.asyncio
async def test_set_checkpoint_replaces_entire_checkpoint():
    """Each call REPLACES the entire checkpoint — old keys are dropped."""
    env = build_env()
    sub_id = _register(env, kind=SubsessionKind.TASK, sub_id="cp-4").id
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=sub_id, depth=1), close_state=close_state
    )
    set_cp = _by_name(tools, "set_checkpoint")

    await set_cp({"ticket_id": "TICK-1", "last_known_state": "open", "retries": 0})
    await set_cp({"ticket_id": "TICK-2", "last_known_state": "closed"})

    info = env.registry.get(sub_id)
    assert info is not None
    # Only the second call's data is present — "retries" is gone.
    assert info.checkpoint == {
        "ticket_id": "TICK-2",
        "last_known_state": "closed",
    }


@pytest.mark.asyncio
async def test_set_checkpoint_preserves_ticket_id_for_wait_for_event():
    """A wait_for_event monitor's ticket_id survives a set_checkpoint replacement."""
    env = build_env()
    sub_id = _register(
        env,
        kind=SubsessionKind.WAIT_FOR_EVENT,
        sub_id="cp-wfe",
        checkpoint={"ticket_id": "TICK-WFE"},
    ).id
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=sub_id, depth=1), close_state=close_state
    )
    set_cp = _by_name(tools, "set_checkpoint")

    await set_cp({"last_known_state": "in_progress"})

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint == {
        "ticket_id": "TICK-WFE",
        "last_known_state": "in_progress",
    }


@pytest.mark.asyncio
async def test_set_checkpoint_empty_dict_clears():
    """Passing an empty dict clears the checkpoint."""
    env = build_env()
    sub_id = _register(env, kind=SubsessionKind.TASK, sub_id="cp-5").id
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=sub_id, depth=1), close_state=close_state
    )
    set_cp = _by_name(tools, "set_checkpoint")

    await set_cp({"ticket_id": "TICK-1"})
    result = await set_cp({})
    assert "Checkpoint updated" in result

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint is None  # empty dict → None in registry


@pytest.mark.asyncio
async def test_set_checkpoint_non_string_key_rejected():
    """Non-string keys produce an error message."""
    env = build_env()
    sub_id = _register(env, kind=SubsessionKind.TASK, sub_id="cp-6").id
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=sub_id, depth=1), close_state=close_state
    )
    set_cp = _by_name(tools, "set_checkpoint")

    result = await set_cp({1: "value"})  # non-string key

    assert "is not a string" in result


# ---------------------------------------------------------------------------
# dedup key tool output differentiation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_tool_dedup_key_fresh_spawn_returns_started_message() -> None:
    """A fresh spawn with a dedup_key returns the normal 'Started' message."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn(
        "user_chat",
        "crash investigation",
        "investigate the asyncio.run crash",
        dedup_key="asyncio.run-crash",
        model_level=3,
    )

    assert result.startswith("Started user_chat subsession ")
    assert "'crash investigation'" in result


@pytest.mark.asyncio
async def test_spawn_tool_dedup_key_duplicate_returns_deduplicated_message() -> None:
    """When a user_chat with same dedup_key already exists, returns dedup message."""
    agent = FakeAgent(["ok"])
    env = build_env(agent=agent)
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    # First spawn — should be fresh.
    first_result = await spawn(
        "user_chat",
        "crash investigation",
        "investigate the asyncio.run crash",
        dedup_key="asyncio.run-crash",
        model_level=3,
    )
    assert first_result.startswith("Started user_chat subsession ")

    # Second spawn with the same key — should be deduplicated.
    second_result = await spawn(
        "user_chat",
        "crash investigation (retry)",
        "investigate again",
        dedup_key="asyncio.run-crash",
        model_level=3,
    )
    assert second_result.startswith("Deduplicated:")
    assert "asyncio.run-crash" in second_result


@pytest.mark.asyncio
async def test_spawn_tool_dedup_key_without_key_returns_normal_started() -> None:
    """A spawn without a dedup_key always returns the normal 'Started' message."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn(
        "user_chat",
        "some question",
        "ask user about deploy",
        model_level=3,
    )

    assert result.startswith("Started user_chat subsession ")


@pytest.mark.asyncio
async def test_spawn_tool_dedup_key_non_user_chat_deduplicated() -> None:
    """Task spawn with dedup_key returns deduplicated message when key is active."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    first = await spawn(
        "task",
        "task 1",
        "do work",
        dedup_key="some-key",
        model_level=3,
    )
    assert first.startswith("Started task subsession ")

    second = await spawn(
        "task",
        "task 2",
        "do more work",
        dedup_key="some-key",
        model_level=3,
    )
    assert second.startswith("Deduplicated: ")


# ---------------------------------------------------------------------------
# wait_for_event + dedup_key: synchronous rejection when ticket_id missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_wait_for_event_without_dedup_key_rejected() -> None:
    """wait_for_event without dedup_key is rejected synchronously."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn(
        "wait_for_event",
        "ticket monitor",
        "monitor ticket foo",
        model_level=3,
    )

    assert "require a dedup_key" in result
    assert "wait_for_event" in result.lower()


@pytest.mark.asyncio
async def test_spawn_wait_for_event_with_dedup_key_populates_checkpoint() -> None:
    """wait_for_event with dedup_key pre-populates checkpoint with ticket_id."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn(
        "wait_for_event",
        "ticket monitor",
        "monitor ticket abc",
        dedup_key="abc-123",
        model_level=3,
    )

    assert result.startswith("Started wait_for_event subsession ")
    # Result format: "Started wait_for_event subsession <id> ('<title>')."
    sub_id = result.split(" (")[0].rsplit(" ", 1)[-1]
    info = env.registry.get(sub_id)
    assert info is not None
    assert info.checkpoint == {"ticket_id": "abc-123"}
    assert info.dedup_key == "abc-123"


@pytest.mark.asyncio
async def test_spawn_wait_for_event_empty_dedup_key_rejected() -> None:
    """wait_for_event with an empty dedup_key string is rejected."""
    env = build_env()
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    result = await spawn(
        "wait_for_event",
        "ticket monitor",
        "monitor ticket bar",
        dedup_key="",
        model_level=3,
    )

    assert "require a dedup_key" in result


# ---------------------------------------------------------------------------
# Spawn-time ticket-verification (404-refusal / board-unreachable paths)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_tool_refuses_monitor_on_ticket_404() -> None:
    """A periodic/wait_for_event spawn with dedup_key is refused on 404.

    When the board returns 404 for the ticket id passed as
    ``dedup_key``, the spawn is blocked and a diagnostic message is
    returned.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    env = build_env()
    # Enable direct_repo with a board URL so the verification path fires.
    env.settings.direct_repo = SimpleNamespace(
        enabled=True, board_api_base_url="https://mill.example.com"
    )
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    # Mock the HTTP client to return a 404.
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        result = await spawn(
            "periodic",
            "phantom monitor",
            "monitor ticket that does not exist",
            interval_seconds=60.0,
            dedup_key="nonexistent-ticket",
            model_level=3,
        )

    assert "Cannot spawn monitor" in result
    assert "404" in result
    # No subsession should have been created.
    assert env.registry.list_for_owner(OWNER) == []


@pytest.mark.asyncio
async def test_spawn_tool_allows_monitor_when_board_unreachable() -> None:
    """A spawn with dedup_key is allowed when the board is unreachable.

    Connection errors (the board is down or unreachable) should not
    block monitor spawns — the watcher handles repeated 404s later.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import httpx

    env = build_env()
    env.settings.direct_repo = SimpleNamespace(
        enabled=True, board_api_base_url="https://mill.example.com"
    )
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    # Mock the HTTP client to raise a connection error.
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        result = await spawn(
            "periodic",
            "monitor despite unreachable board",
            "monitor ticket",
            interval_seconds=60.0,
            dedup_key="ticket-maybe",
            model_level=3,
        )

    # Spawn should succeed — the board-unreachable path logs a warning
    # but allows the spawn.
    assert result.startswith("Started periodic subsession ")
    assert "'monitor despite unreachable board'" in result


@pytest.mark.asyncio
async def test_spawn_tool_allows_monitor_on_non_404_http_error() -> None:
    """A spawn with dedup_key is allowed on a non-404 server error.

    Only a 404 should block the spawn; temporary board issues (5xx,
    etc.) should let the spawn proceed — the watcher catches the
    fallout later.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    env = build_env()
    env.settings.direct_repo = SimpleNamespace(
        enabled=True, board_api_base_url="https://mill.example.com"
    )
    spawn = _by_name(build_subsession_tools(env, ctx=_ctx()), "spawn_subsession")

    # Mock the HTTP client to return a 500.
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)

    mock_instance = MagicMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_client)
    mock_instance.__aexit__ = AsyncMock(return_value=None)

    with patch("httpx.AsyncClient", MagicMock(return_value=mock_instance)):
        result = await spawn(
            "wait_for_event",
            "event monitor",
            "monitor ticket",
            dedup_key="ticket-maybe",
            model_level=3,
        )

    # Should succeed — only 404 blocks the spawn.
    assert result.startswith("Started wait_for_event subsession ")


# ---------------------------------------------------------------------------
# Periodic sibling spawning & forbidden-kind pre-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_tool_periodic_on_close_refusal() -> None:
    """A periodic subsession spawning on_close is refused silently (logged)."""
    env = build_env()
    parent = _register(env, kind=SubsessionKind.PERIODIC, interval_seconds=10.0)
    tools = build_subsession_tools(env, ctx=_ctx(subsession_id=parent.id, depth=1))
    spawn = _by_name(tools, "spawn_subsession")

    result = await spawn(
        "on_close",
        "cleanup",
        "clean up later",
        model_level=3,
    )

    assert "cannot spawn on_close" in result


@pytest.mark.asyncio
async def test_spawn_tool_periodic_user_chat_sibling() -> None:
    """A periodic subsession spawning user_chat creates a SIBLING."""
    agent = FakeAgent(["decision made"])
    env = build_env(agent=agent)
    # Create the periodic with a known parent (a task subsession).
    task_parent = _register(env, kind=SubsessionKind.TASK)
    periodic = _register(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=10.0,
        parent_id=task_parent.id,
        depth=2,
    )
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=periodic.id, depth=periodic.depth)
    )
    spawn = _by_name(tools, "spawn_subsession")

    result = await spawn(
        "user_chat",
        "escalation",
        "operator decision needed",
        model_level=3,
    )

    assert result.startswith("Started user_chat subsession ")
    # Verify it's a sibling: same depth as periodic, same parent.
    infos = env.registry.list_for_owner(OWNER)
    chats = [i for i in infos if i.kind is SubsessionKind.USER_CHAT]
    assert len(chats) == 1
    assert chats[0].depth == periodic.depth
    assert chats[0].parent_id == periodic.parent_id


@pytest.mark.asyncio
async def test_spawn_tool_periodic_task_sibling_depth() -> None:
    """A periodic at depth 2 spawns a task sibling also at depth 2."""
    agent = FakeAgent(["remediated"])
    env = build_env(agent=agent)
    # Create a task at depth 1, then a periodic child at depth 2.
    depth1 = _register(env, kind=SubsessionKind.TASK, depth=1)
    periodic = _register(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=10.0,
        parent_id=depth1.id,
        depth=2,
    )
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=periodic.id, depth=periodic.depth)
    )
    spawn = _by_name(tools, "spawn_subsession")

    result = await spawn(
        "task",
        "remediation",
        "fix the issue",
        model_level=3,
    )

    assert result.startswith("Started task subsession ")
    infos = env.registry.list_for_owner(OWNER)
    tasks = [
        i for i in infos if i.kind is SubsessionKind.TASK and i.title == "remediation"
    ]
    assert len(tasks) == 1
    # Sibling: same depth as periodic, parent is depth1 (not the periodic).
    assert tasks[0].depth == periodic.depth
    assert tasks[0].parent_id == periodic.parent_id
    assert tasks[0].parent_id == depth1.id


# ---------------------------------------------------------------------------
# self_update_subsession tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_update_tool_not_available_to_main_agent() -> None:
    """The main agent (no close_state) does not get self_update_subsession."""
    env = build_env()
    tools = build_subsession_tools(env, ctx=_ctx())
    assert "self_update_subsession" not in _tool_names(tools)


@pytest.mark.asyncio
async def test_self_update_rejects_non_periodic_subsession() -> None:
    """self_update_subsession refuses task/user_chat subsessions."""
    env = build_env()
    info = _register(env, kind=SubsessionKind.TASK, sub_id="task-1")
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(instructions="new instructions")

    assert "only periodic subsessions can self-update" in result


@pytest.mark.asyncio
async def test_self_update_rejects_inactive_subsession() -> None:
    """self_update_subsession refuses a closed/terminal subsession."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-1"
    )
    env.registry.mark_closed(info.id, summary="done", reason="completed")
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(instructions="new instructions")

    assert "not active" in result


@pytest.mark.asyncio
async def test_self_update_no_fields_returns_error() -> None:
    """Calling self_update_subsession with no arguments returns an error."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-2"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update()

    assert "no fields to update" in result


@pytest.mark.asyncio
async def test_self_update_instructions_too_long() -> None:
    """Instructions exceeding 8000 chars are rejected."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-3"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(instructions="x" * 8001)

    assert "too long" in result
    assert "8001" in result


@pytest.mark.asyncio
async def test_self_update_instructions_not_a_string() -> None:
    """Non-string instructions are rejected."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-4"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(instructions=123)

    assert "must be a string" in result


@pytest.mark.asyncio
async def test_self_update_interval_below_minimum() -> None:
    """An interval below the configured minimum is rejected."""
    env = build_env(settings=make_settings(min_interval_seconds=10))
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-5"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(interval_seconds=5.0)

    assert "must be >= 10" in result


@pytest.mark.asyncio
async def test_self_update_interval_not_a_number() -> None:
    """Non-numeric interval is rejected."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-6"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(interval_seconds="fast")

    assert "must be a number" in result


@pytest.mark.asyncio
async def test_self_update_max_runs_negative() -> None:
    """Negative max_runs is rejected."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-7"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(max_runs=-1)

    assert "must be >= 0" in result


@pytest.mark.asyncio
async def test_self_update_max_runs_not_int() -> None:
    """Non-integer max_runs is rejected."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-8"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(max_runs=3.5)

    assert "must be an integer" in result


@pytest.mark.asyncio
async def test_self_update_changes_instructions() -> None:
    """self_update_subsession updates the prompt/instructions field."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-9"
    )
    old_prompt = info.prompt
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(instructions="watch tickets T-42 and T-99")

    assert "Self-update applied" in result
    assert "instructions" in result
    assert info.prompt == "watch tickets T-42 and T-99"
    assert info.prompt != old_prompt


@pytest.mark.asyncio
async def test_self_update_changes_interval() -> None:
    """self_update_subsession updates the interval_seconds field."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-10"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(interval_seconds=120.0)

    assert "Self-update applied" in result
    assert "interval" in result
    assert info.interval_seconds == 120.0


@pytest.mark.asyncio
async def test_self_update_changes_max_runs() -> None:
    """self_update_subsession updates the max_runs field."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-11"
    )
    assert info.max_runs is None  # default
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(max_runs=10)

    assert "Self-update applied" in result
    assert "max_runs" in result
    assert info.max_runs == 10


@pytest.mark.asyncio
async def test_self_update_does_not_reset_run_counter() -> None:
    """self_update_subsession never resets the runs counter."""
    env = build_env()
    info = _register(
        env,
        kind=SubsessionKind.PERIODIC,
        interval_seconds=60.0,
        sub_id="per-12",
        runs=5,
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    await update(instructions="new instructions", interval_seconds=30.0)

    assert info.runs == 5  # unchanged


@pytest.mark.asyncio
async def test_self_update_multiple_fields_at_once() -> None:
    """self_update_subsession can change multiple fields in one call."""
    env = build_env()
    info = _register(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=60.0, sub_id="per-13"
    )
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id=info.id, depth=1), close_state=close_state
    )
    update = _by_name(tools, "self_update_subsession")

    result = await update(
        instructions="watch T-42 and T-99",
        interval_seconds=300.0,
        max_runs=20,
    )

    assert "Self-update applied" in result
    assert "instructions" in result
    assert "interval" in result
    assert "max_runs" in result
    assert info.prompt == "watch T-42 and T-99"
    assert info.interval_seconds == 300.0
    assert info.max_runs == 20
