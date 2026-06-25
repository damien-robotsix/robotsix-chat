"""Chat SSE server — ASGI application and entry point.

Exposes an LLM agent to human users via HTTP + Server-Sent Events, and serves
a self-contained browser chat UI from the same origin.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import logging.config
from collections.abc import AsyncIterator, Callable
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from asgi_correlation_id import CorrelationIdMiddleware
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from robotsix_chat import PROJECT_TITLE
from robotsix_chat.board_reader import build_board_reader_tools
from robotsix_chat.calendar import build_calendar_tools
from robotsix_chat.chat.auth import BasicAuthConfig, BasicAuthMiddleware
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.events import EventBus
from robotsix_chat.chat.tasks import TaskRegistry
from robotsix_chat.component_client import build_component_tools
from robotsix_chat.config import Settings, level_needs_api_key
from robotsix_chat.knowledge import build_knowledge_tools
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.mail import build_mail_tools
from robotsix_chat.memory import build_memory
from robotsix_chat.mill import build_mill_tools
from robotsix_chat.pending_questions import build_pending_questions_tools
from robotsix_chat.pending_questions.store import PendingQuestionsStore
from robotsix_chat.refdocs import build_refdocs_tools
from robotsix_chat.selfreview import build_recent_activity_tools
from robotsix_chat.version_check import build_version_check_tools

if TYPE_CHECKING:
    from robotsix_chat.chat.loops import CheckLoopRegistry
    from robotsix_chat.chat.runner import DeliveryChannel

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


def _load_ui_html(idle_timeout_minutes: int) -> str:
    """Read the bundled browser UI (``ui/index.html``) and fill placeholders."""
    raw = (resources.files("robotsix_chat") / "ui" / "index.html").read_text(
        encoding="utf-8"
    )
    return raw.replace("{{ PROJECT_TITLE }}", PROJECT_TITLE).replace(
        "{{ IDLE_TIMEOUT_MINUTES }}", str(idle_timeout_minutes)
    )


async def ui_endpoint(request: Request) -> HTMLResponse:
    """Serve the self-contained browser chat UI at ``GET /``."""
    timeout = request.app.state.idle_timeout_minutes
    return HTMLResponse(_load_ui_html(timeout))


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

    # -- parse & validate images (optional) -------------------------------
    max_per_msg: int = request.app.state.max_images_per_message
    max_bytes: int = request.app.state.max_image_bytes
    allowed_types: list[str] = request.app.state.allowed_image_media_types

    raw_images = body.get("images")
    images: list[tuple[str, bytes]] | None = None
    if raw_images is not None:
        if not isinstance(raw_images, list):
            return JSONResponse(
                {"error": "'images' must be a JSON array"}, status_code=400
            )
        if len(raw_images) > max_per_msg:
            return JSONResponse(
                {
                    "error": (
                        f"too many images: got {len(raw_images)}, maximum {max_per_msg}"
                    )
                },
                status_code=400,
            )
        images = []
        import base64

        for idx, img in enumerate(raw_images):
            if not isinstance(img, dict):
                return JSONResponse(
                    {"error": f"images[{idx}]: expected a JSON object"},
                    status_code=400,
                )
            media_type = img.get("media_type")
            if not isinstance(media_type, str) or not media_type:
                return JSONResponse(
                    {"error": (f"images[{idx}]: missing or invalid 'media_type'")},
                    status_code=400,
                )
            if media_type not in allowed_types:
                return JSONResponse(
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
                return JSONResponse(
                    {"error": f"images[{idx}]: missing or invalid 'data'"},
                    status_code=400,
                )
            try:
                raw_bytes = base64.b64decode(data_b64, validate=True)
            except Exception:
                return JSONResponse(
                    {"error": f"images[{idx}]: 'data' is not valid base64"},
                    status_code=400,
                )
            if len(raw_bytes) > max_bytes:
                return JSONResponse(
                    {
                        "error": (
                            f"images[{idx}]: decoded size {len(raw_bytes)} "
                            f"exceeds maximum {max_bytes}"
                        )
                    },
                    status_code=400,
                )
            images.append((media_type, raw_bytes))

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

    if session_id:
        session_id, history = store.begin(session_id)
    else:
        session_id, history = store.new_session_id(), None

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
                reply_parts: list[str] = []
                try:
                    async for token in agent.stream(
                        message,
                        history=history,
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
            {"session_id": "...", "title": "...", "last_active": 1.0, "turn_count": 3},
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


# ---------------------------------------------------------------------------
# Pending questions endpoints
# ---------------------------------------------------------------------------


async def pending_questions_list_endpoint(request: Request) -> JSONResponse:
    """Return all pending questions for a session as JSON.

    ``GET /pending-questions?session_id=...`` returns
    ``{"questions": [...]}`` with each entry containing question_id, text,
    detail, status, and created_at.
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
                    "created_at": q.created_at,
                }
                for q in entries
            ]
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


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text."""
    return JSONResponse({"error": "not found"}, status_code=404)


async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unhandled server errors."""
    return JSONResponse({"error": "internal server error"}, status_code=500)


# ---------------------------------------------------------------------------
# Application factory & entry point
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _make_lifespan(
    on_startup: Callable[[], None] | None,
    *,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> AsyncIterator[None]:
    """Starlette lifespan that invokes hooks on startup and shutdown.

    A resume failure is logged but does not crash app startup.
    """
    if on_startup is not None:
        try:
            on_startup()
        except Exception:
            logger.exception("Startup hook failed — continuing")
    if on_startup_async is not None:
        try:
            await on_startup_async()
        except Exception:
            logger.exception("Async startup hook failed — continuing")
    try:
        yield
    finally:
        if on_shutdown is not None:
            try:
                await on_shutdown()
            except Exception:
                logger.exception("Shutdown hook failed")


def create_app(
    agent: ChatAgent,
    *,
    serve_ui: bool = True,
    idle_timeout_minutes: int = 30,
    max_images_per_message: int = 8,
    max_image_bytes: int = 5_242_880,
    allowed_image_media_types: list[str] | None = None,
    cors_allow_origins: list[str] | None = None,
    auth: BasicAuthConfig | None = None,
    correlation_id_header: str = "X-Request-ID",
    conversation_store: ConversationStore | None = None,
    task_registry: TaskRegistry | None = None,
    event_bus: EventBus | None = None,
    check_loop_registry: CheckLoopRegistry | None = None,
    run_serializer: RunSerializer | None = None,
    pq_store: PendingQuestionsStore | None = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> Starlette:
    """Return a Starlette ASGI app wired to ``agent``.

    The returned app is a fully-initialised ASGI application that can be
    mounted directly in tests via ``httpx.ASGITransport`` or passed to
    ``uvicorn.run()``.

    Args:
        agent: Object whose ``stream(message)`` yields response tokens.
        serve_ui: When ``True`` (default), serve the bundled browser chat
            UI at ``GET /`` so the UI and ``/chat`` share one origin.
        idle_timeout_minutes: Minutes of no user activity before the UI
            auto-restarts the conversation; ``0`` disables.
        max_images_per_message: Maximum number of images a client may attach
            to a single ``POST /chat`` request.  Default ``8``.
        max_image_bytes: Maximum decoded size (bytes) of a single attached
            image.  Default ``5_242_880`` (5 MiB).
        allowed_image_media_types: Media types accepted for image
            attachments.  Default ``["image/png", "image/jpeg", "image/gif",
            "image/webp"]``.
        cors_allow_origins: Origins permitted to call ``/chat`` cross-origin
            (e.g. when the UI is hosted separately). ``None`` (default)
            adds no CORS headers; ``["*"]`` allows any origin.
        auth: When set, gate every request except ``GET /health`` behind
            HTTP Basic Auth with these credentials. ``None`` (default)
            leaves the server open.
        correlation_id_header: HTTP header name for the correlation /
            request-id. Default ``X-Request-ID``.
        conversation_store: Tracks per-client multi-turn conversation history
            and trace sessions. ``None`` (default) builds one with default
            settings (30-minute idle reset).
        task_registry: Shared registry for background sub-agent task
            lifecycle.  When ``None`` (default), a fresh registry is
            created and wired to the internal event bus.  Pass an existing
            instance to share the same registry between the foreground
            agent's delegation tool and the ``GET /events`` SSE endpoint.
        event_bus: Per-client SSE notification bus for ``GET /events``.
            When ``None`` (default), a fresh :class:`EventBus` is created.
            Pass the same instance given to the :class:`TaskRegistry` so
            lifecycle frames published by the registry reach the SSE
            subscribers.
        check_loop_registry: Shared registry for recurring check-loop
            lifecycle.  Leave ``None`` (default) when check loops are
            not wired — the stop route returns 503 and the resume hook
            is skipped.
        run_serializer: Per-owner ``RunSerializer`` that prevents
            overlapping agent runs for the same owner.  When ``None``
            (default), a fresh ``RunSerializer`` is created.  Pass the
            same instance to the ``ConversationDeliveryChannel`` so
            tick-triggered runs and user-initiated ``/chat`` requests
            are serialized together.
        pq_store: Per-server pending-questions store for the real-time
            agent-questions panel.  When ``None`` (default), a fresh
            :class:`PendingQuestionsStore` is created and wired to the
            internal event bus.  Pass an existing instance to share state.
        on_startup: Optional callable invoked during application startup
            (the Starlette lifespan ``startup`` phase).  Pass a closure
            that e.g. resumes persisted check loops.
        on_startup_async: Optional async callable invoked after *on_startup*
            during application startup.  Pass a coroutine function that
            e.g. starts the component-agent responder.
        on_shutdown: Optional async callable invoked during application
            shutdown (after ``yield``).  Pass a coroutine function that
            e.g. stops the component-agent responder.

    """
    routes = [
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/chat", chat_endpoint, methods=["POST"]),
        Route("/events", events_endpoint, methods=["GET"]),
        Route("/history", history_endpoint, methods=["GET"]),
        Route("/loops/{loop_id}/stop", loops_stop_endpoint, methods=["POST"]),
        Route("/loops", loops_list_endpoint, methods=["GET"]),
        Route("/sessions", sessions_list_endpoint, methods=["GET"]),
        Route("/sessions", sessions_create_endpoint, methods=["POST"]),
        Route("/pending-questions", pending_questions_list_endpoint, methods=["GET"]),
        Route(
            "/pending-questions/{question_id}",
            pending_questions_delete_endpoint,
            methods=["DELETE"],
        ),
    ]
    if serve_ui:
        routes.append(Route("/", ui_endpoint, methods=["GET"]))

    # CorrelationIdMiddleware is outermost so every request (and its log lines)
    # carries a request id. CORS comes next so it can answer preflight
    # ``OPTIONS`` (which carry no credentials) before the auth layer rejects them.
    middleware = [
        Middleware(CorrelationIdMiddleware, header_name=correlation_id_header)
    ]
    if cors_allow_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=cors_allow_origins,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type"],
            )
        )
    if auth is not None:
        middleware.append(Middleware(BasicAuthMiddleware, config=auth))

    app = Starlette(
        routes=routes,
        middleware=middleware,
        exception_handlers={
            404: not_found_handler,
            500: server_error_handler,
        },
        lifespan=lambda app: _make_lifespan(
            on_startup,
            on_startup_async=on_startup_async,
            on_shutdown=on_shutdown,
        ),
    )
    app.state.agent = agent
    app.state.conversation_store = conversation_store or ConversationStore()
    app.state.idle_timeout_minutes = idle_timeout_minutes
    app.state.max_images_per_message = max_images_per_message
    app.state.max_image_bytes = max_image_bytes
    app.state.allowed_image_media_types = (
        allowed_image_media_types
        if allowed_image_media_types is not None
        else ["image/png", "image/jpeg", "image/gif", "image/webp"]
    )
    app.state.event_bus = event_bus or EventBus()
    app.state.task_registry = task_registry or TaskRegistry(
        event_sink=app.state.event_bus
    )
    app.state.check_loop_registry = check_loop_registry  # may be None
    app.state.run_serializer = run_serializer or RunSerializer()
    app.state.pq_store = pq_store or PendingQuestionsStore(
        event_bus=app.state.event_bus
    )
    return app


def run_server(
    agent: ChatAgent,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    serve_ui: bool = True,
    idle_timeout_minutes: int = 30,
    max_images_per_message: int = 8,
    max_image_bytes: int = 5_242_880,
    allowed_image_media_types: list[str] | None = None,
    cors_allow_origins: list[str] | None = None,
    auth: BasicAuthConfig | None = None,
    correlation_id_header: str = "X-Request-ID",
    conversation_store: ConversationStore | None = None,
    task_registry: TaskRegistry | None = None,
    event_bus: EventBus | None = None,
    check_loop_registry: CheckLoopRegistry | None = None,
    run_serializer: RunSerializer | None = None,
    pq_store: PendingQuestionsStore | None = None,
    on_startup: Callable[[], None] | None = None,
    on_startup_async: Callable[[], Any] | None = None,
    on_shutdown: Callable[[], Any] | None = None,
) -> None:
    """Start the chat SSE server on ``host:port``.

    Blocks until the process is interrupted (uvicorn handles
    ``SIGINT`` / ``SIGTERM``).
    """
    import uvicorn

    app = create_app(
        agent,
        serve_ui=serve_ui,
        idle_timeout_minutes=idle_timeout_minutes,
        max_images_per_message=max_images_per_message,
        max_image_bytes=max_image_bytes,
        allowed_image_media_types=allowed_image_media_types,
        cors_allow_origins=cors_allow_origins,
        auth=auth,
        correlation_id_header=correlation_id_header,
        conversation_store=conversation_store,
        task_registry=task_registry,
        event_bus=event_bus,
        check_loop_registry=check_loop_registry,
        run_serializer=run_serializer,
        pq_store=pq_store,
        on_startup=on_startup,
        on_startup_async=on_startup_async,
        on_shutdown=on_shutdown,
    )
    uvicorn.run(app, host=host, port=port)


def create_agent_from_settings(
    instruction: str | None = None,
    settings: Settings | None = None,
    *,
    task_registry: TaskRegistry | None = None,
    delivery_channel: DeliveryChannel | None = None,
    check_loop_registry: CheckLoopRegistry | None = None,
    conversation_store: ConversationStore | None = None,
    model_override: str | None = None,
    tool_wrapper: Callable[[list[Any]], list[Any]] | None = None,
    pq_store: PendingQuestionsStore | None = None,
) -> LlmioChatAgent:
    """Build an :class:`LlmioChatAgent` wired from *settings*.

    The backend is chosen by robotsix-llmio's capability ``model_level``
    (``settings.llmio_model_level``) — the level encodes the transport + model.
    ``settings.llmio_api_key`` is forwarded only when that level's transport
    needs a key (so keyless levels like claude-sdk never receive one).

    *model_override* is a bare model name (e.g. ``"sonnet"``) passed through
    to :class:`LlmioChatAgent` as ``model_name=``.  When ``None`` (the
    default), the model is resolved from the level's tier default.

    When *settings* is ``None``, ``Settings.load()`` resolves configuration
    from the YAML config file and environment. When *instruction* is ``None``,
    it is taken from ``settings.agent_instruction``.

    Long-term memory is attached when ``settings.memory.enabled`` is set
    (otherwise a no-op memory is used and the agent stays stateless). The mill
    consult tool is attached when ``settings.mill.enabled`` is set; the calendar
    and task tools are attached when ``settings.calendar.enabled`` is set; the
    reference-docs tools are attached when ``settings.refdocs.enabled`` is set;
    the self-review ``read_recent_activity`` tool is attached when
    ``settings.self_review.enabled`` is set and *conversation_store* is provided
    (otherwise no tools are added).

    When both *task_registry* and *delivery_channel* are provided, the
    ``delegate_task`` tool is also added so the agent can offload long-running
    work to a background sub-agent.  Sub-agents built by the runner — which
    omits these two arguments — do **not** receive the delegation tool,
    preventing infinite recursion.

    When *check_loop_registry* is provided, the ``start_check_loop`` tool is
    added so the agent can launch a recurring check loop on the user's behalf.
    Sub-agents (which omit it) do not receive the loop tool, preventing
    recursion.
    """
    if settings is None:
        settings = Settings.load()
    if instruction is None:
        instruction = settings.agent_instruction

    api_key = (
        settings.llmio_api_key
        if level_needs_api_key(settings.llmio_model_level)
        else ""
    )
    tools: list[Any] = [
        *build_mill_tools(settings.mill),
        *build_mail_tools(settings.mail),
        *build_calendar_tools(settings.calendar),
        *build_component_tools(settings.component_client),
        *build_refdocs_tools(settings.refdocs),
        *build_board_reader_tools(settings.board_reader),
        *build_knowledge_tools(settings.knowledge),
        *build_recent_activity_tools(settings.self_review, conversation_store),
        *build_version_check_tools(settings.version_check),
    ]
    if tool_wrapper is not None:
        tools = tool_wrapper(tools)
    # Attach per-request tools from independently-gated sources so the
    # foreground agent can delegate work and launch check loops.
    # The factory lambda is called once per stream() invocation with the
    # request's session id (passed via stream()'s client_id argument), so tool
    # closures capture the owning session lexically — surviving the
    # claude_sdk/MCP boundary.  Background tasks and check loops are therefore
    # scoped to the session that spawned them.
    request_tools_factory: Callable[[str], list[Any]] | None = None
    if (
        task_registry is not None and delivery_channel is not None
    ) or check_loop_registry is not None:
        from robotsix_chat.chat.delegation import (
            build_check_loop_tools,
            build_delegation_tools,
        )
        from robotsix_chat.chat.runner import NULL_CHANNEL

        # Compute the sub-agent model override ONCE.  The override is only
        # meaningful for the keyless claudeSDK provider (level 3); for
        # OpenRouter levels (1–2) a bare "sonnet"/"haiku" is not a valid
        # model name, so the override is suppressed.
        subagent_model = (
            settings.subagent_model
            if (
                settings.subagent_model
                and not level_needs_api_key(settings.llmio_model_level)
            )
            else None
        )

        def _subagent_factory(s: Settings) -> LlmioChatAgent:
            """Build a sub-agent with the downgraded model.

            When the override is suppressed the foreground model is used.
            Omits task_registry, delivery_channel, and check_loop_registry so
            sub-agents get neither delegate_task nor start_check_loop tools —
            preserving the recursion guard.
            """
            return create_agent_from_settings(settings=s, model_override=subagent_model)

        def _make_request_tools(session_id: str) -> list[Any]:
            request_tools: list[Any] = []
            if task_registry is not None and delivery_channel is not None:
                request_tools.extend(
                    build_delegation_tools(
                        settings,
                        task_registry,
                        delivery_channel,
                        session_id=session_id,
                        agent_factory=_subagent_factory,
                    )
                )
            if check_loop_registry is not None:
                request_tools.extend(
                    build_check_loop_tools(
                        settings,
                        check_loop_registry,
                        delivery_channel or NULL_CHANNEL,
                        session_id=session_id,
                        agent_factory=_subagent_factory,
                    )
                )
            if pq_store is not None:
                request_tools.extend(
                    build_pending_questions_tools(
                        settings.pending_questions,
                        pq_store,
                        session_id=session_id,
                    )
                )
            return request_tools

        request_tools_factory = _make_request_tools
    return LlmioChatAgent(
        model_level=settings.llmio_model_level,
        instruction=instruction,
        api_key=api_key,
        memory=build_memory(settings.memory),
        tools=tools,
        request_tools_factory=request_tools_factory,
        model_name=model_override,
    )


def _setup_observability() -> None:
    """Configure Langfuse tracing and OTel-aware logging (idempotent).

    Both calls are no-ops when their prerequisites are absent:
    * ``setup_langfuse_tracing`` returns ``False`` when ``LANGFUSE_PUBLIC_KEY``
      / ``LANGFUSE_SECRET_KEY`` env vars are unset.
    * ``setup_logging`` is always safe to call; it only configures the
      ``robotsix_llmio`` logger namespace and leaves the root logger alone.

    Both are wrapped in a blanket ``ImportError`` guard so the server still
    starts when the ``tracing`` optional-dependency extra is not installed.
    """
    try:
        from robotsix_llmio.core.tracing import setup_langfuse_tracing
        from robotsix_llmio.logging import setup_logging
    except ImportError:
        logger.debug("robotsix-llmio tracing extras not installed — skipping")
        return

    setup_logging()
    setup_langfuse_tracing()


def run_server_from_config(agent: ChatAgent | None = None) -> None:
    """Start the chat SSE server using ``Settings.load()`` for configuration.

    Resolves settings through the full cascade (pydantic defaults → YAML
    config file → environment, with ``.env`` support), configures Python
    logging, builds a default :class:`LlmioChatAgent` when *agent* is
    ``None`` (using ``agent_instruction`` from config), enables HTTP Basic
    Auth when ``auth.enabled`` is set, and delegates to :func:`run_server`.
    """
    settings = Settings.load()
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "correlation_id": {
                    "()": "asgi_correlation_id.CorrelationIdFilter",
                }
            },
            "formatters": {
                "default": {
                    "format": (
                        "%(asctime)s %(levelname)-8s "
                        "[%(correlation_id)s] %(name)s %(message)s"
                    ),
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "default",
                    "filters": ["correlation_id"],
                },
            },
            "root": {
                "level": settings.log_level.upper(),
                "handlers": ["default"],
            },
        }
    )

    # -- tracing / observability (graceful no-op when deps or creds absent) --
    _setup_observability()

    # -- shared task registry + delivery channel for delegation -----------
    # Two independent notification sinks for background-task lifecycles:
    #
    # 1. EventBus → GET /events SSE → browser UI
    #    TaskRegistry.complete() / .fail() publish task_completed / task_failed
    #    frames to the EventBus, which the /events endpoint streams to the
    #    connected browser.  This path is unchanged.
    #
    # 2. ConversationDeliveryChannel → ConversationStore → agent history
    #    The runner's _worker also calls channel.publish(session_id, frame).
    #    This channel records completed/failed task results into the
    #    ConversationStore keyed by the originating session_id, so the
    #    foreground agent sees them in its next-turn history and can relay
    #    task IDs / URLs / findings to the user.
    #
    # The two sinks have different destinations (browser SSE vs. agent
    # history) — there is no duplicate-frame concern.
    #
    # The conversation store MUST be constructed before the channel so both
    # the channel and run_server() receive the exact same store instance.
    from robotsix_chat.chat.delegation import ConversationDeliveryChannel
    from robotsix_chat.chat.loops import CheckLoopRegistry, resume_check_loops

    event_bus = EventBus()
    registry = TaskRegistry(event_sink=event_bus)
    check_loop_registry = CheckLoopRegistry(event_sink=event_bus)

    persist_path_str = settings.conversation.persist_path
    conversation_store = ConversationStore(
        idle_reset_seconds=settings.conversation.idle_reset_seconds,
        max_history_turns=settings.conversation.max_history_turns,
        max_conversations=settings.conversation.max_conversations,
        persist_path=Path(persist_path_str) if persist_path_str else None,
    )

    # Per-owner run serializer — shared between /chat requests and
    # tick-triggered runs so they never overlap for the same owner.
    run_serializer = RunSerializer()

    # No-loop-tools foreground agent factory for tick-triggered runs.
    # Omits check_loop_registry so a tick-triggered run can never spawn
    # a new check loop (preserves the loop-sub-agent no-loop-tools boundary).
    # Other foreground tools (delegation, consult_mill, etc.) remain.
    def _tick_agent_factory(s: Settings) -> LlmioChatAgent:
        return create_agent_from_settings(
            settings=s,
            task_registry=registry,
            delivery_channel=channel,
            check_loop_registry=None,  # NO loop tools — recursion guard
            conversation_store=conversation_store,
        )

    # The channel uses the no-loop-tools factory for tick-triggered runs;
    # we build the channel first, then wire it as the delivery_channel for
    # the foreground agent (which DOES get loop tools).
    channel = ConversationDeliveryChannel(
        conversation_store,
        event_bus=event_bus,
        run_serializer=run_serializer,
        agent_factory=_tick_agent_factory,
        settings=settings,
    )

    if agent is None:
        agent = create_agent_from_settings(
            settings=settings,
            task_registry=registry,
            delivery_channel=channel,
            check_loop_registry=check_loop_registry,
            conversation_store=conversation_store,
        )

    # -- resume persisted check loops after redeploy ----------------------
    def _resume() -> None:
        """Resume any check loops that were RUNNING at last shutdown."""
        resume_check_loops(check_loop_registry, settings, channel=channel)

    # -- component-agent responder (disabled by default; gated on the -----
    # -- optional broker extra) -------------------------------------------
    async def _start_responder() -> None:
        """Start the component-agent responder when enabled + broker present."""
        if not settings.component_agent.enabled:
            return
        try:
            found = importlib.util.find_spec("robotsix_agent_comm")
        except (ValueError, ModuleNotFoundError):
            found = None
        if not found:
            logger.info(
                "component_agent.enabled=True but the broker extra "
                "(robotsix-agent-comm) is not installed — responder not started."
            )
            return
        from robotsix_chat.component_agent.responder import (
            ComponentAgentResponder,
        )

        _responder = ComponentAgentResponder(
            settings,
            check_loop_registry=check_loop_registry,
            conversation_store=conversation_store,
            event_bus=event_bus,
        )
        # Stash on the function so _stop_responder can reach it.
        _start_responder._responder = _responder  # type: ignore[attr-defined]
        await _responder.start()

    async def _stop_responder() -> None:
        """Stop the component-agent responder if it was started."""
        responder = getattr(_start_responder, "_responder", None)
        if responder is not None:
            await responder.stop()

    auth = (
        BasicAuthConfig(
            username=settings.auth.username, password=settings.auth.password
        )
        if settings.auth.enabled
        else None
    )
    run_server(
        agent,
        host=settings.server_host,
        port=settings.server_port,
        idle_timeout_minutes=settings.idle_timeout_minutes,
        max_images_per_message=settings.max_images_per_message,
        max_image_bytes=settings.max_image_bytes,
        allowed_image_media_types=settings.allowed_image_media_types,
        cors_allow_origins=settings.cors_allow_origins,
        auth=auth,
        correlation_id_header=settings.correlation_id_header,
        conversation_store=conversation_store,
        task_registry=registry,
        event_bus=event_bus,
        check_loop_registry=check_loop_registry,
        run_serializer=run_serializer,
        on_startup=_resume,
        on_startup_async=_start_responder,
        on_shutdown=_stop_responder,
    )
