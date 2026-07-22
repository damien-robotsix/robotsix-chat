"""Tests for ``ParentDelivery`` — routing of subsession outcomes to parents."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robotsix_chat.subsessions.delivery import ParentDelivery
from robotsix_chat.subsessions.models import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
)

# ---------------------------------------------------------------------------
# module-level constants
# ---------------------------------------------------------------------------

_DELIVERY_LOGGER = "robotsix_chat.subsessions.delivery"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_info(
    *,
    parent_id: str | None = None,
    owner_session_id: str = "owner-sess-1",
    sub_id: str = "sub-abc12345",
    kind: SubsessionKind = SubsessionKind.TASK,
    title: str = "test-job",
) -> SubsessionInfo:
    """Build a minimal ``SubsessionInfo`` for delivery tests."""
    return SubsessionInfo(
        id=sub_id,
        kind=kind,
        owner_session_id=owner_session_id,
        parent_id=parent_id,
        depth=1,
        title=title,
        prompt="do the thing",
        model_level=3,
        status=SubsessionStatus.RUNNING,
        created_at=1000.0,
        last_activity_at=1001.0,
    )


def _build_delivery(
    *,
    store: MagicMock | None = None,
    registry: MagicMock | None = None,
    lock: MagicMock | None = None,
    event_sink: MagicMock | None = None,
    agent: MagicMock | None = None,
) -> ParentDelivery:
    """Build a ``ParentDelivery`` with mocked collaborators.

    Passing *agent* calls :meth:`ParentDelivery.set_agent` after
    construction, mirroring how ``cli.py`` wires it post-construction.
    """
    store = store or MagicMock()
    registry = registry or MagicMock()
    run_serializer = MagicMock()
    run_serializer.for_owner.return_value = lock or _async_context_manager()
    delivery = ParentDelivery(
        conversation_store=store,
        registry=registry,
        run_serializer=run_serializer,
        event_sink=event_sink,
    )
    if agent is not None:
        delivery.set_agent(agent)
    return delivery


async def _await_reaction_tasks(delivery: ParentDelivery) -> None:
    """Wait for all in-flight background reaction tasks to complete."""
    while delivery._reaction_tasks:
        tasks = list(delivery._reaction_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)


def _fake_agent(chunks: list[str]) -> MagicMock:
    """Build a ChatAgent stub whose ``stream()`` yields the given chunks."""
    agent = MagicMock()

    async def _stream(
        message: str,
        *,
        history=None,
        session_id=None,
        client_id=None,
        images=None,
        trace_metadata=None,
    ):
        for chunk in chunks:
            yield chunk

    agent.stream = _stream
    return agent


def _raising_agent(exc: Exception) -> MagicMock:
    """Build a ChatAgent stub whose ``stream()`` raises *exc* once consumed."""
    agent = MagicMock()

    async def _stream(
        message: str,
        *,
        history=None,
        session_id=None,
        client_id=None,
        images=None,
        trace_metadata=None,
    ):
        raise exc
        yield  # pragma: no cover — makes this an async generator

    agent.stream = _stream
    return agent


def _async_context_manager() -> MagicMock:
    """Return a mock that supports ``async with``."""
    mock = MagicMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    return mock


# ---------------------------------------------------------------------------
# deliver_summary — main-chat parent (parent_id is None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_main_chat_parent_records_to_store() -> None:
    """When parent_id is None, deliver_summary records to the owning session."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"  # owner_session_id
    assert info.id[:8] in args[1]  # label (id truncated to 8 chars)
    assert "all done" in args[2]  # summary

    # Registry enqueue_message must not be called for main-chat parent.
    registry.enqueue_message.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_result_main_chat_parent_records_to_store() -> None:
    """When parent_id is None, deliver_result records to the owning session."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)

    await delivery.deliver_result(info, 3, "interim result text")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert info.id[:8] in args[1]  # label (id truncated to 8 chars)
    assert "run 3" in args[1]
    assert "interim result text" in args[2]


# ---------------------------------------------------------------------------
# deliver_summary — main-chat parent, agent wired (real reaction turn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_with_agent_runs_reaction_turn() -> None:
    """With an agent wired, the outcome triggers a real turn.

    Not a passive record of the raw summary.
    """
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["Got it, ", "moving on."])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    # The recorded "user" turn is the reaction prompt (mentions the outcome),
    # not the bare label — and the "assistant" reply is the agent's own
    # output, not the raw summary text.
    assert "all done" in args[1]
    assert args[2] == "Got it, moving on."


@pytest.mark.asyncio
async def test_deliver_summary_with_agent_publishes_agent_message_frame() -> None:
    """A wired event_sink gets an agent_message frame with the reply."""
    from robotsix_chat.chat.events import SSE_AGENT_MESSAGE_TYPE

    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["reaction reply"])
    event_sink = MagicMock()
    delivery = _build_delivery(
        store=store, registry=registry, agent=agent, event_sink=event_sink
    )
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    event_sink.publish.assert_called_once()
    session_id, frame = event_sink.publish.call_args[0]
    assert session_id == "owner-sess-1"
    assert frame["type"] == SSE_AGENT_MESSAGE_TYPE
    assert frame["text"] == "reaction reply"


@pytest.mark.asyncio
async def test_deliver_summary_with_agent_no_event_sink_skips_publish() -> None:
    """Without an event_sink, the reply is still recorded but never published.

    No sink to publish to — this must not raise.
    """
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["reply text"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()


@pytest.mark.asyncio
async def test_deliver_summary_empty_reply_skips_publish() -> None:
    """An empty reply from the reaction turn is still recorded.

    But no agent_message frame is published for empty text.
    """
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent([])  # no chunks → empty reply
    event_sink = MagicMock()
    delivery = _build_delivery(
        store=store, registry=registry, agent=agent, event_sink=event_sink
    )
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[2] == ""
    event_sink.publish.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_summary_reaction_turn_failure_degrades_to_passive_record() -> (
    None
):
    """When the reaction turn itself raises, fall back to the old record.

    The old passive record of the raw outcome — it must never be silently
    lost.
    """
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _raising_agent(RuntimeError("backend exploded"))
    event_sink = MagicMock()
    delivery = _build_delivery(
        store=store, registry=registry, agent=agent, event_sink=event_sink
    )
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert info.id[:8] in args[1]  # degraded label form
    assert args[2] == "all done"  # raw outcome, not a generated reply
    event_sink.publish.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_result_with_agent_runs_reaction_turn() -> None:
    """deliver_result also runs a real reaction turn when an agent is wired."""
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["noted"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    info = _make_info(parent_id=None)

    await delivery.deliver_result(info, 2, "interim text")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert "interim text" in args[1]  # prompt mentions the outcome
    assert args[2] == "noted"


# ---------------------------------------------------------------------------
# loop guard — depth-bounded trigger chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_loop_guard_degrades_when_reaction_in_progress() -> None:
    """When a reaction is in flight, the new trigger queues behind it.

    The outcome is recorded under the lock but the agent does NOT get a new
    reaction turn until the prior one completes.  This prevents unbounded
    trigger chains via depth-bounding.
    """
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["reply"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    info = _make_info(parent_id=None)

    # Simulate a reaction already in progress for this session (depth=2,
    # one below the cap — new closures still schedule, they just queue).
    delivery._reaction_depth["owner-sess-1"] = 2

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    # The agent runs because depth (2) < _MAX_REACTION_DEPTH (3).
    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert "all done" in args[1]  # prompt form (not degraded)
    assert args[2] == "reply"


@pytest.mark.asyncio
async def test_deliver_summary_loop_guard_degraded_waits_for_lock() -> None:
    """Passive record written under the lock when degraded due to in-flight reaction."""
    store = MagicMock()
    registry = MagicMock()
    lock = MagicMock()
    lock.__aenter__ = AsyncMock()
    lock.__aexit__ = AsyncMock()
    delivery = _build_delivery(store=store, registry=registry, lock=lock)
    info = _make_info(parent_id=None)

    # Push depth to max so the next schedule degrades.
    delivery._reaction_depth["owner-sess-1"] = 3

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    lock.__aenter__.assert_awaited_once()
    lock.__aexit__.assert_awaited_once()
    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[2] == "all done"


@pytest.mark.asyncio
async def test_deliver_summary_loop_guard_allows_reaction_when_flag_cleared() -> None:
    """With an agent wired and depth below the cap, the agent runs normally."""
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["real reaction"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    info = _make_info(parent_id=None)

    # No depth set — reaction should proceed.
    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert "all done" in args[1]  # prompt mentions the outcome
    assert args[2] == "real reaction"  # agent-generated reply


@pytest.mark.asyncio
async def test_deliver_summary_loop_guard_clears_depth_after_reaction() -> None:
    """After a reaction completes, the depth counter must be decremented.

    Subsequent subsession closures should be able to trigger new reactions
    (subject to the _MAX_REACTION_DEPTH cap).
    """
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["first"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    info_a = _make_info(sub_id="sub-aaaaaaaa", parent_id=None)
    info_b = _make_info(sub_id="sub-bbbbbbbb", parent_id=None)

    await delivery.deliver_summary(info_a, "summary a", "completed")
    await _await_reaction_tasks(delivery)
    # After the first reaction, the depth should be cleared.
    assert "owner-sess-1" not in delivery._reaction_depth

    # A second reaction should now proceed normally (not degraded).
    await delivery.deliver_summary(info_b, "summary b", "completed")
    await _await_reaction_tasks(delivery)
    assert store.record_for_session.call_count == 2
    # Second call should use the agent (not degraded).
    second_args, _kwargs = store.record_for_session.call_args
    assert "summary b" in second_args[1]  # prompt form


@pytest.mark.asyncio
async def test_deliver_summary_loop_guard_depth_cap_degrades() -> None:
    """At max depth, closures degrade to passive records — no agent turn."""
    from robotsix_chat.subsessions.delivery import _MAX_REACTION_DEPTH

    store = MagicMock()
    registry = MagicMock()
    agent = _fake_agent(["reply"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)

    # Push depth right up to the cap.
    delivery._reaction_depth["owner-sess-1"] = _MAX_REACTION_DEPTH

    info = _make_info(parent_id=None)
    await delivery.deliver_summary(info, "summary at cap", "completed")
    await _await_reaction_tasks(delivery)

    # Degraded to passive — label/outcome form.
    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[2] == "summary at cap"


@pytest.mark.asyncio
async def test_deliver_summary_loop_guard_depth_below_cap_schedules_reaction() -> None:
    """When reaction depth is below the cap, the reaction turn is scheduled."""
    from robotsix_chat.subsessions.delivery import _MAX_REACTION_DEPTH

    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["still reacting"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)

    # One below the cap — should still schedule.
    delivery._reaction_depth["owner-sess-1"] = _MAX_REACTION_DEPTH - 1

    info = _make_info(parent_id=None)
    await delivery.deliver_summary(info, "summary below cap", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert "summary below cap" in args[1]  # prompt form (not degraded)
    assert args[2] == "still reacting"


# ---------------------------------------------------------------------------
# deliver_summary — nested parent (parent_id is not None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_nested_parent_enqueues_message() -> None:
    """When parent_id is set and enqueue_message succeeds, no store write."""
    store = MagicMock()
    registry = MagicMock()
    registry.enqueue_message.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-sub-99")

    await delivery.deliver_summary(info, "nested done", "completed")

    registry.enqueue_message.assert_called_once()
    args, _kwargs = registry.enqueue_message.call_args
    assert args[0] == "parent-sub-99"  # parent_id
    assert args[1] == "parent"  # role
    assert "nested done" in args[2]  # text includes summary
    store.record_for_session.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_result_nested_parent_enqueues_message() -> None:
    """When parent_id is set and enqueue_message succeeds, no store write."""
    store = MagicMock()
    registry = MagicMock()
    registry.enqueue_message.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-sub-99")

    await delivery.deliver_result(info, 1, "first run result")

    registry.enqueue_message.assert_called_once()
    args, _kwargs = registry.enqueue_message.call_args
    assert args[0] == "parent-sub-99"
    assert args[1] == "parent"
    assert "first run result" in args[2]
    assert "run 1" in args[2]
    store.record_for_session.assert_not_called()


# ---------------------------------------------------------------------------
# deliver_summary / deliver_result — nested parent terminal (degrades to store)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_nested_parent_terminal_degrades_to_store() -> None:
    """When enqueue_message returns False, degrade to store (outcome not lost)."""
    store = MagicMock()
    registry = MagicMock()
    registry.enqueue_message.return_value = False  # parent is terminal
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-sub-terminal")

    await delivery.deliver_summary(info, "degraded summary", "completed")
    await _await_reaction_tasks(delivery)

    registry.enqueue_message.assert_called_once()
    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert "degraded summary" in args[2]


@pytest.mark.asyncio
async def test_deliver_result_nested_parent_terminal_degrades_to_store() -> None:
    """When enqueue_message returns False, deliver_result degrades to store."""
    store = MagicMock()
    registry = MagicMock()
    registry.enqueue_message.return_value = False
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-sub-terminal")

    await delivery.deliver_result(info, 5, "degraded result")
    await _await_reaction_tasks(delivery)

    registry.enqueue_message.assert_called_once()
    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert "degraded result" in args[2]


# ---------------------------------------------------------------------------
# deliver_summary / deliver_result — periodic parent (enqueues + reacts)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_periodic_parent_enqueues_and_reacts() -> None:
    """When parent is PERIODIC, enqueue to parent AND react in main chat."""
    store = MagicMock()
    registry = MagicMock()
    parent = MagicMock()
    parent.kind = SubsessionKind.PERIODIC
    registry.get.return_value = parent
    registry.enqueue_message.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-periodic", kind=SubsessionKind.USER_CHAT)

    await delivery.deliver_summary(info, "periodic child done", "completed")
    await _await_reaction_tasks(delivery)

    # Enqueued to the periodic parent's inbox.
    registry.enqueue_message.assert_called_once()
    args, _kwargs = registry.enqueue_message.call_args
    assert args[0] == "parent-periodic"
    assert args[1] == "parent"
    assert "periodic child done" in args[2]

    # Also scheduled a reaction in the main chat (the owner session).
    store.record_for_session.assert_called_once()
    store_args, _store_kwargs = store.record_for_session.call_args
    assert store_args[0] == "owner-sess-1"
    assert "periodic child done" in store_args[2]

    # The parent-kind check was done.
    registry.get.assert_called_with("parent-periodic")


@pytest.mark.asyncio
async def test_deliver_result_periodic_parent_enqueues_and_reacts() -> None:
    """When parent is PERIODIC, deliver_result enqueues to parent and reacts."""
    store = MagicMock()
    registry = MagicMock()
    parent = MagicMock()
    parent.kind = SubsessionKind.PERIODIC
    registry.get.return_value = parent
    registry.enqueue_message.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-periodic", kind=SubsessionKind.TASK)

    await delivery.deliver_result(info, 3, "periodic run result")
    await _await_reaction_tasks(delivery)

    registry.enqueue_message.assert_called_once()
    args, _kwargs = registry.enqueue_message.call_args
    assert args[0] == "parent-periodic"
    assert args[1] == "parent"
    assert "periodic run result" in args[2]
    assert "run 3" in args[2]

    store.record_for_session.assert_called_once()
    store_args, _store_kwargs = store.record_for_session.call_args
    assert store_args[0] == "owner-sess-1"
    assert "periodic run result" in store_args[2]


# ---------------------------------------------------------------------------
# exception paths — logged but not raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_exception_is_logged_not_raised() -> None:
    """An exception during delivery is logged but never propagates."""
    store = MagicMock()
    store.record_for_session.side_effect = RuntimeError("store is down")
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "exception") as log_exc:
        await delivery.deliver_summary(info, "summary", "completed")
        await _await_reaction_tasks(delivery)

    # Must not raise — we reach this line.
    log_exc.assert_called()
    # The exception is caught inside the background reaction task, not
    # in deliver_summary itself (fire-and-forget).
    assert "Reaction task failed for subsession" in log_exc.call_args[0][0]


@pytest.mark.asyncio
async def test_deliver_result_exception_is_logged_not_raised() -> None:
    """An exception during result delivery is logged but never propagates."""
    store = MagicMock()
    store.record_for_session.side_effect = RuntimeError("store is down")
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "exception") as log_exc:
        await delivery.deliver_result(info, 1, "text")
        await _await_reaction_tasks(delivery)

    log_exc.assert_called()
    assert "Reaction task failed for subsession" in log_exc.call_args[0][0]


@pytest.mark.asyncio
async def test_deliver_summary_enqueue_raises_is_logged_not_raised() -> None:
    """When enqueue_message raises, the exception is logged not raised."""
    store = MagicMock()
    registry = MagicMock()
    registry.enqueue_message.side_effect = RuntimeError("registry is down")
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-sub-99")

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "exception") as log_exc:
        await delivery.deliver_summary(info, "summary", "completed")

    log_exc.assert_called_once()
    # Store must not be called because the exception happened before degradation.
    store.record_for_session.assert_not_called()


@pytest.mark.asyncio
async def test_deliver_result_enqueue_raises_is_logged_not_raised() -> None:
    """When enqueue_message raises, deliver_result logs and does not raise."""
    store = MagicMock()
    registry = MagicMock()
    registry.enqueue_message.side_effect = RuntimeError("registry is down")
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id="parent-sub-99")

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "exception") as log_exc:
        await delivery.deliver_result(info, 1, "text")

    log_exc.assert_called_once()
    store.record_for_session.assert_not_called()


# ---------------------------------------------------------------------------
# run_serializer lock is acquired for store writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_acquires_run_serializer_lock() -> None:
    """Store writes happen inside the per-owner RunSerializer lock."""
    store = MagicMock()
    registry = MagicMock()
    lock = MagicMock()
    lock.__aenter__ = AsyncMock()
    lock.__aexit__ = AsyncMock()
    delivery = _build_delivery(store=store, registry=registry, lock=lock)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "s", "completed")
    await _await_reaction_tasks(delivery)

    lock.__aenter__.assert_awaited()
    lock.__aexit__.assert_awaited()


@pytest.mark.asyncio
async def test_deliver_summary_nested_enqueue_skips_lock() -> None:
    """When enqueue_message succeeds, the run serializer lock is not acquired."""
    store = MagicMock()
    registry = MagicMock()
    registry.enqueue_message.return_value = True
    lock = MagicMock()
    lock.__aenter__ = AsyncMock()
    lock.__aexit__ = AsyncMock()
    delivery = _build_delivery(store=store, registry=registry, lock=lock)
    info = _make_info(parent_id="parent-sub-99")

    await delivery.deliver_summary(info, "s", "completed")

    lock.__aenter__.assert_not_awaited()
    lock.__aexit__.assert_not_awaited()
