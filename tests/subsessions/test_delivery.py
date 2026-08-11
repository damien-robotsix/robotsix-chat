"""Tests for ``ParentDelivery`` — routing of subsession outcomes to parents."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from robotsix_chat.autonomous.models import AutonomousSession, AutonomousState
from robotsix_chat.subsessions.delivery import (
    _REACT_PROMPT_ACTIVE_PLAN_TEMPLATE,
    _REACT_PROMPT_TEMPLATE,
    ParentDelivery,
    _sanitize_reaction_reply,
    _strip_inline_metadata,
)
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
        trace_name=None,
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
        trace_name=None,
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
    lost.  Additionally, a fallback agent_message frame is published so the
    user still sees a live notification even when the LLM API is
    unavailable.
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

    # Fallback agent_message frame is published so the user sees the
    # outcome even when the LLM call fails.
    event_sink.publish.assert_called_once()
    call_args, _ = event_sink.publish.call_args
    assert call_args[0] == "owner-sess-1"
    frame = call_args[1]
    from robotsix_chat.chat.events import SSE_AGENT_MESSAGE_TYPE

    assert frame["type"] == SSE_AGENT_MESSAGE_TYPE
    assert "all done" in frame["text"]
    assert "test-job" in frame["text"]


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


# ---------------------------------------------------------------------------
# deliver_summary — nested parent terminal (degrades to store)
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


# ---------------------------------------------------------------------------
# deliver_summary — periodic parent (enqueues + reacts)
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


# ---------------------------------------------------------------------------
# user_chat transcript inclusion — outcome enrichment
# ---------------------------------------------------------------------------


def test_format_user_chat_outcome_no_transcript() -> None:
    """When transcript is empty, the outcome is just the summary (unchanged)."""
    from robotsix_chat.subsessions.delivery import _format_user_chat_outcome

    result = _format_user_chat_outcome("Decisions recorded", [])
    assert result == "Decisions recorded"


def test_format_user_chat_outcome_includes_transcript() -> None:
    """Transcript entries are formatted as role-tagged lines after the summary."""
    from robotsix_chat.subsessions.delivery import _format_user_chat_outcome
    from robotsix_chat.subsessions.models import TranscriptEntry

    transcript = [
        TranscriptEntry(role="assistant", text="What should we do?", timestamp=1.0),
        TranscriptEntry(role="user", text="Close the ticket.", timestamp=2.0),
        TranscriptEntry(role="assistant", text="OK, closing now.", timestamp=3.0),
    ]
    result = _format_user_chat_outcome("Decisions recorded", transcript)
    assert result.startswith("Decisions recorded\n\nConversation transcript:")
    assert "[assistant] What should we do?" in result
    assert "[user] Close the ticket." in result
    assert "[assistant] OK, closing now." in result


@pytest.mark.asyncio
async def test_deliver_summary_user_chat_includes_transcript_in_outcome() -> None:
    """For user_chat kind, the transcript is appended to the delivered outcome."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    from robotsix_chat.subsessions.models import TranscriptEntry

    info = _make_info(parent_id=None, kind=SubsessionKind.USER_CHAT)
    info.transcript = [
        TranscriptEntry(role="user", text="Yes, close it.", timestamp=1.0),
        TranscriptEntry(role="assistant", text="Will do.", timestamp=2.0),
    ]

    await delivery.deliver_summary(info, "Decisions recorded", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    outcome = args[2]
    assert outcome.startswith("Decisions recorded")
    assert "Conversation transcript:" in outcome
    assert "[user] Yes, close it." in outcome
    assert "[assistant] Will do." in outcome


@pytest.mark.asyncio
async def test_deliver_summary_task_kind_does_not_include_transcript() -> None:
    """Non-user_chat kinds (e.g. TASK) pass the summary through unchanged."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    from robotsix_chat.subsessions.models import TranscriptEntry

    info = _make_info(parent_id=None, kind=SubsessionKind.TASK)
    info.transcript = [
        TranscriptEntry(role="assistant", text="Task output.", timestamp=1.0),
    ]

    await delivery.deliver_summary(info, "task completed", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    outcome = args[2]
    # TASK kind: summary is NOT enriched with the transcript.
    assert outcome == "task completed"
    assert "Conversation transcript:" not in outcome


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


# ---------------------------------------------------------------------------
# deliver_summary — duplicate terminal-report suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_suppresses_duplicate_ticket_terminal() -> None:
    """When is_duplicate_ticket_terminal returns True, delivery is skipped entirely."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_ticket_terminal.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-123"}

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "info") as log_info:
        await delivery.deliver_summary(info, "all done", "ticket_terminal")

    # No reaction tasks scheduled — the early return happens synchronously.
    assert len(delivery._reaction_tasks) == 0

    # Store must NOT be called (no passive record, no reaction turn).
    store.record_for_session.assert_not_called()
    store.history.assert_not_called()

    # Registry enqueue must NOT be called.
    registry.enqueue_message.assert_not_called()

    # Suppression log message was emitted with correct ticket_id.
    log_info.assert_called_once()
    log_args = log_info.call_args[0]
    assert "Suppressing duplicate terminal report" in log_args[0]
    assert log_args[1] == "T-123"  # ticket_id as first %s arg


@pytest.mark.asyncio
async def test_deliver_summary_suppresses_duplicate_with_reason_completed() -> None:
    """Duplicate suppression also fires when reason is 'completed'."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_ticket_terminal.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-456"}

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "info") as log_info:
        await delivery.deliver_summary(info, "done", "completed")

    assert len(delivery._reaction_tasks) == 0
    store.record_for_session.assert_not_called()
    log_info.assert_called_once()
    log_args = log_info.call_args[0]
    assert log_args[1] == "T-456"  # ticket_id


@pytest.mark.asyncio
async def test_deliver_summary_no_check_on_non_terminal_reason() -> None:
    """When reason is not 'ticket_terminal'/'completed', delivery proceeds normally.

    is_duplicate_ticket_terminal is never consulted.
    """
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "paused", "paused")
    await _await_reaction_tasks(delivery)

    # is_duplicate_ticket_terminal must NOT be called.
    registry.is_duplicate_ticket_terminal.assert_not_called()
    # Delivery proceeded (passive record because no agent wired).
    store.record_for_session.assert_called()


@pytest.mark.asyncio
async def test_deliver_summary_no_check_when_ticket_id_is_none() -> None:
    """When _extract_ticket_id returns None, the duplicate check is skipped.

    This covers: no checkpoint, or checkpoint without a ticket_id key.
    """
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    # info has no checkpoint by default — _extract_ticket_id returns None.
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "all done", "ticket_terminal")
    await _await_reaction_tasks(delivery)

    # is_duplicate_ticket_terminal must NOT be called.
    registry.is_duplicate_ticket_terminal.assert_not_called()
    # Delivery proceeded normally.
    store.record_for_session.assert_called()


@pytest.mark.asyncio
async def test_deliver_summary_no_check_when_checkpoint_lacks_ticket_id() -> None:
    """When checkpoint exists but ticket_id is missing, duplicate check is skipped."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"other_key": "value"}  # no ticket_id

    await delivery.deliver_summary(info, "all done", "completed")
    await _await_reaction_tasks(delivery)

    registry.is_duplicate_ticket_terminal.assert_not_called()
    store.record_for_session.assert_called()


@pytest.mark.asyncio
async def test_deliver_summary_no_check_when_ticket_id_is_not_string() -> None:
    """When checkpoint ticket_id is not a str, duplicate check is skipped."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": 12345}  # int, not str

    await delivery.deliver_summary(info, "all done", "ticket_terminal")
    await _await_reaction_tasks(delivery)

    registry.is_duplicate_ticket_terminal.assert_not_called()
    store.record_for_session.assert_called()


@pytest.mark.asyncio
async def test_deliver_summary_no_suppression_when_check_returns_false() -> None:
    """When is_duplicate_ticket_terminal returns False, delivery proceeds normally."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_ticket_terminal.return_value = False
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-789"}

    await delivery.deliver_summary(info, "all done", "ticket_terminal")
    await _await_reaction_tasks(delivery)

    # is_duplicate_ticket_terminal was consulted.
    registry.is_duplicate_ticket_terminal.assert_called_once_with("T-789", info.id)
    # Delivery proceeded (no agent wired → passive record).
    store.record_for_session.assert_called()


# ---------------------------------------------------------------------------
# deliver_summary — duplicate auto-pause / no-change report suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_suppresses_duplicate_auto_pause() -> None:
    """When is_duplicate_auto_pause returns True, delivery is skipped for 'paused'."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_auto_pause.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-123"}

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "info") as log_info:
        await delivery.deliver_summary(info, "auto-paused", "paused")

    # No reaction tasks scheduled — early return.
    assert len(delivery._reaction_tasks) == 0
    store.record_for_session.assert_not_called()
    store.history.assert_not_called()
    registry.enqueue_message.assert_not_called()

    log_info.assert_called_once()
    log_args = log_info.call_args[0]
    assert "Suppressing duplicate auto-pause report" in log_args[0]
    assert log_args[1] == "T-123"


@pytest.mark.asyncio
async def test_deliver_summary_suppresses_dup_no_change_auto_stop() -> None:
    """Duplicate suppression fires when reason is 'no_change_auto_stop'."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_auto_pause.return_value = True
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-456"}

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "info") as log_info:
        await delivery.deliver_summary(info, "auto-stopped", "no_change_auto_stop")

    assert len(delivery._reaction_tasks) == 0
    store.record_for_session.assert_not_called()
    log_info.assert_called_once()
    log_args = log_info.call_args[0]
    assert log_args[1] == "T-456"


@pytest.mark.asyncio
async def test_deliver_summary_no_auto_pause_check_for_other_reasons() -> None:
    """is_duplicate_auto_pause skipped for non-pause/non-auto-stop reasons."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-789"}

    await delivery.deliver_summary(info, "failed", "failed")
    await _await_reaction_tasks(delivery)

    registry.is_duplicate_auto_pause.assert_not_called()
    store.record_for_session.assert_called()


@pytest.mark.asyncio
async def test_deliver_summary_no_auto_pause_check_when_ticket_id_none() -> None:
    """When _extract_ticket_id returns None, auto-pause duplicate check is skipped."""
    store = MagicMock()
    registry = MagicMock()
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)  # no checkpoint

    await delivery.deliver_summary(info, "paused", "paused")
    await _await_reaction_tasks(delivery)

    registry.is_duplicate_auto_pause.assert_not_called()
    store.record_for_session.assert_called()


@pytest.mark.asyncio
async def test_deliver_summary_no_auto_pause_suppression_when_check_false() -> None:
    """When is_duplicate_auto_pause returns False, delivery proceeds normally."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_auto_pause.return_value = False
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-789"}

    await delivery.deliver_summary(info, "paused", "paused")
    await _await_reaction_tasks(delivery)

    registry.is_duplicate_auto_pause.assert_called_once_with("T-789", info.id)
    store.record_for_session.assert_called()


# ---------------------------------------------------------------------------
# deliver_summary — terminal-state auto-pause suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deliver_summary_suppresses_auto_pause_for_terminal_ticket() -> None:
    """Auto-pause delivery is skipped when the monitored ticket is already terminal."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_auto_pause.return_value = False
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-123", "last_known_state": "closed"}

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "info") as log_info:
        await delivery.deliver_summary(info, "auto-paused", "paused")

    # No reaction tasks scheduled — early return.
    assert len(delivery._reaction_tasks) == 0
    store.record_for_session.assert_not_called()
    store.history.assert_not_called()
    registry.enqueue_message.assert_not_called()

    assert log_info.called
    suppression_calls = [
        c
        for c in log_info.call_args_list
        if "ticket is already in a terminal state" in str(c.args[0])
    ]
    assert len(suppression_calls) == 1
    assert suppression_calls[0].args[1] == "T-123"


@pytest.mark.asyncio
async def test_deliver_summary_suppresses_auto_stop_for_terminal_ticket() -> None:
    """Auto-stop delivery is skipped when the monitored ticket is already terminal."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_auto_pause.return_value = False
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-456", "last_known_state": "done"}

    with patch.object(logging.getLogger(_DELIVERY_LOGGER), "info") as log_info:
        await delivery.deliver_summary(info, "auto-stopped", "no_change_auto_stop")

    assert len(delivery._reaction_tasks) == 0
    store.record_for_session.assert_not_called()
    suppression_calls = [
        c
        for c in log_info.call_args_list
        if "ticket is already in a terminal state" in str(c.args[0])
    ]
    assert len(suppression_calls) == 1
    assert suppression_calls[0].args[1] == "T-456"


@pytest.mark.asyncio
async def test_deliver_no_suppression_for_non_terminal_ticket() -> None:
    """Auto-pause delivery proceeds when the ticket is not in a terminal state."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_auto_pause.return_value = False
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-789", "last_known_state": "open"}

    await delivery.deliver_summary(info, "auto-paused", "paused")
    await _await_reaction_tasks(delivery)

    # Delivery should have proceeded (store was called).
    store.record_for_session.assert_called()


@pytest.mark.asyncio
async def test_deliver_no_suppression_when_last_known_missing() -> None:
    """Auto-pause delivery proceeds when checkpoint has no last_known_state."""
    store = MagicMock()
    registry = MagicMock()
    registry.is_duplicate_auto_pause.return_value = False
    delivery = _build_delivery(store=store, registry=registry)
    info = _make_info(parent_id=None)
    info.checkpoint = {"ticket_id": "T-789"}  # no last_known_state

    await delivery.deliver_summary(info, "auto-paused", "paused")
    await _await_reaction_tasks(delivery)

    # Delivery should have proceeded (store was called).
    store.record_for_session.assert_called()


# ---------------------------------------------------------------------------
# Reaction prompt — active autonomous plan detection
# ---------------------------------------------------------------------------


def _mock_autonomous_runner(
    session_id: str, state: AutonomousState, plan_text: str = "test plan"
) -> MagicMock:
    """Build an AutonomousRunner stub with a session in *state*."""
    runner = MagicMock()
    session = AutonomousSession(
        session_id=session_id,
        owner_id=session_id,
        state=state,
        plan_text=plan_text,
    )
    runner.get_session.return_value = session
    return runner


@pytest.mark.asyncio
async def test_reaction_with_autonomous_executing_uses_active_plan_template() -> None:
    """Reaction prompt uses the active-plan template when the session is executing."""
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["Noted — continuing the plan."])
    runner = _mock_autonomous_runner(
        "owner-sess-1", AutonomousState.executing, "Close the misfiled ticket"
    )
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    delivery.set_autonomous_runner(runner)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "P1 outage resolved", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    prompt = args[1]
    # The active-plan template must be used, not the default one.
    assert "executing your approved plan" in prompt
    assert "Close the misfiled ticket" in prompt
    assert "DO NOT re-request approval" in prompt
    assert "P1 outage resolved" in prompt
    # The default template's "not actively conversing" phrase must be absent.
    assert "not actively conversing" not in prompt


@pytest.mark.asyncio
async def test_reaction_with_autonomous_proposal_uses_active_plan_template() -> None:
    """Reaction prompt uses the active-plan template when the session is in proposal."""
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["Noted."])
    runner = _mock_autonomous_runner(
        "owner-sess-1", AutonomousState.proposal, "Proposed: close misfiled ticket"
    )
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    delivery.set_autonomous_runner(runner)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "P1 outage resolved", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    prompt = args[1]
    assert "waiting for operator approval of" in prompt
    assert "Proposed: close misfiled ticket" in prompt
    assert "DO NOT re-request approval" in prompt
    assert "not actively conversing" not in prompt


@pytest.mark.asyncio
async def test_reaction_with_autonomous_planning_uses_default_template() -> None:
    """Default template is used when the session is still in planning state."""
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["ok"])
    runner = _mock_autonomous_runner("owner-sess-1", AutonomousState.planning)
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    delivery.set_autonomous_runner(runner)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "some outcome", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    prompt = args[1]
    assert "not actively conversing" in prompt
    assert "DO NOT re-request approval" not in prompt


@pytest.mark.asyncio
async def test_reaction_without_autonomous_runner_uses_default_template() -> None:
    """Default template is used when no autonomous runner is wired (backward compat)."""
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["got it"])
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    # Intentionally not calling set_autonomous_runner.
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "some outcome", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    prompt = args[1]
    assert "not actively conversing" in prompt
    assert "DO NOT re-request approval" not in prompt


@pytest.mark.asyncio
async def test_reaction_unknown_session_uses_default_template() -> None:
    """Default template is used when the runner has no record for this session."""
    store = MagicMock()
    store.history.return_value = []
    registry = MagicMock()
    agent = _fake_agent(["ok"])
    runner = MagicMock()
    runner._sessions = {}  # no sessions
    delivery = _build_delivery(store=store, registry=registry, agent=agent)
    delivery.set_autonomous_runner(runner)
    info = _make_info(parent_id=None)

    await delivery.deliver_summary(info, "outcome", "completed")
    await _await_reaction_tasks(delivery)

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    prompt = args[1]
    assert "not actively conversing" in prompt
    assert "DO NOT re-request approval" not in prompt


def test_react_prompt_forbids_reemitting_already_shown_payload() -> None:
    """The REACT prompt template must forbid re-emitting already-shown data."""
    text = _REACT_PROMPT_TEMPLATE.lower()
    assert "do not re-list" in text
    assert "delta" in text


def test_active_plan_react_prompt_forbids_reemitting_already_shown_payload() -> None:
    """Active-plan template must forbid re-emitting already-shown data."""
    text = _REACT_PROMPT_ACTIVE_PLAN_TEMPLATE.lower()
    assert "delta" in text
    assert "already presented" in text


# ---------------------------------------------------------------------------
# _sanitize_reaction_reply — stripping internal metadata from reaction output
# ---------------------------------------------------------------------------


class TestSanitizeReactionReply:
    """Unit tests for ``_sanitize_reaction_reply`` and ``_strip_inline_metadata``."""

    # -- lines that are purely metadata → dropped entirely -------------------

    def test_strips_bare_kind_status_line(self) -> None:
        """Lines like '0 kind=periodic status=closed' are dropped entirely."""
        result = _sanitize_reaction_reply("0 kind=periodic status=closed")
        assert "kind=" not in result
        assert "status=" not in result
        assert "nothing new to report" in result

    def test_strips_bracketed_kind_status_line(self) -> None:
        """Lines like '[1] kind=periodic status=closed' are dropped."""
        result = _sanitize_reaction_reply("[1] kind=periodic status=closed")
        assert "kind=" not in result
        assert "status=" not in result

    def test_strips_no_ticket_id_line(self) -> None:
        """Lines containing 'No ticket_id in checkpoint' are dropped."""
        result = _sanitize_reaction_reply("No ticket_id in checkpoint for T-123")
        assert "ticket_id" not in result

    def test_strips_sub_id_line(self) -> None:
        """Lines starting with 'sub_id' followed by an identifier are dropped."""
        result = _sanitize_reaction_reply("sub_id abc12345: all done")
        assert "sub_id" not in result

    def test_strips_multiple_metadata_lines(self) -> None:
        """Multiple metadata-only lines are all dropped."""
        result = _sanitize_reaction_reply(
            "0 kind=periodic status=closed\n"
            "[1] kind=periodic status=closed\n"
            "No ticket_id in checkpoint"
        )
        assert "kind=" not in result
        assert "ticket_id" not in result
        assert "nothing new to report" in result

    # -- valid content passes through ---------------------------------------

    def test_preserves_valid_content(self) -> None:
        """Normal user-facing text passes through unchanged."""
        reply = (
            "Tracking complete for ticket T-123 — it was resolved.\nNo action needed."
        )
        result = _sanitize_reaction_reply(reply)
        assert result == reply

    def test_preserves_kind_word_without_equals(self) -> None:
        """The word 'kind' without '=' is not metadata — passes through."""
        reply = "What kind of ticket is this? It looks like a bug."
        result = _sanitize_reaction_reply(reply)
        assert "kind of ticket" in result

    def test_preserves_status_word_without_equals(self) -> None:
        """The word 'status' without '=' is not metadata — passes through."""
        reply = "The current status is that everything is working."
        result = _sanitize_reaction_reply(reply)
        assert "current status" in result

    # -- mixed content: metadata stripped, valid content kept --------------

    def test_strips_inline_kind_status_from_mixed_line(self) -> None:
        """Inline 'kind=X status=Y' is stripped from otherwise valid lines."""
        reply = "The monitor kind=periodic status=closed has finished."
        result = _sanitize_reaction_reply(reply)
        assert "The monitor" in result
        assert "has finished." in result
        assert "kind=" not in result
        assert "status=" not in result

    def test_strips_parenthetical_metadata(self) -> None:
        """Parenthetical metadata like '(0 kind=periodic status=closed)' is stripped."""
        reply = "The periodic check (0 kind=periodic status=closed) found no changes."
        result = _sanitize_reaction_reply(reply)
        assert "The periodic check" in result
        assert "found no changes." in result
        assert "kind=" not in result
        assert "status=" not in result

    def test_strips_bracket_prefix_from_mixed_line(self) -> None:
        """'[0]' prefix patterns are stripped from mixed lines."""
        reply = "[0] The ticket T-123 was resolved."
        result = _sanitize_reaction_reply(reply)
        assert result.startswith("The ticket T-123 was resolved.")

    # -- empty / whitespace -------------------------------------------------

    def test_empty_reply_returns_empty(self) -> None:
        """An empty string stays empty (no fallback injection)."""
        result = _sanitize_reaction_reply("")
        assert result == ""

    def test_whitespace_only_returns_unchanged(self) -> None:
        """A whitespace-only string is returned as-is (no metadata to strip)."""
        result = _sanitize_reaction_reply("   \n  ")
        assert result == "   \n  "

    # -- integration: reaction turn with leaking agent ----------------------

    @pytest.mark.asyncio
    async def test_reaction_sanitizes_leaked_metadata_before_record(self) -> None:
        """Sanitize leaked metadata before recording and publishing.

        When the agent leaks raw metadata, the sanitizer cleans it before
        the reply is recorded and published.
        """
        store = MagicMock()
        store.history.return_value = []
        registry = MagicMock()
        # Agent that outputs raw internal metadata despite prompt instructions.
        agent = _fake_agent(
            [
                "0 kind=periodic status=closed\n",
                "No ticket_id in checkpoint\n",
                "The monitor tracked T-456 and found no changes.",
            ]
        )
        event_sink = MagicMock()
        delivery = _build_delivery(
            store=store, registry=registry, agent=agent, event_sink=event_sink
        )
        info = _make_info(parent_id=None)

        await delivery.deliver_summary(info, "monitor check done", "paused")
        await _await_reaction_tasks(delivery)

        # The recorded reply must be sanitized.
        store.record_for_session.assert_called_once()
        _args, kwargs = store.record_for_session.call_args
        recorded_reply = _args[2] if len(_args) >= 3 else kwargs.get("reply", "")
        assert "kind=" not in recorded_reply
        assert "ticket_id" not in recorded_reply
        assert "T-456" in recorded_reply
        assert "found no changes" in recorded_reply

        # The published frame must also be sanitized.
        if event_sink.publish.called:
            _session_id, frame = event_sink.publish.call_args[0]
            assert "kind=" not in frame["text"]
            assert "ticket_id" not in frame["text"]

    @pytest.mark.asyncio
    async def test_reaction_all_metadata_yields_fallback(self) -> None:
        """When the agent outputs ONLY metadata, the fallback message is used."""
        store = MagicMock()
        store.history.return_value = []
        registry = MagicMock()
        agent = _fake_agent(
            ["0 kind=periodic status=closed\n", "[1] kind=task status=running\n"]
        )
        delivery = _build_delivery(store=store, registry=registry, agent=agent)
        info = _make_info(parent_id=None)

        await delivery.deliver_summary(info, "monitor done", "completed")
        await _await_reaction_tasks(delivery)

        store.record_for_session.assert_called_once()
        _args, kwargs = store.record_for_session.call_args
        recorded_reply = _args[2] if len(_args) >= 3 else kwargs.get("reply", "")
        assert "nothing new to report" in recorded_reply
        assert "kind=" not in recorded_reply


class TestStripInlineMetadata:
    """Unit tests for ``_strip_inline_metadata``."""

    def test_removes_parenthetical_metadata(self) -> None:
        """'(0 kind=periodic status=closed)' is removed from the line."""
        result = _strip_inline_metadata(
            "Monitor (0 kind=periodic status=closed) finished."
        )
        assert result == "Monitor finished."

    def test_removes_bare_kind_status(self) -> None:
        """Bare 'kind=periodic status=closed' is stripped."""
        result = _strip_inline_metadata("Monitor kind=periodic status=closed done.")
        assert result == "Monitor done."

    def test_removes_bracket_prefix(self) -> None:
        """'[0] ' prefix is stripped."""
        result = _strip_inline_metadata("[0] Ticket resolved.")
        assert result == "Ticket resolved."

    def test_cleans_double_spaces(self) -> None:
        """Multiple spaces are collapsed to one."""
        result = _strip_inline_metadata("The   monitor    finished.")
        assert result == "The monitor finished."

    def test_cleans_empty_parens(self) -> None:
        """Empty parentheses artifacts are removed."""
        result = _strip_inline_metadata("Monitor () finished.")
        assert result == "Monitor finished."

    def test_preserves_normal_text(self) -> None:
        """Normal text without metadata patterns passes through unchanged."""
        result = _strip_inline_metadata("The ticket T-123 was resolved.")
        assert result == "The ticket T-123 was resolved."
