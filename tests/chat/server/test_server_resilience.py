"""Resilience tests for the chat SSE server.

Covers client-disconnect persistence, message coalescing, event-bus turn
mirroring, turn retry, the shared create_app/run_server parameter guard and
the periodic failsafe / pending-report paths. Split out of ``test_server.py``
(module-size).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
)
from robotsix_chat.subsessions import SubsessionKind, SubsessionRegistry
from tests.chat.server.test_server import _parse_sse, _register_subsession
from tests.conftest import MockAgent, mock_app

# ---------------------------------------------------------------------------
# Chat endpoint — client disconnect resilience
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_persists_on_client_disconnect() -> None:
    """Persist the reply to conversation history even when the client disconnects."""
    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    from robotsix_chat.chat.server.routes import chat_endpoint

    async with mock_app(tokens=["Hello", " ", "world!"]) as f:
        session_id = "test-disconnect-persist"
        owner_id = "owner-disconnect"

        body = json.dumps(
            {"message": "hello", "session_id": session_id, "owner_id": owner_id}
        ).encode()

        body_sent = False

        async def receive() -> dict[str, object]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        scope: dict[str, object] = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "path": "/chat",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
            ],
            "app": f.app,
        }

        request = Request(scope, receive)
        response = await chat_endpoint(request)
        assert isinstance(response, StreamingResponse)

        body_iter = response.body_iterator
        # Consume the heartbeat to confirm the stream started.
        chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=2.0)
        assert chunk == b": keepalive\n\n"

        # Simulate client disconnect.
        await body_iter.aclose()

        # The turn must be persisted despite the disconnect — by the
        # background coalescer task, eventually (the stream no longer holds
        # the request open until completion).
        turns: list[tuple[str, str]] = []
        for _ in range(500):
            turns = f.app.state.conversation_store.history(session_id)
            if turns:
                break
            await asyncio.sleep(0.01)
        assert len(turns) == 1
        assert turns[0] == ("hello", "Hello world!")


# ---------------------------------------------------------------------------
# Message coalescing — rapid-fire messages batched into a single agent run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coalescer_concatenates_rapid_messages() -> None:
    """Two rapid messages in the same session are concatenated into one agent run."""
    async with mock_app(tokens=["ok"], message_coalesce_seconds=0.0) as f:
        session_id = "s1"
        owner_id = "o1"

        # Send two messages simultaneously (fire-and-forget, let both land
        # before the coalescer's debounce fires).
        t1 = asyncio.create_task(
            f.client.post(
                "/chat",
                json={
                    "message": "msg1",
                    "session_id": session_id,
                    "owner_id": owner_id,
                },
            )
        )
        t2 = asyncio.create_task(
            f.client.post(
                "/chat",
                json={
                    "message": "msg2",
                    "session_id": session_id,
                    "owner_id": owner_id,
                },
            )
        )
        responses = await asyncio.gather(t1, t2)

        # Both responses are successful SSE streams.
        for r in responses:
            assert r.status_code == 200
            assert r.headers["content-type"] == SSE_CONTENT_TYPE

        # The agent should have been called exactly once.
        assert f.agent.call_count == 1

        # The concatenated message should contain both messages in order.
        assert "msg1" in f.agent.called_with
        assert "msg2" in f.agent.called_with
        assert f.agent.called_with.index("msg1") < f.agent.called_with.index("msg2")

        # The separator should be present between the two messages.
        assert f.app.state.message_coalescer.MESSAGE_SEPARATOR in f.agent.called_with

        # Only one turn should be recorded in conversation history.
        turns = f.app.state.conversation_store.history(session_id)
        assert len(turns) == 1


@pytest.mark.asyncio
async def test_chat_turn_mirrored_to_event_bus() -> None:
    """A foreground /chat turn is mirrored onto the EventBus.

    It publishes chat_turn_started / chat_token / chat_turn_done so other
    views can re-attach to the live turn.
    """
    from robotsix_chat.chat.events import (
        SSE_CHAT_TOKEN_TYPE,
        SSE_CHAT_TURN_DONE_TYPE,
        SSE_CHAT_TURN_STARTED_TYPE,
    )

    async with mock_app(
        tokens=["Hello", " ", "world!"], message_coalesce_seconds=0.0
    ) as f:
        session_id = "s-mirror"
        # Subscribe before sending so the whole turn is captured.
        q = f.app.state.event_bus.subscribe(session_id)

        r = await f.client.post(
            "/chat",
            json={"message": "hi", "session_id": session_id, "owner_id": "o"},
        )
        assert r.status_code == 200

        # Drain the bus, waiting briefly for end_turn (runs in the background
        # coalescer task, just after the SSE response completes).
        frames: list[dict[str, object]] = []
        for _ in range(100):
            while not q.empty():
                frames.append(q.get_nowait())
            if frames and frames[-1]["type"] == SSE_CHAT_TURN_DONE_TYPE:
                break
            await asyncio.sleep(0.01)

        types = [fr["type"] for fr in frames]
        assert types[0] == SSE_CHAT_TURN_STARTED_TYPE
        assert SSE_CHAT_TOKEN_TYPE in types
        assert types[-1] == SSE_CHAT_TURN_DONE_TYPE

        # A single turn_id threads the whole turn.
        turn_ids = {fr["turn_id"] for fr in frames}
        assert len(turn_ids) == 1

        # The streamed tokens reconstruct the reply.
        content = "".join(
            str(fr["content"]) for fr in frames if fr["type"] == SSE_CHAT_TOKEN_TYPE
        )
        assert content == "Hello world!"


@pytest.mark.asyncio
async def test_event_bus_turn_error_is_curated() -> None:
    """The turn-error published to the bus carries no raw exception text.

    ``end_turn(error=...)`` publishes to every subscriber of the session, so
    this path is client-facing too — not an internal-only sink.
    """
    from robotsix_chat.chat.events import SSE_CHAT_TURN_ERROR_TYPE

    secret = "/srv/app/.env token=sk-leaked https://upstream.internal/v1"
    async with mock_app(error=RuntimeError(secret), message_coalesce_seconds=0.0) as f:
        session_id = "s-error-bus"
        q = f.app.state.event_bus.subscribe(session_id)

        r = await f.client.post(
            "/chat",
            json={"message": "hi", "session_id": session_id, "owner_id": "o"},
        )
        assert r.status_code == 200

        frames: list[dict[str, object]] = []
        for _ in range(100):
            while not q.empty():
                frames.append(q.get_nowait())
            if frames and frames[-1]["type"] == SSE_CHAT_TURN_ERROR_TYPE:
                break
            await asyncio.sleep(0.01)

        error_frames = [fr for fr in frames if fr["type"] == SSE_CHAT_TURN_ERROR_TYPE]
        assert len(error_frames) == 1
        message = str(error_frames[0]["message"])
        assert secret not in message
        assert "sk-leaked" not in message
        assert message


@pytest.mark.asyncio
async def test_coalescer_single_message_passes_unchanged() -> None:
    """A single message with no racing partner is processed normally."""
    async with mock_app(tokens=["ok"], message_coalesce_seconds=0.0) as f:
        session_id = "s-solo"
        owner_id = "o-solo"

        r = await f.client.post(
            "/chat",
            json={
                "message": "just one message",
                "session_id": session_id,
                "owner_id": owner_id,
            },
        )
        assert r.status_code == 200
        assert f.agent.call_count == 1
        assert f.agent.called_with == "just one message"

        # No separator in a single-message batch.
        sep = f.app.state.message_coalescer.MESSAGE_SEPARATOR
        assert sep not in f.agent.called_with


@pytest.mark.asyncio
async def test_coalescer_separate_sessions_processed_independently() -> None:
    """Messages for different sessions are not batched together."""
    async with mock_app(tokens=["ok"], message_coalesce_seconds=0.0) as f:
        t1 = asyncio.create_task(
            f.client.post(
                "/chat",
                json={
                    "message": "session-a",
                    "session_id": "sa",
                    "owner_id": "oa",
                },
            )
        )
        t2 = asyncio.create_task(
            f.client.post(
                "/chat",
                json={
                    "message": "session-b",
                    "session_id": "sb",
                    "owner_id": "ob",
                },
            )
        )
        await asyncio.gather(t1, t2)

        # Each session triggers its own agent run.
        assert f.agent.call_count == 2


@pytest.mark.asyncio
async def test_coalescer_second_batch_after_first_completes() -> None:
    """Messages arriving after the first batch completes start a new batch."""
    async with mock_app(tokens=["ok"], message_coalesce_seconds=0.0) as f:
        session_id = "s-batch"
        owner_id = "o-batch"

        # First batch.
        await f.client.post(
            "/chat",
            json={
                "message": "batch1-msg1",
                "session_id": session_id,
                "owner_id": owner_id,
            },
        )
        assert f.agent.call_count == 1

        # After the first batch completes, a new message starts a fresh batch.
        await f.client.post(
            "/chat",
            json={
                "message": "batch2-msg1",
                "session_id": session_id,
                "owner_id": owner_id,
            },
        )
        assert f.agent.call_count == 2
        assert f.agent.called_with == "batch2-msg1"


# -- create_app / run_server shared-parameter sync guard --------------------


def test_create_app_and_run_server_shared_params() -> None:
    """Verify ``create_app()`` and ``run_server()`` share keyword params."""
    from inspect import signature

    from robotsix_chat.chat.server.app import (
        SHARED_PARAMS,
    )
    from robotsix_chat.chat.server.app import (
        create_app as _create_app,
    )
    from robotsix_chat.chat.server.cli import run_server as _run_server

    ca_sig = signature(_create_app)
    rs_sig = signature(_run_server)

    for name in SHARED_PARAMS:
        assert name in ca_sig.parameters, (
            f"Missing in create_app: '{name}' — add it to the signature "
            f"or remove it from SHARED_PARAMS"
        )
        assert name in rs_sig.parameters, (
            f"Missing in run_server: '{name}' — add it to the signature "
            f"or remove it from SHARED_PARAMS"
        )

        ca_default = ca_sig.parameters[name].default
        rs_default = rs_sig.parameters[name].default
        assert ca_default == rs_default, (
            f"Default mismatch for '{name}': "
            f"create_app={ca_default!r}, run_server={rs_default!r}"
        )


@pytest.mark.asyncio
async def test_failed_turn_is_persisted_to_history() -> None:
    """A turn that errors still lands in the transcript (message + error).

    Operator-reported: messages were lost when the turn failed while they
    were viewing another session — nothing was recorded on the error path.
    """

    class ExplodingAgent(MockAgent):
        async def stream(self, message: str, **kwargs: Any) -> AsyncIterator[str]:  # type: ignore[override]
            raise RuntimeError("deterministic boom")
            yield ""  # pragma: no cover

    store = ConversationStore()
    sid = cast(str, store.create_session("owner-loss")["session_id"])
    async with mock_app(conversation_store=store) as f:
        f.app.state.agent = ExplodingAgent(tokens=[])
        response = await f.client.post(
            "/chat",
            json={
                "message": "important ask",
                "session_id": sid,
                "owner_id": "owner-loss",
            },
        )
        frames = _parse_sse(response)

    assert [fr for fr in frames if fr["type"] == SSE_ERROR_TYPE]
    history = store.history(sid)
    assert len(history) == 1
    user_msg, reply = history[0][0], history[0][1]
    assert "important ask" in user_msg
    assert "turn failed" in reply


@pytest.mark.asyncio
async def test_turn_retries_transparently_on_pre_stream_quota_failure() -> None:
    """A retryable failure before any token streamed is retried in place.

    Operator-reported: having to resend messages whenever Claude is depleted
    and the backup tier hiccups. The turn loop retries (up to 3 attempts)
    when nothing has streamed yet and the failure classifies as
    budget_exhausted / rate-limit / timeout.
    """
    from robotsix_llmio.claude_sdk import ClaudeSDKUsageExhaustedError

    class FlakyAgent(MockAgent):
        """Fails the first stream call with a chained quota error."""

        attempts = 0

        async def stream(self, message: str, **kwargs: Any) -> AsyncIterator[str]:  # type: ignore[override]
            type(self).attempts += 1
            if type(self).attempts == 1:
                try:
                    raise ClaudeSDKUsageExhaustedError(
                        "You've hit your limit · resets 1pm (UTC)"
                    )
                except ClaudeSDKUsageExhaustedError:
                    raise RuntimeError(
                        "Claude Code returned an error result: success"
                    ) from None
            async for token in super().stream(message, **kwargs):
                yield token

    store = ConversationStore()
    sid = cast(str, store.create_session("owner-retry")["session_id"])
    async with mock_app(conversation_store=store) as f:
        f.app.state.agent = FlakyAgent(tokens=["recovered"])
        response = await f.client.post(
            "/chat",
            json={"message": "hi", "session_id": sid, "owner_id": "owner-retry"},
        )
        frames = _parse_sse(response)

    assert FlakyAgent.attempts == 2
    assert not [fr for fr in frames if fr["type"] == SSE_ERROR_TYPE]
    tokens = "".join(
        str(fr["content"]) for fr in frames if fr["type"] == SSE_TOKEN_TYPE
    )
    assert "recovered" in tokens


@pytest.mark.asyncio
async def test_idle_gap_never_compacts_any_session() -> None:
    """Idle compaction is gone: the trim scheduler is the ONE reduction path.

    A long-idle session with plenty of fresh turns must come back verbatim —
    the summary agent is never invoked and no compacted_summary appears
    (operator decision: one way to trim conversations across all sessions).
    """
    import time as time_mod

    store = ConversationStore()
    sid = cast(str, store.create_session("owner-idle")["session_id"])
    for i in range(5):
        store.record(sid, "owner-idle", f"q{i}", f"a{i}")

    session = store.get_session(sid)
    assert session is not None
    session.wall_last_active = time_mod.time() - 7200  # two hours idle

    summary_agent = MockAgent(tokens=["should never run"])
    async with mock_app(
        tokens=["ok"],
        summary_agent=summary_agent,
        conversation_store=store,
        idle_timeout_minutes=30,
    ) as f:
        response = await f.client.post(
            "/chat",
            json={"message": "back", "session_id": sid, "owner_id": "owner-idle"},
        )
        frames = _parse_sse(response)
        done = [fr for fr in frames if fr["type"] == SSE_DONE_TYPE][-1]

    assert done["session_id"] == sid
    assert summary_agent.call_count == 0
    assert store.get_session(sid).compacted_summary is None  # type: ignore[union-attr]
    # Agent view is the full verbatim history (5 old turns + the new one).
    assert [u for u, _ in store.agent_history(sid)] == [f"q{i}" for i in range(5)] + [
        "back"
    ]


@pytest.mark.asyncio
async def test_legacy_compacted_into_chain_still_reroutes() -> None:
    """Sessions compacted by the OLD design carry compacted_into pointers.

    Posting to such an id must land in the live end of the chain.
    """
    store = ConversationStore()
    old_sid = cast(str, store.create_session("owner-idle")["session_id"])
    live_sid = cast(str, store.create_session("owner-idle")["session_id"])
    store.get_session(old_sid).compacted_into = live_sid  # type: ignore[union-attr]

    async with mock_app(
        tokens=["ok"],
        conversation_store=store,
        idle_timeout_minutes=30,
    ) as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hi", "session_id": old_sid, "owner_id": "owner-idle"},
        )
        frames = _parse_sse(response)
        done = [fr for fr in frames if fr["type"] == SSE_DONE_TYPE][-1]

    assert done["session_id"] == live_sid
    assert [u for u, _ in store.history(live_sid)] == ["hi"]
    assert store.history(old_sid) == []


# ---------------------------------------------------------------------------
# Per-turn actions log — route wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_turn_persists_actions() -> None:
    """Actions recorded during a turn are stored on the session."""
    from robotsix_chat.chat.actions import record_action

    class ActingAgent(MockAgent):
        """A MockAgent that performs one tool call while replying."""

        async def stream(self, message: str, **kwargs: Any) -> AsyncIterator[str]:  # type: ignore[override]
            record_action("create_ticket", {"title": message}, {"ticket_id": "T-04d8"})
            async for token in super().stream(message, **kwargs):
                yield token

    store = ConversationStore()
    sid = cast(str, store.create_session("owner-act")["session_id"])
    async with mock_app(
        conversation_store=store,
        idle_timeout_minutes=30,
    ) as f:
        f.app.state.agent = ActingAgent(tokens=["Filed."])
        await f.client.post(
            "/chat",
            json={
                "message": "file the ticket",
                "session_id": sid,
                "owner_id": "owner-act",
            },
        )
        await f.client.post(
            "/chat",
            json={"message": "and again", "session_id": sid, "owner_id": "owner-act"},
        )

    actions = store.history_actions(sid)
    assert len(actions) == 2
    assert actions[0][0].startswith("create_ticket(")
    assert "T-04d8" in actions[0][0]


# ---------------------------------------------------------------------------
# Periodic failsafe PARTIAL REPORT detection (_periodic_ensure_partial_report)
# ---------------------------------------------------------------------------


def _periodic_def():
    from robotsix_chat.config.periodic_models import PeriodicSessionDefinition

    return PeriodicSessionDefinition(
        name="mail-triage",
        initial_prompt="Review the mail queue. READ-ONLY.",
        schedule_interval_seconds=3600,
    )


def _mk_periodic_app(store, tmp_path):
    return mock_app(
        conversation_store=store,
        periodic_definitions=[_periodic_def()],
        periodic_agent_factory=lambda level: MockAgent(tokens=["ok"]),
        periodic_state_path=str(tmp_path / "state.json"),
    )


@pytest.mark.asyncio
async def test_periodic_failsafe_appends_on_empty_transcript(tmp_path):
    """An existing session with no recorded turns → the failsafe appends a report."""
    store = ConversationStore()
    async with _mk_periodic_app(store, tmp_path) as f:
        sid = str(store.create_session("periodic")["session_id"])
        handler = f.app.state.periodic_scheduler._ensure_partial_report_on_interruption
        await handler(sid)
        turns = store.history(sid)
        assert len(turns) == 1
        assert turns[-1][1].startswith("PARTIAL REPORT")


@pytest.mark.asyncio
async def test_periodic_failsafe_appends_on_whitespace_final_reply(tmp_path):
    """A whitespace-only final assistant reply → the failsafe appends a report."""
    store = ConversationStore()
    async with _mk_periodic_app(store, tmp_path) as f:
        sid = str(store.create_session("periodic")["session_id"])
        store.record(sid, "periodic", "do work", "   \n  ")
        handler = f.app.state.periodic_scheduler._ensure_partial_report_on_interruption
        await handler(sid)
        turns = store.history(sid)
        assert len(turns) == 2
        assert turns[-1][1].startswith("PARTIAL REPORT")


@pytest.mark.asyncio
async def test_periodic_failsafe_skips_on_nonempty_final_reply(tmp_path):
    """A healthy run (non-empty final reply) → nothing appended."""
    store = ConversationStore()
    async with _mk_periodic_app(store, tmp_path) as f:
        sid = str(store.create_session("periodic")["session_id"])
        store.record(sid, "periodic", "do work", "DONE: 3 items processed")
        handler = f.app.state.periodic_scheduler._ensure_partial_report_on_interruption
        await handler(sid)
        turns = store.history(sid)
        assert len(turns) == 1  # unchanged — no failsafe report appended
        assert turns[-1][1] == "DONE: 3 items processed"


# ---------------------------------------------------------------------------
# Periodic pending-subsession fallback (_periodic_report_pending_subsessions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_periodic_report_pending_surfaces_open_user_chat(tmp_path):
    """Open user_chat panels are recorded when a crashed turn triggers the fallback."""
    store = ConversationStore()
    registry = SubsessionRegistry(store_path=None)
    async with mock_app(
        conversation_store=store,
        subsession_registry=registry,
        periodic_definitions=[_periodic_def()],
        periodic_agent_factory=lambda level: MockAgent(tokens=["ok"]),
        periodic_state_path=str(tmp_path / "state.json"),
    ) as f:
        sid = str(store.create_session("periodic")["session_id"])
        sub = _register_subsession(
            registry,
            owner=sid,
            kind=SubsessionKind.USER_CHAT,
            title="Approve deploy?",
        )
        handler = f.app.state.periodic_scheduler._report_pending_subsessions
        await handler(sid)
        turns = store.history(sid)
        assert len(turns) == 1
        recorded = turns[-1][1]
        assert "PENDING OPERATOR DECISIONS" in recorded
        assert sub.id in recorded
        assert "Approve deploy?" in recorded


@pytest.mark.asyncio
async def test_periodic_report_pending_noop_when_no_open_user_chat(tmp_path):
    """No open user_chat panels → nothing is recorded (transcript stays clean)."""
    store = ConversationStore()
    registry = SubsessionRegistry(store_path=None)
    async with mock_app(
        conversation_store=store,
        subsession_registry=registry,
        periodic_definitions=[_periodic_def()],
        periodic_agent_factory=lambda level: MockAgent(tokens=["ok"]),
        periodic_state_path=str(tmp_path / "state.json"),
    ) as f:
        sid = str(store.create_session("periodic")["session_id"])
        # A non-user_chat subsession must not be surfaced.
        _register_subsession(registry, owner=sid, kind=SubsessionKind.TASK)
        handler = f.app.state.periodic_scheduler._report_pending_subsessions
        await handler(sid)
        assert store.history(sid) == []
