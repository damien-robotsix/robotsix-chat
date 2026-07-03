"""Tests for ``ParentDelivery`` — routing of subsession outcomes to parents."""

from __future__ import annotations

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
) -> ParentDelivery:
    """Build a ``ParentDelivery`` with mocked collaborators."""
    store = store or MagicMock()
    registry = registry or MagicMock()
    run_serializer = MagicMock()
    run_serializer.for_owner.return_value = lock or _async_context_manager()
    return ParentDelivery(
        conversation_store=store,
        registry=registry,
        run_serializer=run_serializer,
    )


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

    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert info.id[:8] in args[1]  # label (id truncated to 8 chars)
    assert "run 3" in args[1]
    assert "interim result text" in args[2]


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

    registry.enqueue_message.assert_called_once()
    store.record_for_session.assert_called_once()
    args, _kwargs = store.record_for_session.call_args
    assert args[0] == "owner-sess-1"
    assert "degraded result" in args[2]


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

    # Must not raise — we reach this line.
    log_exc.assert_called_once()
    assert "Failed to deliver subsession" in log_exc.call_args[0][0]
    assert log_exc.call_args[0][1] == info.id


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

    log_exc.assert_called_once()
    assert "Failed to deliver subsession" in log_exc.call_args[0][0]


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

    lock.__aenter__.assert_awaited_once()
    lock.__aexit__.assert_awaited_once()


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
