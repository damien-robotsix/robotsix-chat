"""End-to-end lifecycle test for the unified subsession system.

Exercises the wired system through the HTTP surface: a ``user_chat``
subsession is spawned against a real ``SubsessionRegistry`` + ``EventBus``
+ ``ParentDelivery``; lifecycle frames are observed on the EventBus (the
same queues ``GET /events`` streams from), the user replies through
``POST /subsessions/{id}/message``, the transcript grows, and the final
``POST /subsessions/{id}/close`` delivers a summary turn into the owning
session's ``ConversationStore`` (visible via ``GET /history``).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import (
    SSE_SUBSESSION_CLOSED_TYPE,
    SSE_SUBSESSION_MESSAGE_TYPE,
    SSE_SUBSESSION_STARTED_TYPE,
    EventBus,
)
from robotsix_chat.chat.server import RunSerializer, create_app
from robotsix_chat.chat.server.routes import ChatAgent
from robotsix_chat.subsessions import (
    ParentDelivery,
    SubsessionEnv,
    SubsessionKind,
    SubsessionRegistry,
    spawn_subsession,
)
from robotsix_chat.subsessions.worker import _USER_CHAT_FIRST_TURN_NOTE
from tests.common.subsession_fakes import (
    CapturingAgentFactory,
    FakeAgent,
    make_settings,
    wait_until,
)
from tests.conftest import MockAgent, http_client

SESSION = "e2e-session"


async def _next_frame_of_type(
    queue: asyncio.Queue[dict[str, object]],
    frame_type: str,
    *,
    timeout: float = 2.0,
) -> dict[str, object]:
    """Consume frames from *queue* until one of *frame_type* arrives."""

    async def _drain() -> dict[str, object]:
        while True:
            frame = await queue.get()
            if frame.get("type") == frame_type:
                return frame

    return await asyncio.wait_for(_drain(), timeout)


def _wire(
    agent: FakeAgent,
) -> tuple[EventBus, SubsessionEnv, ConversationStore, Callable[[], ChatAgent]]:
    """Build the real subsession stack shared by app and worker."""
    bus = EventBus()
    store = ConversationStore()
    serializer = RunSerializer()
    registry = SubsessionRegistry(event_sink=bus, store_path=None)
    delivery = ParentDelivery(
        conversation_store=store, registry=registry, run_serializer=serializer
    )
    env = SubsessionEnv(
        settings=make_settings(),
        registry=registry,
        delivery=delivery,
        conversation_store=store,
        agent_factory=CapturingAgentFactory(agent),
        event_sink=bus,
    )

    def _app_factory() -> ChatAgent:
        return MockAgent()

    return bus, env, store, _app_factory


@pytest.mark.asyncio
async def test_user_chat_subsession_full_lifecycle_over_http() -> None:
    """Spawn → SSE frames → user message → transcript growth → close → history."""
    agent = FakeAgent(["hello, what do you need?", "here are the details"])
    bus, env, store, app_agent = _wire(agent)
    app = create_app(
        app_agent(),
        conversation_store=store,
        event_bus=bus,
        subsession_registry=env.registry,
        subsession_delivery=env.delivery,
    )

    # Subscribe like a connected browser (the /events endpoint streams
    # from exactly these EventBus queues).
    queue = bus.subscribe(SESSION)

    async with http_client(app) as client:
        # -- spawn a user_chat subsession ----------------------------------
        sub_id = spawn_subsession(
            env=env,
            kind=SubsessionKind.USER_CHAT,
            owner_session_id=SESSION,
            parent_id=None,
            depth=1,
            title="deploy question",
            prompt="ask the user about the deploy window",
            model_level=3,
        )

        started = await _next_frame_of_type(queue, SSE_SUBSESSION_STARTED_TYPE)
        assert started["subsession_id"] == sub_id
        assert started["kind"] == SubsessionKind.USER_CHAT.value
        assert started["title"] == "deploy question"

        first_msg = await _next_frame_of_type(queue, SSE_SUBSESSION_MESSAGE_TYPE)
        assert first_msg["subsession_id"] == sub_id
        assert first_msg["role"] == "assistant"
        assert first_msg["text"] == "hello, what do you need?"

        # -- the subsession shows up on the REST surface -------------------
        listing = await client.get("/subsessions", params={"session_id": SESSION})
        assert listing.status_code == 200
        assert [s["subsession_id"] for s in listing.json()["subsessions"]] == [sub_id]

        # -- user replies via POST /subsessions/{id}/message ----------------
        posted = await client.post(
            f"/subsessions/{sub_id}/message", json={"text": "next Tuesday works"}
        )
        assert posted.status_code == 202
        assert posted.json() == {"subsession_id": sub_id, "status": "queued"}

        # The user message is echoed as a frame, then the worker runs
        # another turn and the assistant reply follows.
        user_msg = await _next_frame_of_type(queue, SSE_SUBSESSION_MESSAGE_TYPE)
        assert user_msg["role"] == "user"
        assert user_msg["text"] == "next Tuesday works"
        second_reply = await _next_frame_of_type(queue, SSE_SUBSESSION_MESSAGE_TYPE)
        assert second_reply["role"] == "assistant"
        assert second_reply["text"] == "here are the details"

        # The FakeAgent saw the grown history on its second turn.
        await wait_until(lambda: len(agent.calls) == 2)
        expected_first_turn = (
            _USER_CHAT_FIRST_TURN_NOTE + "\n\nask the user about the deploy window",
            "hello, what do you need?",
        )
        assert agent.calls[1]["history"] == [expected_first_turn]

        # -- transcript grew (assistant, user, assistant) --------------------
        detail = await client.get(f"/subsessions/{sub_id}")
        assert detail.status_code == 200
        transcript = detail.json()["transcript"]
        assert [(e["role"], e["text"]) for e in transcript] == [
            ("assistant", "hello, what do you need?"),
            ("user", "next Tuesday works"),
            ("assistant", "here are the details"),
        ]

        # -- close from the UI ----------------------------------------------
        worker = env.registry._running.get(sub_id)
        closed_resp = await client.post(f"/subsessions/{sub_id}/close")
        assert closed_resp.status_code == 200
        body = closed_resp.json()
        assert body["subsession_id"] == sub_id
        assert body["closed"] is True
        assert "here are the details" in body["summary"]

        closed_frame = await _next_frame_of_type(queue, SSE_SUBSESSION_CLOSED_TYPE)
        assert closed_frame["subsession_id"] == sub_id
        assert closed_frame["closed_by"] == "user"

        if worker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(worker, 2.0)

        # -- the summary turn landed in the owning session's history --------
        history_resp = await client.get("/history", params={"session_id": SESSION})
        assert history_resp.status_code == 200
        turns = history_resp.json()["turns"]
        assert len(turns) == 1
        label, summary = turns[0]
        assert label.startswith(f"[Subsession {sub_id[:8]} (user_chat)")
        assert "here are the details" in summary

    bus.unsubscribe(SESSION, queue)
