"""Tests for the agent-facing subsession tool factory."""

from __future__ import annotations

import asyncio
import contextlib
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
    ]


def test_agent_at_max_depth_gets_only_complete_tool() -> None:
    """At ``max_depth`` the agent cannot spawn — only ``complete_subsession``."""
    env = build_env(settings=make_settings(max_depth=2))

    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-1", depth=2), close_state=CloseState()
    )

    assert _tool_names(tools) == ["complete_subsession"]


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

    result = await spawn("task", "quick job", "do it")

    assert result.startswith("Started task subsession ")
    assert "'quick job'" in result
    infos = env.registry.list_for_owner(OWNER)
    assert len(infos) == 1
    await wait_until(lambda: not infos[0].is_active)
    assert infos[0].summary == "done quickly"


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

    await spawn("task", "long job", "work forever")
    await wait_until(lambda: len(agent.calls) == 1)
    info = env.registry.list_for_owner(OWNER)[0]
    worker = env.registry._running[info.id]

    result = await close(info.id, "no longer needed")

    assert result.startswith(f"Closed subsession {info.id}.")
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no longer needed"
    # The summary was delivered to the owning session's history.
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
    close_state = CloseState()
    tools = build_subsession_tools(
        env, ctx=_ctx(subsession_id="sub-1", depth=1), close_state=close_state
    )
    complete = _by_name(tools, "complete_subsession")

    result = await complete("all objectives met")

    assert close_state.requested is True
    assert close_state.summary == "all objectives met"
    assert "Close requested" in result
