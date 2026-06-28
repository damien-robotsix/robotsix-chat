"""Chat SSE server — route handlers, SSE helpers, and request-processing types.

Every HTTP endpoint, SSE wire-format constant, helper, and protocol class
that used to live in the monolithic ``server.py`` lives here.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.pending_questions.store import (
    PendingQuestion,
    PendingQuestionsStore,
    ThreadMessage,
)

if TYPE_CHECKING:
    from robotsix_chat.chat.loops import CheckLoopRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE wire-format constants — single source of truth for tests and consumers.
# ---------------------------------------------------------------------------

SSE_CONTENT_TYPE = "text/event-stream"
SSE_TOKEN_TYPE = "token"
SSE_DONE_TYPE = "done"
SSE_ERROR_TYPE = "error"

# The agent returns its reply as a single block only once the whole pipeline
# (memory recall, the LLM, any tool calls) completes — which can be many seconds
# with no bytes on the wire. A silent connection that long gets dropped by the
# browser/proxy ("NetworkError"). Emit an SSE *comment* heartbeat immediately and
# on this interval so the data channel never goes quiet. Comments carry no
# ``data:`` line, so clients ignore them.
SSE_HEARTBEAT_INTERVAL = 5.0
_SSE_HEARTBEAT_FRAME = b": keepalive\n\n"


def _sse_frame(payload: object) -> bytes:
    """Return an SSE ``data:`` frame with a JSON-serialised *payload*."""
    return f"data: {json.dumps(payload)}\n\n".encode()


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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


async def health_endpoint(request: Request) -> JSONResponse:
    """Liveness probe — returns 200 ``{"status": "ok"}``."""
    return JSONResponse({"status": "ok"})


async def ui_endpoint(request: Request) -> HTMLResponse:
    """Serve the self-contained browser chat UI at ``GET /``."""
    from . import _load_ui_html  # lazy import for patchability

    timeout = request.app.state.idle_timeout_minutes
    return HTMLResponse(_load_ui_html(timeout))


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
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

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
                    async for token in agent.stream(
                        message,
                        history=current_history,
                        session_id=session_id,
                        client_id=client_id,
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


async def history_endpoint(request: Request) -> JSONResponse:
    """Return a session's stored conversation history as JSON.

    ``GET /history?session_id=...`` returns ``{"turns": [[user, assistant], ...]}``.
    Also tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        # Legacy fallback: treat client_id as session_id.
        session_id = request.query_params.get("client_id")
    if not session_id:
        return JSONResponse(
            {"error": "session_id query parameter is required"}, status_code=400
        )

    store: ConversationStore = request.app.state.conversation_store
    turns = store.history(session_id)
    return JSONResponse({"turns": turns})


async def events_endpoint(request: Request) -> JSONResponse | StreamingResponse:
    """Open a persistent SSE channel for background-task lifecycle events.

    ``GET /events?session_id=...`` opens a never-closing ``text/event-stream``
    that delivers ``task_started``, ``task_completed``, and ``task_failed``
    frames pushed via :class:`~robotsix_chat.chat.events.EventBus`.  Heartbeat
    comments keep the connection alive during quiet periods.

    Tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = request.query_params.get("client_id")
    if not session_id:
        return JSONResponse(
            {"error": "session_id query parameter is required"}, status_code=400
        )

    async def event_stream() -> AsyncIterator[bytes]:
        queue = request.app.state.event_bus.subscribe(session_id)
        try:
            yield _SSE_HEARTBEAT_FRAME  # first byte immediately
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), SSE_HEARTBEAT_INTERVAL)
                except TimeoutError:
                    if await request.is_disconnected():
                        break
                    yield _SSE_HEARTBEAT_FRAME
                    continue
                yield _sse_frame(frame)
        finally:
            request.app.state.event_bus.unsubscribe(session_id, queue)

    return StreamingResponse(
        event_stream(),
        media_type=SSE_CONTENT_TYPE,
        headers={"Content-Type": SSE_CONTENT_TYPE},
    )


async def loops_stop_endpoint(request: Request) -> JSONResponse:
    """Stop a running check loop.

    ``POST /loops/{loop_id}/stop`` stops the loop and returns a 200 JSON ack.
    Returns 503 when the check-loop feature is not wired (registry is None),
    or 404 when the loop id is unknown.
    """
    registry: CheckLoopRegistry | None = request.app.state.check_loop_registry
    if registry is None:
        return JSONResponse(
            {"error": "check-loop feature not enabled"}, status_code=503
        )

    loop_id = request.path_params["loop_id"]

    if registry.get(loop_id) is None:
        return JSONResponse(
            {"error": "unknown loop", "loop_id": loop_id}, status_code=404
        )

    registry.stop(loop_id, reason="stopped via api")
    return JSONResponse({"loop_id": loop_id, "status": "stopped"})


async def loops_list_endpoint(request: Request) -> JSONResponse:
    """Return all check loops for a session as JSON.

    ``GET /loops?session_id=...`` returns ``{"loops": [...]}`` where each
    entry contains every :class:`~robotsix_chat.chat.loops.LoopInfo` field
    with ``status`` serialized as its plain string value.

    Tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).

    Returns 400 when ``session_id`` is missing, and 503 when the check-loop
    feature is not wired (``app.state.check_loop_registry`` is ``None``).
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = request.query_params.get("client_id")
    if not session_id:
        return JSONResponse(
            {"error": "session_id query parameter is required"}, status_code=400
        )

    registry: CheckLoopRegistry | None = request.app.state.check_loop_registry
    if registry is None:
        return JSONResponse(
            {"error": "check-loop feature not enabled"}, status_code=503
        )

    loops = registry.list_for_session(session_id)
    result: list[dict[str, object]] = []
    for info in loops:
        result.append(
            {
                "id": info.id,
                "session_id": info.session_id,
                "prompt": info.prompt,
                "interval_seconds": info.interval_seconds,
                "status": info.status.value,
                "iterations": info.iterations,
                "max_iterations": info.max_iterations,
                "last_result": info.last_result,
                "next_run": info.next_run,
                "error": info.error,
                "stop_reason": info.stop_reason,
                "reason": info.reason,
                "last_result_at": info.last_result_at,
            }
        )
    return JSONResponse({"loops": result})


# ---------------------------------------------------------------------------
# Sessions endpoints
# ---------------------------------------------------------------------------


async def sessions_list_endpoint(request: Request) -> JSONResponse:
    """List all sessions for an owner.

    ``GET /sessions?owner_id=...`` returns::

        {
          "sessions": [
            {
              "session_id": "...", "title": "...",
              "last_active": 1.0, "turn_count": 3, "closed": false
            },
            ...
          ],
          "active_session_id": "..."
        }

    Sorted by ``last_active`` descending.  If the owner has no sessions, a
    default empty session is lazily created and returned (so the list is
    never empty).
    """
    owner_id = request.query_params.get("owner_id")
    if not owner_id:
        return JSONResponse(
            {"error": "owner_id query parameter is required"}, status_code=400
        )

    store: ConversationStore = request.app.state.conversation_store
    sessions, active_id = store.list_sessions(owner_id)
    return JSONResponse({"sessions": sessions, "active_session_id": active_id})


async def sessions_create_endpoint(request: Request) -> JSONResponse:
    """Create a new empty session for an owner.

    ``POST /sessions`` with body ``{"owner_id": "..."}`` returns::

        {"session_id": "...", "title": "New chat", "last_active": 1.0, "turn_count": 0}

    The new session is marked as the owner's active session.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    owner_id = body.get("owner_id")
    if not owner_id or not isinstance(owner_id, str):
        return JSONResponse(
            {"error": "'owner_id' field is required and must be a string"},
            status_code=400,
        )

    store: ConversationStore = request.app.state.conversation_store
    session = store.create_session(owner_id)
    return JSONResponse(session)


async def sessions_delete_endpoint(request: Request) -> JSONResponse:
    """Close (delete) a session and stop its background work.

    ``DELETE /sessions/{session_id}?owner_id=...`` stops every check loop and
    cancels every in-flight background task owned by the session, deletes the
    session and its history, and returns::

        {
          "deleted": true,
          "active_session_id": "...",   # the owner's new active session
          "loops_stopped": 1,
          "tasks_cancelled": 0
        }

    ``owner_id`` is required (query param).  Returns 404 when the session is
    not found / not owned by *owner_id*.  Stopping loops/tasks is best-effort
    and runs even when the conversation delete is a no-op (so an orphaned loop
    can still be cleaned up).
    """
    session_id = request.path_params["session_id"]
    owner_id = request.query_params.get("owner_id")
    if not owner_id:
        return JSONResponse(
            {"error": "owner_id query parameter is required"}, status_code=400
        )

    # 1. Stop the session's check loops (registry may be unwired → None).
    loops_stopped = 0
    loop_registry = request.app.state.check_loop_registry
    if loop_registry is not None:
        loops_stopped = loop_registry.stop_all_for_session(
            session_id, reason="session closed"
        )

    # 2. Cancel the session's in-flight background tasks.
    tasks_cancelled = 0
    task_registry = request.app.state.task_registry
    if task_registry is not None:
        tasks_cancelled = task_registry.cancel_all_for_session(session_id)

    # 3. Delete the conversation/session itself.
    store: ConversationStore = request.app.state.conversation_store
    result = store.delete_session(owner_id, session_id)

    if not result.get("deleted"):
        return JSONResponse(
            {
                "error": "session not found",
                "session_id": session_id,
                "loops_stopped": loops_stopped,
                "tasks_cancelled": tasks_cancelled,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "deleted": True,
            "active_session_id": result.get("active_session_id", ""),
            "loops_stopped": loops_stopped,
            "tasks_cancelled": tasks_cancelled,
        }
    )


async def sessions_close_endpoint(request: Request) -> JSONResponse:
    """Close (mark as closed) a session and stop its background work.

    ``POST /sessions/{session_id}/close?owner_id=...`` stops every check loop
    and cancels every in-flight background task owned by the session, marks
    the session as ``closed`` (preventing it from spawning new work), and
    returns::

        {
          "closed": true,
          "session_id": "...",
          "loops_stopped": 1,
          "tasks_cancelled": 0
        }

    ``owner_id`` is required (query param).  Returns 404 when the session is
    not found / not owned by *owner_id*.  Stopping loops/tasks is best-effort
    and runs even when the session is not found (so orphaned work can still be
    cleaned up).

    Unlike ``DELETE /sessions/{session_id}``, closing preserves the session's
    history and metadata — the session cannot spawn new background work but
    its conversation history remains available.
    """
    session_id = request.path_params["session_id"]
    owner_id = request.query_params.get("owner_id")
    if not owner_id:
        return JSONResponse(
            {"error": "owner_id query parameter is required"}, status_code=400
        )

    # 1. Stop the session's check loops (registry may be unwired → None).
    loops_stopped = 0
    loop_registry = request.app.state.check_loop_registry
    if loop_registry is not None:
        loops_stopped = loop_registry.stop_all_for_session(
            session_id, reason="session closed"
        )

    # 2. Cancel the session's in-flight background tasks.
    tasks_cancelled = 0
    task_registry = request.app.state.task_registry
    if task_registry is not None:
        tasks_cancelled = task_registry.cancel_all_for_session(session_id)

    # 3. Mark the session as closed in the conversation store.
    store: ConversationStore = request.app.state.conversation_store
    result = store.close_session(owner_id, session_id)

    if not result.get("closed"):
        return JSONResponse(
            {
                "error": "session not found",
                "session_id": session_id,
                "loops_stopped": loops_stopped,
                "tasks_cancelled": tasks_cancelled,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "closed": True,
            "session_id": session_id,
            "loops_stopped": loops_stopped,
            "tasks_cancelled": tasks_cancelled,
        }
    )


# ---------------------------------------------------------------------------
# Pending questions endpoints
# ---------------------------------------------------------------------------


async def pending_questions_list_endpoint(request: Request) -> JSONResponse:
    """Return all pending questions for a session as JSON.

    ``GET /pending-questions?session_id=...`` returns
    ``{"questions": [...]}`` with each entry containing question_id, text,
    detail, status, answer, answered_at, and created_at.
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = request.query_params.get("client_id")
    if not session_id:
        return JSONResponse(
            {"error": "session_id query parameter is required"}, status_code=400
        )

    store: PendingQuestionsStore = request.app.state.pq_store
    entries = store.list_for_session(session_id)
    return JSONResponse(
        {
            "questions": [
                {
                    "question_id": q.question_id,
                    "session_id": q.session_id,
                    "text": q.text,
                    "detail": q.detail,
                    "status": q.status,
                    "answer": q.answer,
                    "answered_at": q.answered_at,
                    "created_at": q.created_at,
                    "thread": [
                        {
                            "role": m.role,
                            "text": m.text,
                            "timestamp": m.timestamp,
                        }
                        for m in q.thread
                    ],
                }
                for q in entries
            ]
        }
    )


async def pending_questions_answer_endpoint(request: Request) -> JSONResponse:
    """Record an answer for a pending question.

    ``POST /pending-questions/{question_id}/answer`` with JSON body
    ``{"answer": "..."}`` sets the question's ``status`` to ``"answered"``
    and stores the answer text.  The question stays in the store (and the
    panel) until the assistant explicitly removes it.

    Returns 200 with the question data on success, or 404 when the id is
    unknown.
    """
    question_id = request.path_params["question_id"]
    store: PendingQuestionsStore = request.app.state.pq_store

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    answer_text = body.get("answer", "")
    if not isinstance(answer_text, str):
        return JSONResponse({"error": "'answer' must be a string"}, status_code=400)

    entry = store.answer(question_id, answer_text)
    if entry is None:
        return JSONResponse(
            {"error": "unknown question", "question_id": question_id},
            status_code=404,
        )
    return JSONResponse(
        {
            "question_id": entry.question_id,
            "status": entry.status,
            "answer": entry.answer,
        }
    )


async def pending_questions_delete_endpoint(request: Request) -> JSONResponse:
    """Remove a pending question by id.

    ``DELETE /pending-questions/{question_id}`` removes the question and
    returns a 200 JSON ack.  Returns 404 when the question id is unknown.
    """
    question_id = request.path_params["question_id"]
    store: PendingQuestionsStore = request.app.state.pq_store
    entry = store.remove(question_id)
    if entry is None:
        return JSONResponse(
            {"error": "unknown question", "question_id": question_id},
            status_code=404,
        )
    return JSONResponse({"question_id": question_id, "status": "removed"})


async def pending_questions_thread_append_endpoint(
    request: Request,
) -> JSONResponse:
    """Append a user message to a pending question's thread and get an LLM reply.

    ``POST /pending-questions/{question_id}/thread`` with JSON body
    ``{"text": "..."}`` appends a ``"user"``-role message to the question's
    conversation thread, then spawns a background task that calls the LLM
    with merged main-chat + thread context and appends the assistant's reply
    to the thread.  Publishes ``pending_question_thread_message`` frames
    so connected browsers update in real time — no page reload is needed.

    Returns 202 with the question id on success (processing continues in
    the background), or 404 when the id is unknown.
    """
    question_id = request.path_params["question_id"]
    store: PendingQuestionsStore = request.app.state.pq_store

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    text = body.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {"error": "'text' must be a non-empty string"},
            status_code=400,
        )

    entry = store.append_to_thread(question_id, "user", text)
    if entry is None:
        return JSONResponse(
            {"error": "unknown question", "question_id": question_id},
            status_code=404,
        )

    # ---- background LLM call -------------------------------------------
    agent: ChatAgent = request.app.state.agent
    conv_store: ConversationStore = request.app.state.conversation_store
    session_id = entry.session_id

    async def _process_thread_message() -> None:
        try:
            # Build merged context: main-chat history + full thread.
            _, history = conv_store.begin(session_id)
            thread = store.get_thread(question_id)
            thread_context = _build_thread_context(entry, thread)

            # Run the agent with merged context.
            reply_parts: list[str] = []
            async for token in agent.stream(
                thread_context,
                history=history,
                session_id=session_id,
                client_id=session_id,
            ):
                reply_parts.append(token)

            reply = "".join(reply_parts).strip()
            if reply:
                store.append_to_thread(question_id, "assistant", reply)
        except Exception:
            logger.exception("Background LLM call failed for thread %s", question_id)

    asyncio.create_task(_process_thread_message())
    return JSONResponse(
        {"question_id": question_id, "status": "message_appended"},
        status_code=202,
    )


async def pending_questions_thread_get_endpoint(
    request: Request,
) -> JSONResponse:
    """Return the full thread for a pending question.

    ``GET /pending-questions/{question_id}/thread`` returns
    ``{"question_id": "...", "thread": [...]}`` with each message
    containing ``role``, ``text``, and ``timestamp``.

    Returns 404 when the question id is unknown.
    """
    question_id = request.path_params["question_id"]
    store: PendingQuestionsStore = request.app.state.pq_store
    thread = store.get_thread(question_id)
    if thread is None:
        return JSONResponse(
            {"error": "unknown question", "question_id": question_id},
            status_code=404,
        )
    return JSONResponse(
        {
            "question_id": question_id,
            "thread": [
                {
                    "role": msg.role,
                    "text": msg.text,
                    "timestamp": msg.timestamp,
                }
                for msg in thread
            ],
        }
    )


# ---------------------------------------------------------------------------
# Thread-context builder for background LLM calls
# ---------------------------------------------------------------------------


def _build_thread_context(
    entry: PendingQuestion,
    thread: list[ThreadMessage] | None,
) -> str:
    """Build a merged-context prompt for an LLM call on a pending-question thread.

    The prompt includes the question text, any detail, and the full
    chronological thread so the LLM has complete awareness of the discussion
    so far — including the user message that just triggered the call.
    """
    parts: list[str] = []
    parts.append(f"[Pending Question: {entry.text}]")
    if entry.detail:
        parts.append(f"[Detail: {entry.detail}]")
    if entry.status == "answered" and entry.answer:
        parts.append(f"[Latest explicit answer: {entry.answer}]")

    if thread:
        parts.append("\nThread conversation (oldest first):")
        for msg in thread:
            role_label = msg.role.upper()
            parts.append(f"  [{role_label}] {msg.text}")

    parts.append(
        "\nYou are replying in a mini-chat thread for the pending question "
        "above.  Address the most recent user message directly.  Keep your "
        "reply concise and conversational.  You have full awareness of the "
        "main conversation and the thread history — use that context to "
        "give a helpful answer."
    )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text."""
    return JSONResponse({"error": "not found"}, status_code=404)


async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unhandled server errors."""
    return JSONResponse({"error": "internal server error"}, status_code=500)
