"""Integration tests for the chat SSE endpoint.

Exercises the server-side behaviour that the browser UI depends on.

Covers SSE frame parsing expectations, session lifecycle, error reporting,
image validation, idempotency, cancel-queued, and UI-related endpoint
behaviours.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, cast

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
    create_app,
)
from tests.conftest import MockAgent, http_client, mock_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse_frames(response: Any) -> list[dict[str, object]]:
    """Split SSE text into parsed JSON frames from ``data:`` lines."""
    text: str = response.text
    frames: list[dict[str, object]] = []
    for event in text.split("\n\n"):
        if event.startswith("data: "):
            frames.append(json.loads(event[len("data: ") :]))
    return frames


def _token_frames(frames: list[dict[str, object]]) -> list[str]:
    """Extract token content strings from a list of SSE frames."""
    return [
        cast(str, f["content"])
        for f in frames
        if f["type"] == SSE_TOKEN_TYPE
    ]


def _last_frame_of_type(
    frames: list[dict[str, object]], type_: str
) -> dict[str, object] | None:
    """Return the last frame matching *type_* or None."""
    for f in reversed(frames):
        if f["type"] == type_:
            return f
    return None


# ---------------------------------------------------------------------------
# SSE protocol — frame format and ordering
# ---------------------------------------------------------------------------


class TestSseProtocol:
    """The SSE stream follows the expected frame protocol."""

    @pytest.mark.asyncio
    async def test_stream_starts_with_heartbeat(self) -> None:
        """Stream opens with a heartbeat comment before any data frames."""
        async with mock_app(tokens=["hi"]) as f:
            response = await f.client.post("/chat", json={"message": "hello"})

        assert response.status_code == 200
        assert response.text.startswith(": keepalive")

    @pytest.mark.asyncio
    async def test_token_frames_have_correct_shape(self) -> None:
        """Each token frame is ``{"type": "token", "content": "..."}``."""
        async with mock_app(tokens=["A", "B", "C"]) as f:
            response = await f.client.post("/chat", json={"message": "x"})

        frames = _parse_sse_frames(response)
        tokens = [f for f in frames if f["type"] == SSE_TOKEN_TYPE]
        assert len(tokens) == 3
        for t in tokens:
            assert set(t.keys()) == {"type", "content"}
            assert isinstance(t["content"], str)

    @pytest.mark.asyncio
    async def test_done_frame_has_session_id(self) -> None:
        """The ``done`` frame carries the session_id for client adoption."""
        async with mock_app(tokens=["ok"]) as f:
            response = await f.client.post(
                "/chat",
                json={"message": "hi", "session_id": "my-session", "owner_id": "me"},
            )

        frames = _parse_sse_frames(response)
        done = _last_frame_of_type(frames, SSE_DONE_TYPE)
        assert done is not None
        assert done["session_id"] == "my-session"

    @pytest.mark.asyncio
    async def test_error_frame_has_message(self) -> None:
        """The ``error`` frame carries a human-readable message."""
        async with mock_app(error=RuntimeError("something broke")) as f:
            response = await f.client.post("/chat", json={"message": "x"})

        frames = _parse_sse_frames(response)
        err = _last_frame_of_type(frames, SSE_ERROR_TYPE)
        assert err is not None
        assert "message" in err

    @pytest.mark.asyncio
    async def test_content_type_is_event_stream(self) -> None:
        """Response content-type is ``text/event-stream``."""
        async with mock_app(tokens=["ok"]) as f:
            response = await f.client.post("/chat", json={"message": "x"})

        assert response.headers["content-type"] == SSE_CONTENT_TYPE

    @pytest.mark.asyncio
    async def test_every_stream_ends_with_done_or_error(self) -> None:
        """Every SSE stream terminates with either ``done`` or ``error``."""
        async with mock_app(tokens=["ok"]) as f:
            response = await f.client.post("/chat", json={"message": "x"})

        frames = _parse_sse_frames(response)
        last = frames[-1]["type"]
        assert last in (SSE_DONE_TYPE, SSE_ERROR_TYPE)

    @pytest.mark.asyncio
    async def test_done_is_always_last_data_frame(self) -> None:
        """The last ``data:`` frame in a successful stream is always ``done``."""
        async with mock_app(tokens=["a", "b"]) as f:
            response = await f.client.post("/chat", json={"message": "x"})

        frames = _parse_sse_frames(response)
        assert frames[-1]["type"] == SSE_DONE_TYPE

    @pytest.mark.asyncio
    async def test_error_stream_contains_no_done(self) -> None:
        """An error stream never emits a ``done`` frame."""
        async with mock_app(error=RuntimeError("fail")) as f:
            response = await f.client.post("/chat", json={"message": "x"})

        frames = _parse_sse_frames(response)
        assert not any(f["type"] == SSE_DONE_TYPE for f in frames)
        assert any(f["type"] == SSE_ERROR_TYPE for f in frames)


# ---------------------------------------------------------------------------
# Session lifecycle — create, switch, list, delete
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """Session CRUD operations that the browser UI relies on."""

    @pytest.mark.asyncio
    async def test_create_session_returns_session_id(self) -> None:
        """``POST /sessions`` returns a new session dict."""
        async with mock_app() as f:
            response = await f.client.post(
                "/sessions", json={"owner_id": "owner-1"}
            )

        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data
        assert data["title"] == "New chat"
        assert data["turn_count"] == 0

    @pytest.mark.asyncio
    async def test_list_sessions_returns_array(self) -> None:
        """``GET /sessions?owner_id=X`` returns a sessions array."""
        store = ConversationStore()
        store.create_session("owner-2")

        async with mock_app(conversation_store=store) as f:
            response = await f.client.get("/sessions?owner_id=owner-2")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data["sessions"], list)
        assert "active_session_id" in data

    @pytest.mark.asyncio
    async def test_list_sessions_includes_title_and_turn_count(self) -> None:
        """Each session entry carries title and turn_count the UI renders."""
        store = ConversationStore()
        sid = cast(str, store.create_session("owner-3")["session_id"])
        store.record(sid, "owner-3", "hello", "hi")

        async with mock_app(conversation_store=store) as f:
            response = await f.client.get("/sessions?owner_id=owner-3")

        data = response.json()
        s = data["sessions"][0]
        assert "title" in s
        assert "turn_count" in s
        assert "last_active" in s
        assert s["turn_count"] == 1

    @pytest.mark.asyncio
    async def test_list_sessions_sorted_by_last_active_desc(self) -> None:
        """Sessions are sorted newest-first."""
        store = ConversationStore()
        sid1 = cast(str, store.create_session("owner-4")["session_id"])
        sid2 = cast(str, store.create_session("owner-4")["session_id"])

        async with mock_app(conversation_store=store) as f:
            response = await f.client.get("/sessions?owner_id=owner-4")

        data = response.json()
        sessions = data["sessions"]
        assert sessions[0]["session_id"] == sid2  # newer first
        assert sessions[1]["session_id"] == sid1

    @pytest.mark.asyncio
    async def test_delete_session_returns_deleted_true(self) -> None:
        """``DELETE /sessions/{id}?owner_id=X`` confirms deletion."""
        store = ConversationStore()
        sid = cast(str, store.create_session("owner-5")["session_id"])

        async with mock_app(conversation_store=store) as f:
            response = await f.client.delete(f"/sessions/{sid}?owner_id=owner-5")

        assert response.status_code == 200
        assert response.json()["deleted"] is True

    @pytest.mark.asyncio
    async def test_delete_session_returns_active_session_id(self) -> None:
        """Deleting one session returns the owner's new active session."""
        store = ConversationStore()
        sid_a = cast(str, store.create_session("owner-6")["session_id"])
        sid_b = cast(str, store.create_session("owner-6")["session_id"])

        async with mock_app(conversation_store=store) as f:
            response = await f.client.delete(f"/sessions/{sid_b}?owner_id=owner-6")

        data = response.json()
        assert data["active_session_id"] == sid_a


# ---------------------------------------------------------------------------
# Error handling — server responses the UI must display
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Validation errors the UI shows to the user."""

    @pytest.mark.asyncio
    async def test_missing_message_and_images_returns_400(self) -> None:
        """``POST /chat`` with no message and no images returns 400."""
        async with mock_app() as f:
            response = await f.client.post("/chat", json={})

        assert response.status_code == 400
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_message_not_string_returns_400(self) -> None:
        """Non-string message is rejected."""
        async with mock_app() as f:
            response = await f.client.post("/chat", json={"message": 42})

        assert response.status_code == 400
        assert "error" in response.json()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        """Malformed JSON body returns 400."""
        async with mock_app() as f:
            response = await f.client.post(
                "/chat",
                content=b"not-json",
                headers={"Content-Type": "application/json"},
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_empty_message_string_returns_400(self) -> None:
        """An empty string message with no images returns 400."""
        async with mock_app() as f:
            response = await f.client.post("/chat", json={"message": ""})

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_agent_exception_produces_sse_error_frame(self) -> None:
        """Agent raising produces an SSE error frame, not an HTTP 500."""
        async with mock_app(error=RuntimeError("crash")) as f:
            response = await f.client.post("/chat", json={"message": "hi"})

        # The POST itself succeeds (200), but the SSE stream contains error.
        assert response.status_code == 200
        frames = _parse_sse_frames(response)
        assert any(f["type"] == SSE_ERROR_TYPE for f in frames)


# ---------------------------------------------------------------------------
# Image attachment validation
# ---------------------------------------------------------------------------


class TestImageValidation:
    """Server-side validation of image attachments the UI sends."""

    @pytest.mark.asyncio
    async def test_valid_image_accepted(self) -> None:
        """A valid base64-encoded PNG passes validation."""
        # smallest valid PNG (1x1 pixel, red)
        png_bytes = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
        )
        b64 = base64.b64encode(png_bytes).decode()

        async with mock_app(tokens=["ok"]) as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "look at this",
                    "images": [{"media_type": "image/png", "data": b64}],
                },
            )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_invalid_media_type_rejected(self) -> None:
        """Unsupported media type returns 400."""
        async with mock_app() as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "x",
                    "images": [{"media_type": "image/svg+xml", "data": "AAAA"}],
                },
            )

        assert response.status_code == 400
        assert "not allowed" in response.json()["error"]

    @pytest.mark.asyncio
    async def test_invalid_base64_rejected(self) -> None:
        """Non-base64 image data returns 400."""
        async with mock_app() as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "x",
                    "images": [{"media_type": "image/png", "data": "!!!not-base64!!!"}],
                },
            )

        assert response.status_code == 400
        assert "base64" in response.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_images_must_be_array(self) -> None:
        """``images`` must be a JSON array, not an object or string."""
        async with mock_app() as f:
            response = await f.client.post(
                "/chat",
                json={"message": "x", "images": "not-an-array"},
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_too_many_images_rejected(self) -> None:
        """Exceeding max_images_per_message returns 400."""
        async with mock_app(max_images_per_message=1) as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "x",
                    "images": [
                        {"media_type": "image/png", "data": "AAAA"},
                        {"media_type": "image/png", "data": "AAAA"},
                    ],
                },
            )

        assert response.status_code == 400
        assert "too many images" in response.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_image_missing_media_type_rejected(self) -> None:
        """Image dict missing ``media_type`` returns 400."""
        async with mock_app() as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "x",
                    "images": [{"data": "AAAA"}],
                },
            )

        assert response.status_code == 400
        assert "media_type" in response.json()["error"].lower()

    @pytest.mark.asyncio
    async def test_image_missing_data_rejected(self) -> None:
        """Image dict missing ``data`` returns 400."""
        async with mock_app() as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "x",
                    "images": [{"media_type": "image/png"}],
                },
            )

        assert response.status_code == 400
        assert "data" in response.json()["error"].lower()


# ---------------------------------------------------------------------------
# Idempotency — duplicate message_id returns cached reply
# ---------------------------------------------------------------------------


class TestMessageIdempotency:
    """Duplicate POST /chat with the same message_id returns the cached reply."""

    @pytest.mark.asyncio
    async def test_duplicate_message_id_returns_cached_reply(self) -> None:
        """Second POST with same message_id returns the first reply."""
        from robotsix_chat.chat.server.idempotency import MessageIdempotencyStore

        store = ConversationStore()
        msg_id_store = MessageIdempotencyStore()

        async with mock_app(
            tokens=["first reply"],
            conversation_store=store,
            msg_id_store=msg_id_store,
        ) as f:
            r1 = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "dup-1",
                    "session_id": "sid-1",
                    "owner_id": "o-1",
                },
            )
            frames1 = _parse_sse_frames(r1)
            assert _token_frames(frames1) == ["first reply"]

        # Reuse the same msg_id_store so the idempotency cache is shared.
        async with mock_app(
            tokens=["SHOULD NOT APPEAR"],
            conversation_store=store,
            msg_id_store=msg_id_store,
        ) as f:
            r2 = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "dup-1",
                    "session_id": "sid-1",
                    "owner_id": "o-1",
                },
            )
            frames2 = _parse_sse_frames(r2)
            # Should get the cached reply, not the new token.
            assert _token_frames(frames2) == ["first reply"]
            # Agent was NOT called (reply came from cache).
            assert f.agent.call_count == 0

    @pytest.mark.asyncio
    async def test_different_message_id_runs_agent(self) -> None:
        """Different message_id triggers a fresh agent run."""
        store = ConversationStore()

        async with mock_app(tokens=["reply 1"], conversation_store=store) as f:
            await f.client.post(
                "/chat",
                json={
                    "message": "msg1",
                    "message_id": "id-1",
                    "session_id": "sid-2",
                    "owner_id": "o-2",
                },
            )

        async with mock_app(tokens=["reply 2"], conversation_store=store) as f:
            await f.client.post(
                "/chat",
                json={
                    "message": "msg2",
                    "message_id": "id-2",
                    "session_id": "sid-2",
                    "owner_id": "o-2",
                },
            )

        assert f.agent.call_count == 1
        assert f.agent.called_with == "msg2"
        assert f.agent.history == [("msg1", "reply 1")]


# ---------------------------------------------------------------------------
# Cancel-queued endpoint
# ---------------------------------------------------------------------------


class TestCancelQueued:
    """``POST /chat/queue/cancel`` cancels pending (not-yet-processing) messages."""

    @pytest.mark.asyncio
    async def test_cancel_queued_missing_session_id_returns_400(self) -> None:
        """Missing ``session_id`` returns 400."""
        async with mock_app() as f:
            response = await f.client.post(
                "/chat/queue/cancel",
                json={},
            )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_cancel_queued_returns_cancelled_count(self) -> None:
        """A successful bulk cancel returns ``{"cancelled": N}``."""
        async with mock_app(
            tokens=["ok"], message_coalesce_seconds=5.0  # long debounce
        ) as f:
            session_id = "sq-cancel"
            owner_id = "o-cancel"

            # Fire a message and immediately cancel before the debounce fires.
            task = asyncio.create_task(
                f.client.post(
                    "/chat",
                    json={
                        "message": "will be cancelled",
                        "session_id": session_id,
                        "owner_id": owner_id,
                    },
                )
            )
            # Give it a moment to land in the coalescer.
            await asyncio.sleep(0.05)

            cancel_r = await f.client.post(
                "/chat/queue/cancel",
                json={"session_id": session_id},
            )

            cancel_data = cancel_r.json()
            assert cancel_data["cancelled"] >= 1

            # The original request should still complete (cancelled messages
            # get a done frame).
            response = await task
            assert response.status_code == 200


# ---------------------------------------------------------------------------
# History endpoint — UI bootstrapping
# ---------------------------------------------------------------------------


class TestHistoryForUI:
    """``GET /history`` provides turn data for the UI's ``loadHistory``."""

    @pytest.mark.asyncio
    async def test_history_returns_empty_for_new_session(self) -> None:
        """Unknown session returns empty turns list."""
        async with mock_app() as f:
            response = await f.client.get("/history?session_id=unknown")

        assert response.status_code == 200
        assert response.json() == {"turns": []}

    @pytest.mark.asyncio
    async def test_history_returns_user_assistant_pairs(self) -> None:
        """History returns ``[[user, assistant], ...]`` pairs."""
        store = ConversationStore()
        sid = cast(str, store.create_session("o-hist")["session_id"])
        store.record(sid, "o-hist", "question", "answer")

        async with mock_app(conversation_store=store) as f:
            response = await f.client.get(f"/history?session_id={sid}")

        assert response.json() == {"turns": [["question", "answer"]]}

    @pytest.mark.asyncio
    async def test_history_preserves_order(self) -> None:
        """Turns are returned in chronological order."""
        store = ConversationStore()
        sid = cast(str, store.create_session("o-hist2")["session_id"])
        store.record(sid, "o-hist2", "first", "1")
        store.record(sid, "o-hist2", "second", "2")

        async with mock_app(conversation_store=store) as f:
            response = await f.client.get(f"/history?session_id={sid}")

        assert response.json() == {"turns": [["first", "1"], ["second", "2"]]}


# ---------------------------------------------------------------------------
# UI endpoint — GET /
# ---------------------------------------------------------------------------


class TestUIEndpoint:
    """``GET /`` serves the rendered HTML."""

    @pytest.mark.asyncio
    async def test_ui_endpoint_returns_html(self) -> None:
        """``GET /`` returns 200 with HTML content-type."""
        async with mock_app() as f:
            response = await f.client.get("/")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_ui_endpoint_contains_chat_div(self) -> None:
        """Served HTML contains the ``#chat`` element."""
        async with mock_app() as f:
            response = await f.client.get("/")

        assert 'id="chat"' in response.text

    @pytest.mark.asyncio
    async def test_ui_endpoint_contains_meta_timeout(self) -> None:
        """Served HTML contains the ``idle-timeout-minutes`` meta tag."""
        async with mock_app(idle_timeout_minutes=15) as f:
            response = await f.client.get("/")

        assert 'content="15"' in response.text
        assert 'name="idle-timeout-minutes"' in response.text

    @pytest.mark.asyncio
    async def test_ui_not_served_when_serve_ui_is_false(self) -> None:
        """When ``serve_ui=False``, ``GET /`` returns 404."""
        agent = MockAgent(tokens=["ok"])
        app = create_app(agent, serve_ui=False)

        async with http_client(app) as client:
            response = await client.get("/")

        assert response.status_code == 404
