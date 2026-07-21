"""Unit tests for the ``chat_endpoint`` route handler."""

from __future__ import annotations

import base64
import json

import pytest

from robotsix_chat.chat.server import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
)
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_sse(response_text: str) -> list[dict[str, object]]:
    """Split SSE text into parsed JSON frames from ``data:`` lines."""
    events = [e for e in response_text.split("\n\n") if e]
    frames: list[dict[str, object]] = []
    for e in events:
        if e.startswith("data: "):
            frames.append(json.loads(e[len("data: ") :]))
    return frames


def _png_pixel() -> bytes:
    """Smallest valid PNG — 1×1 red pixel."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5"
        "+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    )


# ---------------------------------------------------------------------------
# Streaming response structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_returns_sse_content_type() -> None:
    """The response has ``text/event-stream`` content type."""
    async with mock_app(tokens=["ok"]) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    assert response.headers["content-type"] == SSE_CONTENT_TYPE


@pytest.mark.asyncio
async def test_chat_endpoint_streams_token_frames() -> None:
    """Each token yielded by the agent becomes an SSE data frame."""
    async with mock_app(tokens=["Hello", " ", "world!"]) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    frames = _parse_sse(response.text)

    token_frames = [f for f in frames if f.get("type") == SSE_TOKEN_TYPE]
    assert len(token_frames) == 3
    assert token_frames[0] == {"type": SSE_TOKEN_TYPE, "content": "Hello"}
    assert token_frames[1] == {"type": SSE_TOKEN_TYPE, "content": " "}
    assert token_frames[2] == {"type": SSE_TOKEN_TYPE, "content": "world!"}


@pytest.mark.asyncio
async def test_chat_endpoint_ends_with_done_frame() -> None:
    """The final SSE data frame has ``type: done`` with a ``session_id``."""
    async with mock_app(tokens=["done"]) as f:
        response = await f.client.post("/chat", json={"message": "hi"})

    assert response.status_code == 200
    frames = _parse_sse(response.text)

    done_frames = [f for f in frames if f.get("type") == SSE_DONE_TYPE]
    assert len(done_frames) == 1
    done = done_frames[0]
    assert isinstance(done["session_id"], str)
    assert done["session_id"]
    assert isinstance(done["timestamp"], (int, float))


@pytest.mark.asyncio
async def test_chat_endpoint_opens_with_heartbeat() -> None:
    """The stream text starts with a ``: keepalive`` heartbeat comment."""
    async with mock_app(tokens=["ok"]) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    # The response text should contain the heartbeat comment before data frames
    assert ": keepalive" in response.text


# ---------------------------------------------------------------------------
# Error handling — missing / invalid message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_missing_message_and_images_returns_400() -> None:
    """``400`` when neither ``message`` nor ``images`` is provided."""
    async with mock_app() as f:
        response = await f.client.post("/chat", json={})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_message_not_string_returns_400() -> None:
    """``400`` when ``message`` is present but not a string."""
    async with mock_app() as f:
        response = await f.client.post("/chat", json={"message": 42})

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_invalid_json_body_returns_400() -> None:
    """``400`` when the request body is not valid JSON."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


# ---------------------------------------------------------------------------
# message_id validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_message_id_non_string_returns_400() -> None:
    """``400`` when ``message_id`` is present but not a string."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hello", "message_id": 123},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_message_id_exceeds_max_length_returns_400() -> None:
    """``400`` when ``message_id`` exceeds 128 characters."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hello", "message_id": "x" * 129},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_valid_message_id_accepted() -> None:
    """A ``message_id`` within the 128-char limit is accepted."""
    async with mock_app(tokens=["ok"]) as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hello", "message_id": "valid-id-123"},
        )

    assert response.status_code == 200
    frames = _parse_sse(response.text)
    assert any(f.get("type") == SSE_DONE_TYPE for f in frames)


# ---------------------------------------------------------------------------
# session_id / owner_id / client_id type validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_session_id_non_string_returns_400() -> None:
    """``400`` when ``session_id`` is not a string."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hello", "session_id": 123},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_owner_id_non_string_returns_400() -> None:
    """``400`` when ``owner_id`` is not a string."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hello", "owner_id": 456},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_client_id_non_string_returns_400() -> None:
    """``400`` when ``client_id`` is not a string."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"message": "hello", "client_id": [1, 2, 3]},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


# ---------------------------------------------------------------------------
# Image validation — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_images_not_a_list_returns_400() -> None:
    """``400`` when ``images`` is not a JSON array."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"images": "not-a-list"},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_too_many_images_returns_400() -> None:
    """``400`` when ``images`` exceeds ``max_images_per_message`` (default 8)."""
    async with mock_app() as f:
        images = [{"media_type": "image/png", "data": "AAAA"} for _ in range(9)]
        response = await f.client.post(
            "/chat",
            json={"images": images},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_image_entry_not_dict_returns_400() -> None:
    """``400`` when an image entry is not a JSON object."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"images": ["not-an-object"]},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_image_missing_media_type_returns_400() -> None:
    """``400`` when ``media_type`` is absent from an image entry."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"images": [{"data": "AAAA"}]},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_image_invalid_media_type_returns_400() -> None:
    """``400`` when ``media_type`` is not in the allowed list."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={
                "images": [{"media_type": "image/tiff", "data": "AAAA"}],
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_image_missing_data_returns_400() -> None:
    """``400`` when the ``data`` field is absent from an image entry."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={"images": [{"media_type": "image/png"}]},
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_image_non_base64_data_returns_400() -> None:
    """``400`` when ``data`` is not valid base64."""
    async with mock_app() as f:
        response = await f.client.post(
            "/chat",
            json={
                "images": [
                    {"media_type": "image/png", "data": "!!!not-valid-base64!!!"}
                ],
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


@pytest.mark.asyncio
async def test_chat_endpoint_oversized_image_returns_400() -> None:
    """``400`` when a decoded image exceeds ``max_image_bytes`` (default ~5 MB)."""
    async with mock_app() as f:
        # Create a valid base64 string whose decoded size exceeds the limit
        huge_b64 = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()
        response = await f.client.post(
            "/chat",
            json={
                "images": [{"media_type": "image/png", "data": huge_b64}],
            },
        )

    assert response.status_code == 400
    data = response.json()
    assert "error" in data


# ---------------------------------------------------------------------------
# Agent error → SSE error frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_agent_error_sends_sse_error_frame() -> None:
    """When the agent raises, an SSE error frame is delivered (no done frame)."""
    async with mock_app(error=RuntimeError("LLM failure")) as f:
        response = await f.client.post("/chat", json={"message": "hello"})

    assert response.status_code == 200
    frames = _parse_sse(response.text)

    error_frames = [f for f in frames if f.get("type") == SSE_ERROR_TYPE]
    assert len(error_frames) == 1
    assert error_frames[0]["message"] == "LLM failure"

    done_frames = [f for f in frames if f.get("type") == SSE_DONE_TYPE]
    assert len(done_frames) == 0
