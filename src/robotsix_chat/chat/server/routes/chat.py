"""Chat endpoint — accepts a chat message and streams the agent reply as SSE."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from robotsix_chat.chat.conversation import ConversationStore

from ._shared import _parse_json_body, _sse_frame
from .constants import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_HEARTBEAT_INTERVAL,
    SSE_TOKEN_TYPE,
    _SSE_HEARTBEAT_FRAME,
)

logger = logging.getLogger(__name__)


class ChatAgent(Protocol):
    """Structural interface for an agent that streams LLM responses.

    Any object whose ``stream(message)`` method returns an
    ``AsyncIterator[str]`` satisfies this protocol — no subclassing
    required.  (An ``async def`` generator method naturally returns an
    async iterator, so real implementations just write ``async def
    stream(self, message: str, *, history=None, session_id=None,
    client_id=None) -> AsyncIterator[str]:`` with ``yield``.)

    *history* (prior ``(user, assistant)`` turns), *session_id* (trace
    grouping), and *client_id* (owning browser) are optional keyword
    arguments the server supplies for multi-turn conversations and
    per-request delegation-tool scoping; an agent free to ignore them
    stays a stateless single query.
    """

    def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
    ) -> AsyncIterator[str]:
        """Yield tokens from the LLM in response to ``message``.

        *images* is an optional list of ``(media_type, raw_bytes)`` pairs
        representing attached images (e.g. ``[("image/png", b"...")]``).
        """
        ...


# ---------------------------------------------------------------------------
# Per-owner run serialization — prevents overlapping agent runs for one owner
# ---------------------------------------------------------------------------


class RunSerializer:
    """Per-owner ``asyncio.Lock`` registry to serialize agent runs.

    Process-local (single-worker server): locks are NOT distributed across
    processes.  In a multi-worker setup this provides best-effort isolation
    per worker, not cross-process mutual exclusion.

    Each owner (keyed by ``client_id`` / ``owner_id``) gets a dedicated
    ``asyncio.Lock``.  Acquire it around any agent run + store record
    sequence so that tick-triggered runs cannot race a user message or
    another tick for the same owner; runs queue and execute one at a time
    per owner.
    """

    def __init__(self) -> None:
        """Create an empty serializer with no locks."""
        self._locks: dict[str, asyncio.Lock] = {}

    def for_owner(self, owner_id: str) -> asyncio.Lock:
        """Return (creating if needed) the lock for *owner_id*."""
        lock = self._locks.get(owner_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[owner_id] = lock
        return lock

    def __repr__(self) -> str:
        """Return a concise representation showing the lock count."""
        return f"RunSerializer(locks={len(self._locks)})"


def _parse_and_validate_images(
    body: dict[str, Any],
    max_per_msg: int,
    max_bytes: int,
    allowed_types: list[str],
) -> tuple[list[tuple[str, bytes]] | None, JSONResponse | None]:
    """Parse and validate the ``images`` field from a chat request body.

    Returns ``(images, error_response)`` where exactly one is ``None``:
    - On success: ``(list_of_tuples, None)`` — each tuple is
      ``(media_type, raw_bytes)``.
    - On validation failure: ``(None, JSONResponse)`` — an HTTP 400 error
      ready to return to the client.

    When the body has no ``images`` key, returns ``(None, None)``.
    """
    raw_images = body.get("images")
    if raw_images is None:
        return None, None

    if not isinstance(raw_images, list):
        return None, JSONResponse(
            {"error": "'images' must be a JSON array"}, status_code=400
        )
    if len(raw_images) > max_per_msg:
        return None, JSONResponse(
            {
                "error": (
                    f"too many images: got {len(raw_images)}, maximum {max_per_msg}"
                )
            },
            status_code=400,
        )

    images: list[tuple[str, bytes]] = []
    for idx, img in enumerate(raw_images):
        if not isinstance(img, dict):
            return None, JSONResponse(
                {"error": f"images[{idx}]: expected a JSON object"},
                status_code=400,
            )
        media_type = img.get("media_type")
        if not isinstance(media_type, str) or not media_type:
            return None, JSONResponse(
                {"error": f"images[{idx}]: missing or invalid 'media_type'"},
                status_code=400,
            )
        if media_type not in allowed_types:
            return None, JSONResponse(
                {
                    "error": (
                        f"images[{idx}]: media_type {media_type!r} not "
                        f"allowed (allowed: {allowed_types})"
                    )
                },
                status_code=400,
            )
        data_b64 = img.get("data")
        if not isinstance(data_b64, str) or not data_b64:
            return None, JSONResponse(
                {"error": f"images[{idx}]: missing or invalid 'data'"},
                status_code=400,
            )
        try:
            raw_bytes = base64.b64decode(data_b64, validate=True)
        except Exception:
            return None, JSONResponse(
                {"error": f"images[{idx}]: 'data' is not valid base64"},
                status_code=400,
            )
        if len(raw_bytes) > max_bytes:
            return None, JSONResponse(
                {
                    "error": (
                        f"images[{idx}]: decoded size {len(raw_bytes)} "
                        f"exceeds maximum {max_bytes}"
                    )
                },
                status_code=400,
            )
        images.append((media_type, raw_bytes))

    return images, None


async def chat_endpoint(
    request: Request,
) -> JSONResponse | StreamingResponse:
    """Accept a chat message and stream the agent's response as SSE."""
    agent: ChatAgent = request.app.state.agent
    store: ConversationStore = request.app.state.conversation_store

    # -- parse & validate JSON body ---------------------------------------
    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body

    message = body.get("message")
    if message is not None and not isinstance(message, str):
        return JSONResponse(
            {"error": "message must be a string when present"}, status_code=400
        )

    # -- parse & validate message_id (optional) ---------------------------
    message_id = body.get("message_id")
    if message_id is not None and not isinstance(message_id, str):
        return JSONResponse({"error": "invalid 'message_id' field"}, status_code=400)
    if message_id is not None and len(message_id) > 128:
        return JSONResponse(
            {"error": "'message_id' exceeds maximum length"}, status_code=400
        )

    # -- parse & validate images (optional) -------------------------------
    images, err_resp = _parse_and_validate_images(
        body,
        max_per_msg=request.app.state.max_images_per_message,
        max_bytes=request.app.state.max_image_bytes,
        allowed_types=request.app.state.allowed_image_media_types,
    )
    if err_resp is not None:
        return err_resp

    # -- require at least one of message or images -----------------------
    if not message and not images:
        return JSONResponse(
            {"error": "either 'message' or at least one image is required"},
            status_code=400,
        )
    if not message:
        message = ""

    # Resolve session identity — accept session_id + owner_id (new) or
    # client_id (legacy fallback: client_id becomes both owner and session).
    session_id = body.get("session_id")
    owner_id = body.get("owner_id")
    client_id = body.get("client_id")

    if client_id is not None and not isinstance(client_id, str):
        return JSONResponse({"error": "invalid 'client_id' field"}, status_code=400)
    if session_id is not None and not isinstance(session_id, str):
        return JSONResponse({"error": "invalid 'session_id' field"}, status_code=400)
    if owner_id is not None and not isinstance(owner_id, str):
        return JSONResponse({"error": "invalid 'owner_id' field"}, status_code=400)

    # Backward compat: client_id alone → both owner and session.
    if not session_id and client_id:
        session_id = client_id
    if not owner_id and client_id:
        owner_id = client_id
    # If session_id is given without owner_id, derive owner_id from session.
    if not owner_id and session_id:
        owner_id = session_id
    # Derive client_id from session_id when not explicitly provided,
    # so delegation tools, EventBus, and check-loop routing still scope
    # correctly when the new session_id+owner_id fields are used alone.
    if not client_id and session_id:
        client_id = session_id

    had_session = bool(session_id)
    if not session_id:
        session_id = store.new_session_id()

    # -- SSE async generator ----------------------------------------------

    async def sse_stream() -> AsyncIterator[bytes]:
        # Drive the agent in a background task and forward its output through a
        # queue, so the response loop can interleave heartbeats while the agent
        # works (it yields its reply only at the end, after a long quiet spell).
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def _produce() -> None:
            # Serialize with any concurrent tick-triggered run for the same
            # owner (the lock is process-local; see RunSerializer docstring).
            run_serializer = request.app.state.run_serializer
            lock_key = client_id or session_id
            async with run_serializer.for_owner(lock_key):
                # Read history inside the lock so a new message always sees
                # the previous message's reply.  Only read when the session
                # pre-existed — a brand-new session has no history.
                _, current_history = (
                    store.begin(session_id) if had_session else (None, None)
                )

                # Idempotency check — replay completed reply if seen before.
                msg_id_store = request.app.state.msg_id_store
                if message_id and session_id:
                    existing_reply = msg_id_store.get_reply(session_id, message_id)
                    if existing_reply is not None:
                        # Replay completed reply as a single token + done;
                        # skip agent.
                        await queue.put((SSE_TOKEN_TYPE, existing_reply))
                        await queue.put((SSE_DONE_TYPE, None))
                        return

                reply_parts: list[str] = []
                try:
                    # NOTE: client_id here feeds the per-request tools factory
                    # (subsession tool scoping) — it must be the SESSION id,
                    # not the per-browser client id, so spawned subsessions and
                    # their SSE frames land in the owning chat session.
                    async for token in agent.stream(
                        message,
                        history=current_history,
                        session_id=session_id,
                        client_id=session_id,
                        images=images,
                    ):
                        reply_parts.append(token)
                        await queue.put((SSE_TOKEN_TYPE, token))
                    # Persist the completed exchange so the next message in
                    # this conversation sees it (only on a clean finish, not
                    # on error).
                    if session_id:
                        store.record(
                            session_id, owner_id, message, "".join(reply_parts)
                        )
                        # Record the completed reply for idempotency replay.
                        if message_id:
                            msg_id_store.mark_completed(
                                session_id, message_id, "".join(reply_parts)
                            )
                    await queue.put((SSE_DONE_TYPE, None))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Agent stream error")
                    await queue.put((SSE_ERROR_TYPE, str(exc)))

        producer = asyncio.create_task(_produce())
        try:
            yield _SSE_HEARTBEAT_FRAME  # first byte immediately
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), SSE_HEARTBEAT_INTERVAL
                    )
                except TimeoutError:
                    yield _SSE_HEARTBEAT_FRAME
                    continue
                if kind == SSE_TOKEN_TYPE:
                    yield _sse_frame({"type": SSE_TOKEN_TYPE, "content": payload})
                elif kind == SSE_DONE_TYPE:
                    yield _sse_frame({"type": SSE_DONE_TYPE})
                    break
                else:  # SSE_ERROR_TYPE
                    yield _sse_frame({"type": SSE_ERROR_TYPE, "message": payload})
                    break
        except asyncio.CancelledError:
            logger.debug("SSE stream cancelled (client disconnect)")
            raise
        finally:
            producer.cancel()
            with contextlib.suppress(BaseException):
                await producer

    return StreamingResponse(
        sse_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Content-Type": SSE_CONTENT_TYPE},
    )
