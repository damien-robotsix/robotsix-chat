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
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Protocol

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse

from robotsix_chat.chat.conversation import ConversationStore

if TYPE_CHECKING:
    from robotsix_chat.subsessions import (
        ParentDelivery,
        SubsessionInfo,
        SubsessionRegistry,
    )

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE wire-format constants — single source of truth for tests and consumers.
# ---------------------------------------------------------------------------

SSE_CONTENT_TYPE = "text/event-stream"
SSE_TOKEN_TYPE = "token"  # nosec B105 — SSE event type name, not a credential
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


async def _parse_json_body(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse and type-guard a request's JSON body.

    Returns the parsed ``dict`` on success, or a ``JSONResponse`` error
    ready to return directly from an endpoint.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError, ValueError:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    return body


def _get_session_id(request: Request) -> str | JSONResponse:
    """Extract ``session_id`` from query params with ``client_id`` fallback.

    Returns the session id string on success, or a ``JSONResponse`` error
    ready to return directly from an endpoint.
    """
    session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = request.query_params.get("client_id")
    if not session_id:
        return JSONResponse(
            {"error": "session_id query parameter is required"}, status_code=400
        )
    return session_id


def _cleanup_session(session_id: str, request: Request) -> int:
    """Close every subsession owned by *session_id* (best-effort).

    Returns the number of subsessions closed; ``0`` when the subsession
    registry is not wired.
    """
    registry: SubsessionRegistry | None = request.app.state.subsession_registry
    if registry is None:
        return 0
    return registry.close_all_for_owner(session_id, reason="session closed")


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


async def history_endpoint(request: Request) -> JSONResponse:
    """Return a session's stored conversation history as JSON.

    ``GET /history?session_id=...`` returns ``{"turns": [[user, assistant], ...]}``.
    Also tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).
    """
    session_id = _get_session_id(request)
    if isinstance(session_id, JSONResponse):
        return session_id

    store: ConversationStore = request.app.state.conversation_store
    turns = store.history(session_id)
    return JSONResponse({"turns": turns})


async def summary_endpoint(request: Request) -> JSONResponse:
    """Generate a structured conversation summary.

    ``POST /summary`` with JSON body ``{"session_id": "..."}`` returns a
    JSON object with string fields ``purpose``, ``pending_work``,
    ``pending_questions``, ``blockers``, and ``relevant_info``.  Each
    field is an empty string when nothing relevant was found.

    The summary is regenerated from the full server-side history on
    every call — callers should invoke it after each assistant turn to
    keep the display current.
    """
    agent: ChatAgent = request.app.state.agent
    store: ConversationStore = request.app.state.conversation_store

    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body

    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        return JSONResponse(
            {"error": "session_id is required"}, status_code=400
        )

    turns = store.history(session_id)
    if not turns:
        return JSONResponse(
            {
                "purpose": "",
                "pending_work": "",
                "pending_questions": "",
                "blockers": "",
                "relevant_info": "",
            }
        )

    # Build a compact transcript.  Long assistant replies are truncated
    # to keep the prompt within reasonable bounds.
    transcript_parts: list[str] = []
    for user_msg, asst_msg in turns:
        transcript_parts.append(f"User: {user_msg}")
        if asst_msg:
            truncated = (
                asst_msg[:2000] + "…" if len(asst_msg) > 2000 else asst_msg
            )
            transcript_parts.append(f"Assistant: {truncated}")
    transcript = "\n".join(transcript_parts)

    _SUMMARY_PROMPT = (
        "Summarize the following conversation between a user and an AI "
        "assistant.  Return ONLY a single JSON object (no markdown fences, "
        "no other text) with exactly these five string fields:\n\n"
        '- "purpose": what the session is about / the goal of the current '
        "work.  Empty string if unclear.\n"
        '- "pending_work": what is currently in progress or still to be '
        "done.  Empty string if none.\n"
        '- "pending_questions": any question the assistant is waiting on '
        "the user to answer.  Empty string if none.\n"
        '- "blockers": anything blocking the current task.  Empty string '
        "if none.\n"
        '- "relevant_info": any other relevant information (links, ticket '
        "ids, background jobs running, etc.).  Empty string if none.\n\n"
        "Conversation:\n"
    )
    prompt = f"{_SUMMARY_PROMPT}{transcript}\n\nJSON summary:"

    reply_parts: list[str] = []
    try:
        async for token in agent.stream(
            prompt,
            history=None,
            session_id=None,
            client_id=None,
        ):
            reply_parts.append(token)
    except Exception:
        logger.exception("Summary generation failed")
        return JSONResponse(
            {"error": "summary generation failed"}, status_code=500
        )

    reply = "".join(reply_parts).strip()

    # Parse JSON from the reply.  The agent may wrap it in markdown fences
    # or add explanatory text — try several extraction strategies.
    summary: dict[str, object] = {}
    try:
        summary = json.loads(reply)
    except json.JSONDecodeError:
        # Try to extract from markdown code fences.
        fence_match = re.search(
            r"```(?:json)?\s*(\{.*?\})\s*```", reply, re.DOTALL
        )
        if fence_match:
            try:
                summary = json.loads(fence_match.group(1))
            except json.JSONDecodeError:
                pass
    if not summary:
        # Last resort: find the first JSON object containing "purpose".
        brace_match = re.search(
            r'\{[^{}]*"purpose"[^{}]*\}', reply, re.DOTALL
        )
        if brace_match:
            try:
                summary = json.loads(brace_match.group())
            except json.JSONDecodeError:
                pass

    # Ensure all expected fields exist with string values.
    _SUMMARY_FIELDS = (
        "purpose",
        "pending_work",
        "pending_questions",
        "blockers",
        "relevant_info",
    )
    result: dict[str, str] = {}
    for field in _SUMMARY_FIELDS:
        value = summary.get(field, "")
        result[field] = str(value) if value else ""

    return JSONResponse(result)


async def events_endpoint(request: Request) -> JSONResponse | StreamingResponse:
    """Open a persistent SSE channel for background-task lifecycle events.

    ``GET /events?session_id=...`` opens a never-closing ``text/event-stream``
    that delivers ``task_started``, ``task_completed``, and ``task_failed``
    frames pushed via :class:`~robotsix_chat.chat.events.EventBus`.  Heartbeat
    comments keep the connection alive during quiet periods.

    Tolerates ``client_id`` as a legacy fallback (treated as ``session_id``).
    """
    session_id = _get_session_id(request)
    if isinstance(session_id, JSONResponse):
        return session_id

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


# ---------------------------------------------------------------------------
# Subsession endpoints
# ---------------------------------------------------------------------------


def _get_subsession_registry(
    request: Request,
) -> SubsessionRegistry | JSONResponse:
    """Return the wired registry, or a ready-to-return 503 error response."""
    registry: SubsessionRegistry | None = request.app.state.subsession_registry
    if registry is None:
        return JSONResponse(
            {"error": "subsessions feature not enabled"}, status_code=503
        )
    return registry


def _resolve_subsession(
    request: Request,
) -> tuple[SubsessionRegistry, SubsessionInfo] | JSONResponse:
    """Resolve the subsession registry and look up the requested subsession.

    Returns ``(registry, info)`` on success, or a ready-to-return
    ``JSONResponse`` (503 or 404) when the lookup fails.
    """
    registry = _get_subsession_registry(request)
    if isinstance(registry, JSONResponse):
        return registry
    sub_id = request.path_params["sub_id"]
    info = registry.get(sub_id)
    if info is None:
        return JSONResponse(
            {"error": "unknown subsession", "subsession_id": sub_id},
            status_code=404,
        )
    return (registry, info)


async def subsessions_list_endpoint(request: Request) -> JSONResponse:
    """Return the whole subsession tree for a chat session.

    ``GET /subsessions?session_id=...`` returns ``{"subsessions": [...]}``
    — every subsession owned by the session (all kinds, all depths, all
    statuses; terminal entries are retained for a while so the panel can
    show recent history), sorted by ``created_at`` ascending, without
    transcripts.  Tolerates ``client_id`` as a legacy fallback.

    Returns 400 when ``session_id`` is missing and 503 when the
    subsession feature is not wired.
    """
    session_id = _get_session_id(request)
    if isinstance(session_id, JSONResponse):
        return session_id
    registry = _get_subsession_registry(request)
    if isinstance(registry, JSONResponse):
        return registry

    return JSONResponse(
        {
            "subsessions": [
                info.snapshot() for info in registry.list_for_owner(session_id)
            ]
        }
    )


async def subsessions_get_endpoint(request: Request) -> JSONResponse:
    """Return one subsession's full snapshot including its transcript.

    ``GET /subsessions/{sub_id}`` returns the snapshot dict plus a
    ``"transcript"`` list.  404 when the id is unknown.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    _registry, info = result
    return JSONResponse(info.snapshot(with_transcript=True))


async def subsessions_transcript_endpoint(request: Request) -> JSONResponse:
    """Return one subsession's transcript only.

    ``GET /subsessions/{sub_id}/transcript`` returns
    ``{"subsession_id": ..., "transcript": [{role, text, timestamp}, ...]}``.
    404 when the id is unknown.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    _registry, info = result
    return JSONResponse(
        {
            "subsession_id": info.id,
            "transcript": [entry.as_dict() for entry in info.transcript],
        }
    )


async def subsessions_message_endpoint(request: Request) -> JSONResponse:
    """Queue a user message for a running subsession.

    ``POST /subsessions/{sub_id}/message`` with body ``{"text": "..."}``
    enqueues the message (role ``"user"``) for delivery at the
    subsession's next turn boundary and returns 202
    ``{"subsession_id": ..., "status": "queued"}``.

    Returns 400 for a missing/empty ``text``, 404 for an unknown id, and
    409 when the subsession is no longer active.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    registry, info = result

    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body
    text = body.get("text")
    if not text or not isinstance(text, str):
        return JSONResponse(
            {"error": "'text' field is required and must be a non-empty string"},
            status_code=400,
        )

    if not registry.enqueue_message(info.id, "user", text):
        return JSONResponse(
            {"error": "subsession is not active", "subsession_id": info.id},
            status_code=409,
        )
    return JSONResponse({"subsession_id": info.id, "status": "queued"}, status_code=202)


async def subsessions_close_endpoint(request: Request) -> JSONResponse:
    """Close a subsession from the UI (user-initiated external close).

    ``POST /subsessions/{sub_id}/close`` cancels the worker, marks the
    subsession closed, delivers a best-effort summary to its parent
    conversation, and returns ``{"subsession_id": ..., "closed": true,
    "summary": "..."}``.

    Idempotent: an already-terminal subsession returns 200 with
    ``"closed": false`` and its current status.  404 for an unknown id.
    """
    result = _resolve_subsession(request)
    if isinstance(result, JSONResponse):
        return result
    registry, info = result

    closed = registry.cancel_and_close(
        info.id, reason="closed by user", closed_by="user"
    )
    if closed is None:
        return JSONResponse(
            {
                "subsession_id": info.id,
                "closed": False,
                "status": info.status.value,
            }
        )

    delivery: ParentDelivery | None = request.app.state.subsession_delivery
    if delivery is not None:
        await delivery.deliver_summary(
            closed, closed.summary or "", closed.close_reason or "closed"
        )
    return JSONResponse(
        {"subsession_id": info.id, "closed": True, "summary": closed.summary}
    )


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
    body = await _parse_json_body(request)
    if isinstance(body, JSONResponse):
        return body

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

    ``DELETE /sessions/{session_id}?owner_id=...`` closes every subsession
    owned by the session, deletes the session and its history, and returns::

        {
          "deleted": true,
          "active_session_id": "...",   # the owner's new active session
          "subsessions_closed": 1
        }

    ``owner_id`` is required (query param).  Returns 404 when the session is
    not found / not owned by *owner_id*.  Closing subsessions is best-effort
    and runs even when the conversation delete is a no-op (so orphaned work
    can still be cleaned up).
    """
    session_id = request.path_params["session_id"]
    owner_id = request.query_params.get("owner_id")
    if not owner_id:
        return JSONResponse(
            {"error": "owner_id query parameter is required"}, status_code=400
        )

    # 1. Close the session's subsessions.
    subsessions_closed = _cleanup_session(session_id, request)

    # 2. Delete the conversation/session itself.
    store: ConversationStore = request.app.state.conversation_store
    result = store.delete_session(owner_id, session_id)

    if not result.get("deleted"):
        return JSONResponse(
            {
                "error": "session not found",
                "session_id": session_id,
                "subsessions_closed": subsessions_closed,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "deleted": True,
            "active_session_id": result.get("active_session_id", ""),
            "subsessions_closed": subsessions_closed,
        }
    )


async def sessions_close_endpoint(request: Request) -> JSONResponse:
    """Close (mark as closed) a session and stop its background work.

    ``POST /sessions/{session_id}/close?owner_id=...`` closes every
    subsession owned by the session, marks the session as ``closed``
    (preventing it from spawning new work), and returns::

        {
          "closed": true,
          "session_id": "...",
          "subsessions_closed": 1
        }

    ``owner_id`` is required (query param).  Returns 404 when the session is
    not found / not owned by *owner_id*.  Closing subsessions is best-effort
    and runs even when the session is not found (so orphaned work can still
    be cleaned up).

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

    # 1. Close the session's subsessions.
    subsessions_closed = _cleanup_session(session_id, request)

    # 2. Mark the session as closed in the conversation store.
    store: ConversationStore = request.app.state.conversation_store
    result = store.close_session(owner_id, session_id)

    if not result.get("closed"):
        return JSONResponse(
            {
                "error": "session not found",
                "session_id": session_id,
                "subsessions_closed": subsessions_closed,
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "closed": True,
            "session_id": session_id,
            "subsessions_closed": subsessions_closed,
        }
    )


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


async def not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unmatched routes instead of plain text."""
    return JSONResponse({"error": "not found"}, status_code=404)


async def server_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return JSON for unhandled server errors."""
    return JSONResponse({"error": "internal server error"}, status_code=500)
