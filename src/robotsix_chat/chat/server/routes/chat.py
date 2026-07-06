"""Chat endpoint — accepts a chat message and streams the agent reply as SSE."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
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
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_INTERVAL,
    SSE_TOKEN_TYPE,
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


# ---------------------------------------------------------------------------
# Per-session message coalescing — batches rapid-fire user messages
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class _PendingMessage:
    """A single user message waiting to be batched."""

    message: str
    images: list[tuple[str, bytes]] | None
    message_id: str | None
    response_queue: asyncio.Queue[tuple[str, str | None]]


class MessageCoalescer:
    """Coalesce rapid-fire user messages into a single agent run per session.

    When multiple ``POST /chat`` requests arrive for the same session in
    quick succession (within *debounce_seconds*), the coalescer batches
    their messages together and runs the agent once with the concatenated
    text.  Each waiting client receives the same streamed response.

    Process-local (single-worker server): batching is NOT distributed across
    processes.  In a multi-worker setup each worker coalesces independently
    for the requests it receives.
    """

    # Separator inserted between concatenated messages.
    MESSAGE_SEPARATOR: str = "\n\n---\n\n"

    def __init__(self, *, debounce_seconds: float = 0.3) -> None:
        """*debounce_seconds* — window to wait for additional messages."""
        self._debounce_seconds = debounce_seconds
        self._batches: dict[str, list[_PendingMessage]] = {}
        # Guard protects the _batches dict — not the individual lists,
        # which are only accessed by their dedicated processor task after
        # the guard releases.
        self._guard: asyncio.Lock = asyncio.Lock()

    async def submit(
        self,
        session_id: str,
        message: str,
        images: list[tuple[str, bytes]] | None,
        message_id: str | None,
        *,
        agent: ChatAgent,
        store: ConversationStore,
        run_serializer: RunSerializer,
        msg_id_store: Any,  # MessageIdempotencyStore (lazy import to avoid circular)
        lock_key: str,
        owner_id: str,
        had_session: bool,
    ) -> asyncio.Queue[tuple[str, str | None]]:
        """Submit a message for batching; return a queue of SSE frames.

        The caller reads ``(type, payload)`` tuples from the returned
        queue and streams them as SSE frames.  The queue receives
        ``SSE_DONE_TYPE`` at completion or ``SSE_ERROR_TYPE`` on failure.
        """
        response_queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()
        pending = _PendingMessage(message, images, message_id, response_queue)

        async with self._guard:
            batch = self._batches.get(session_id)
            if batch is None:
                batch = []
                self._batches[session_id] = batch
            batch.append(pending)

            # Only start a processor when the first message lands in an
            # empty batch.
            if len(batch) == 1:
                asyncio.create_task(
                    self._process_batch(
                        session_id,
                        agent,
                        store,
                        run_serializer,
                        msg_id_store,
                        lock_key,
                        owner_id,
                        had_session,
                    )
                )

        return response_queue

    async def _process_batch(
        self,
        session_id: str,
        agent: ChatAgent,
        store: ConversationStore,
        run_serializer: RunSerializer,
        msg_id_store: Any,
        lock_key: str,
        owner_id: str,
        had_session: bool,
    ) -> None:
        """Wait for the debounce window, drain, lock, run agent, fan out."""
        await asyncio.sleep(self._debounce_seconds)

        # Atomically drain the batch — messages arriving after this point
        # will start a fresh batch (next submit call creates a new
        # processor).
        async with self._guard:
            pending = self._batches.pop(session_id, [])

        if not pending:
            return

        # Concatenate messages in arrival order.
        messages = [p.message for p in pending if p.message]
        if len(messages) > 1:
            concatenated = self.MESSAGE_SEPARATOR.join(messages)
        elif messages:
            concatenated = messages[0]
        else:
            concatenated = ""

        # Combine images from all batched messages.
        all_images: list[tuple[str, bytes]] = []
        for p in pending:
            if p.images:
                all_images.extend(p.images)
        combined_images = all_images or None

        # Acquire the per-owner lock, read history, and run the agent.
        async with run_serializer.for_owner(lock_key):
            _, current_history = (
                store.begin(session_id) if had_session else (None, None)
            )

            # Idempotency check on the first pending message's message_id.
            first_msg = pending[0]
            if first_msg.message_id and session_id:
                existing = msg_id_store.get_reply(session_id, first_msg.message_id)
                if existing is not None:
                    for p in pending:
                        await p.response_queue.put((SSE_TOKEN_TYPE, existing))
                        await p.response_queue.put((SSE_DONE_TYPE, None))
                    return

            reply_parts: list[str] = []
            try:
                async for token in agent.stream(
                    concatenated,
                    history=current_history,
                    session_id=session_id,
                    client_id=session_id,
                    images=combined_images,
                ):
                    reply_parts.append(token)
                    for p in pending:
                        await p.response_queue.put((SSE_TOKEN_TYPE, token))

                full_reply = "".join(reply_parts)
                if session_id:
                    store.record(session_id, owner_id, concatenated, full_reply)
                    for p in pending:
                        if p.message_id:
                            msg_id_store.mark_completed(
                                session_id, p.message_id, full_reply
                            )

                for p in pending:
                    await p.response_queue.put((SSE_DONE_TYPE, None))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Agent stream error")
                for p in pending:
                    await p.response_queue.put((SSE_ERROR_TYPE, str(exc)))


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

    lock_key = client_id or session_id

    # -- Submit to the message coalescer ----------------------------------

    coalescer: MessageCoalescer = request.app.state.message_coalescer
    response_queue = await coalescer.submit(
        session_id,
        message,
        images,
        message_id,
        agent=agent,
        store=store,
        run_serializer=request.app.state.run_serializer,
        msg_id_store=request.app.state.msg_id_store,
        lock_key=lock_key,
        owner_id=owner_id or "",
        had_session=had_session,
    )

    # -- SSE async generator ----------------------------------------------

    async def sse_stream() -> AsyncIterator[bytes]:
        finished_normally = False
        try:
            yield SSE_HEARTBEAT_FRAME  # first byte immediately
            while True:
                try:
                    kind, payload = await asyncio.wait_for(
                        response_queue.get(), SSE_HEARTBEAT_INTERVAL
                    )
                except TimeoutError:
                    yield SSE_HEARTBEAT_FRAME
                    continue
                if kind == SSE_TOKEN_TYPE:
                    yield _sse_frame({"type": SSE_TOKEN_TYPE, "content": payload})
                elif kind == SSE_DONE_TYPE:
                    yield _sse_frame({"type": SSE_DONE_TYPE})
                    finished_normally = True
                    break
                else:  # SSE_ERROR_TYPE
                    yield _sse_frame({"type": SSE_ERROR_TYPE, "message": payload})
                    finished_normally = True
                    break
        except asyncio.CancelledError:
            logger.debug("SSE stream cancelled (client disconnect)")
        finally:
            # On client disconnect the DONE/ERROR frame hasn't been
            # consumed yet — drain the response queue so the background
            # coalescer task can complete and persist the reply (matches
            # the old ``await producer`` guarantee).
            if not finished_normally:
                with contextlib.suppress(Exception):
                    while True:
                        kind, _ = await response_queue.get()
                        if kind in (SSE_DONE_TYPE, SSE_ERROR_TYPE):
                            break

    return StreamingResponse(
        sse_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Content-Type": SSE_CONTENT_TYPE},
    )
