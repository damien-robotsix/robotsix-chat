"""Chat endpoint — accepts a chat message and streams the agent reply as SSE."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Protocol

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from robotsix_chat.chat.actions import collect_actions
from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.summarize import generate_idle_summary

from ._shared import (
    _detect_truncation,
    _parse_json_body,
    _sse_frame,
    build_transcript,
)
from .constants import (
    SSE_CONTENT_TYPE,
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_HEARTBEAT_FRAME,
    SSE_HEARTBEAT_INTERVAL,
    SSE_TOKEN_TYPE,
)
from .errors import curated_stream_error

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
        trace_metadata: dict[str, str] | None = None,
        trace_name: str | None = None,
        model_level: int | None = None,
    ) -> AsyncIterator[str]:
        """Yield tokens from the LLM in response to ``message``.

        *images* is an optional list of ``(media_type, raw_bytes)`` pairs
        representing attached images (e.g. ``[("image/png", b"...")]``).
        *trace_metadata* is an optional dict of key-value attributes
        stamped onto the Langfuse trace span for observability (e.g.
        ``{"parent_session_id": "..."}``).
        *trace_name* is an optional human-readable label for the Langfuse
        trace (e.g. ``"chat-turn"``, ``"subsession-turn"``) so cost can be
        attributed by function.
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

#: One queued SSE frame: ``(type, payload)``. The payload is the token text for
#: ``SSE_TOKEN_TYPE``, ``None`` for ``SSE_DONE_TYPE``, and the curated error
#: mapping (``code``/``message``/``correlation_id``) for ``SSE_ERROR_TYPE``.
type SSEQueueFrame = tuple[str, str | dict[str, str] | None]


@dataclasses.dataclass
class _PendingMessage:
    """A single user message waiting to be batched."""

    message: str
    images: list[tuple[str, bytes]] | None
    message_id: str | None
    response_queue: asyncio.Queue[SSEQueueFrame]


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
        # Strong references to in-flight processor tasks. asyncio only
        # holds a weak reference to a task once created — without this,
        # the task backing an agent run can be garbage-collected mid-run
        # (e.g. when the user switches sessions or reloads, freeing other
        # objects and triggering a GC pass), silently aborting the run
        # before store.record() ever persists the reply. See the asyncio
        # docs' own warning on create_task() for this exact pitfall.
        self._background_tasks: set[asyncio.Task[None]] = set()

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
        summary_agent: ChatAgent | None = None,
        autonomous_runner: Any = None,
        event_bus: Any = None,  # EventBus | None (lazy typing to avoid cycle)
    ) -> asyncio.Queue[SSEQueueFrame]:
        """Submit a message for batching; return a queue of SSE frames.

        The caller reads ``(type, payload)`` tuples from the returned
        queue and streams them as SSE frames.  The queue receives
        ``SSE_DONE_TYPE`` at completion or ``SSE_ERROR_TYPE`` on failure.

        When *event_bus* is given, the turn is also mirrored onto the
        /events channel (``chat_turn_started`` / ``chat_token`` /
        ``chat_turn_done``) so other views can re-attach live.
        """
        response_queue: asyncio.Queue[SSEQueueFrame] = asyncio.Queue()
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
                task = asyncio.create_task(
                    self._process_batch(
                        session_id,
                        agent,
                        store,
                        run_serializer,
                        msg_id_store,
                        lock_key,
                        owner_id,
                        had_session,
                        summary_agent,
                        autonomous_runner,
                        event_bus,
                    )
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

        return response_queue

    async def cancel_message(
        self,
        session_id: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel pending (not-yet-processing) messages for *session_id*.

        When *message_id* is given, only that specific message is cancelled.
        When ``None``, every pending message in the session's batch is
        cancelled (bulk cancel).

        Returns a dict:
            ``{"cancelled": N}`` — *N* messages were removed.
            ``{"cancelled": 0, "processing": True}`` — the batch is already
                being processed; no messages can be cancelled.
        """
        async with self._guard:
            batch = self._batches.get(session_id)
            if batch is None:
                # No batch at all — either already drained into processing
                # or never existed.
                return {"cancelled": 0, "processing": True}

            if message_id is None:
                # Bulk cancel: remove every pending message.
                count = len(batch)
                self._batches.pop(session_id, None)
                for p in batch:
                    await p.response_queue.put((SSE_DONE_TYPE, None))
                return {"cancelled": count}

            # Per-message cancel: find and remove one.
            for idx, p in enumerate(batch):
                if p.message_id == message_id:
                    batch.pop(idx)
                    await p.response_queue.put((SSE_DONE_TYPE, None))
                    if not batch:
                        self._batches.pop(session_id, None)
                    return {"cancelled": 1}

            # message_id not found in the batch.
            return {"cancelled": 0, "processing": True}

    async def close(self, *, timeout: float = 5.0) -> None:
        """Cancel and drain all in-flight processor tasks.

        Called during graceful shutdown so pending agent runs are not
        abandoned — avoids "Task was destroyed but it is pending" warnings
        and gives each run a bounded window to persist its store record
        and finish its trace before the event loop tears down.

        *timeout* caps the total drain wait (matches uvicorn's
        ``timeout_graceful_shutdown`` default).
        """
        if not self._background_tasks:
            return
        for task in self._background_tasks:
            task.cancel()
        _, _pending = await asyncio.wait(
            self._background_tasks,
            timeout=timeout,
            return_when=asyncio.ALL_COMPLETED,
        )
        # Any task still pending after the timeout is truly abandoned —
        # the event loop is about to tear down regardless.
        self._background_tasks.clear()

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
        summary_agent: ChatAgent | None = None,
        autonomous_runner: Any = None,
        event_bus: Any = None,
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

            # An operator turn reopens a previously closed session: the
            # conversation is live again, so the agent may spawn/steer
            # subsessions regardless of any earlier operator close.  Only
            # the operator-driven chat path reopens — background drivers
            # (e.g. the autonomous runner) record turns without it, so a
            # closed session that nobody messages stays closed.
            if had_session:
                store.reopen_session(session_id)
                # The same turn reopens an autonomous session.  Its run
                # completes within minutes but the card stays on screen for
                # the rest of the trigger interval, so the session the
                # operator opens and talks to is usually one the runner has
                # already marked completed — i.e. one its next restart fire
                # would retire and replace mid-conversation.
                if autonomous_runner is not None:
                    autonomous_runner.note_operator_turn(session_id)

            # Idempotency check on the first pending message's message_id.
            first_msg = pending[0]
            if first_msg.message_id and session_id:
                existing = msg_id_store.get_reply(session_id, first_msg.message_id)
                if existing is not None:
                    for p in pending:
                        await p.response_queue.put((SSE_TOKEN_TYPE, existing))
                        await p.response_queue.put((SSE_DONE_TYPE, None))
                    return

            # Mirror the turn onto the /events channel so a non-originating
            # view (second tab, or a tab that switched away and back) can
            # re-attach live. The originating POST request still renders from
            # its own response body and ignores the /events echo.
            publish_turn = event_bus is not None and bool(session_id)
            turn_id = uuid.uuid4().hex if publish_turn else ""
            if publish_turn:
                event_bus.begin_turn(session_id, turn_id)

            reply_parts: list[str] = []
            try:
                # Collect the turn's tool calls (ticket filed, PR merged,
                # subsession spawned, ...) so they are persisted next to the
                # reply and visible to the compaction summariser later.
                with collect_actions() as turn_actions:
                    async for token in agent.stream(
                        concatenated,
                        history=current_history,
                        session_id=session_id,
                        client_id=session_id,
                        images=combined_images,
                        trace_name="chat-turn",
                        # A session the agent escalated runs at its pinned
                        # level; None leaves the agent on its configured one.
                        model_level=store.get_model_level(session_id),
                    ):
                        reply_parts.append(token)
                        for p in pending:
                            await p.response_queue.put((SSE_TOKEN_TYPE, token))
                        if publish_turn:
                            event_bus.append_turn_token(session_id, turn_id, token)

                full_reply = "".join(reply_parts)

                # Detect LLM output-length truncation and append a
                # human-visible note so the user knows the list may be
                # incomplete and can request the rest.
                truncation_note = _detect_truncation(full_reply)
                if truncation_note is not None:
                    full_reply += truncation_note
                    for p in pending:
                        await p.response_queue.put((SSE_TOKEN_TYPE, truncation_note))
                    if publish_turn:
                        event_bus.append_turn_token(
                            session_id, turn_id, truncation_note
                        )

                if session_id:
                    store.record(
                        session_id,
                        owner_id,
                        concatenated,
                        full_reply,
                        actions=turn_actions,
                    )
                    # Generate an LLM title after the first turn.
                    await self._maybe_generate_title(
                        session_id, summary_agent, concatenated, full_reply, store
                    )
                    for p in pending:
                        if p.message_id:
                            msg_id_store.mark_completed(
                                session_id, p.message_id, full_reply
                            )

                    # Scan autonomous session replies for lifecycle markers.
                    if autonomous_runner is not None:
                        autonomous_runner.check_reply_for_markers(
                            session_id,
                            full_reply,
                        )

                await self._fan_out(pending, SSE_DONE_TYPE)
                if publish_turn:
                    event_bus.end_turn(session_id, turn_id, timestamp=time.time())
            except asyncio.CancelledError:
                if publish_turn:
                    event_bus.end_turn(session_id, turn_id, error="cancelled")
                raise
            except Exception as exc:
                # str(exc) stays server-side only: it routinely embeds paths,
                # upstream URLs and provider error bodies, and both sinks below
                # fan out to every client watching the session.
                error = curated_stream_error(exc, fallback_id=turn_id)
                logger.exception(
                    "Agent stream error (code=%s correlation_id=%s)",
                    error["code"],
                    error["correlation_id"],
                )
                await self._fan_out(pending, SSE_ERROR_TYPE, error)
                if publish_turn:
                    event_bus.end_turn(session_id, turn_id, error=error["message"])

    async def _maybe_generate_title(
        self,
        session_id: str,
        summary_agent: ChatAgent | None,
        concatenated: str,
        full_reply: str,
        store: ConversationStore,
    ) -> str:
        """Generate an LLM title after the first turn, if conditions are met.

        Returns the title string, or empty if generation is skipped or fails.
        """
        if summary_agent is None or not concatenated.strip() or not full_reply.strip():
            return ""
        session = store.get_session(session_id)
        if session is None or session.turn_count != 1:
            return ""
        title = await _generate_title(summary_agent, concatenated, full_reply)
        if title:
            store.set_title(session_id, title)
        return title

    @staticmethod
    async def _fan_out(
        pending: list[_PendingMessage],
        event_type: str,
        payload: str | dict[str, str] | None = None,
    ) -> None:
        """Put an SSE frame onto every pending response queue."""
        for p in pending:
            await p.response_queue.put((event_type, payload))


def _parse_and_validate_images(
    body: dict[str, Any],
    max_per_msg: int,
    max_bytes: int,
    allowed_types: list[str],
) -> list[tuple[str, bytes]] | None:
    """Parse and validate the ``images`` field from a chat request body.

    Returns the list of ``(media_type, raw_bytes)`` tuples on success,
    or ``None`` when the body has no ``images`` key.  Raises
    ``HTTPException(400)`` on any validation failure.
    """
    raw_images = body.get("images")
    if raw_images is None:
        return None

    if not isinstance(raw_images, list):
        raise HTTPException(status_code=400, detail="'images' must be a JSON array")
    if len(raw_images) > max_per_msg:
        raise HTTPException(
            status_code=400,
            detail=f"too many images: got {len(raw_images)}, maximum {max_per_msg}",
        )

    images: list[tuple[str, bytes]] = []
    for idx, img in enumerate(raw_images):
        if not isinstance(img, dict):
            raise HTTPException(
                status_code=400,
                detail=f"images[{idx}]: expected a JSON object",
            )
        media_type = img.get("media_type")
        if not isinstance(media_type, str) or not media_type:
            raise HTTPException(
                status_code=400,
                detail=f"images[{idx}]: missing or invalid 'media_type'",
            )
        if media_type not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"images[{idx}]: media_type {media_type!r} not "
                    f"allowed (allowed: {allowed_types})"
                ),
            )
        data_b64 = img.get("data")
        if not isinstance(data_b64, str) or not data_b64:
            raise HTTPException(
                status_code=400,
                detail=f"images[{idx}]: missing or invalid 'data'",
            )
        try:
            raw_bytes = base64.b64decode(data_b64, validate=True)
        except Exception:
            raise HTTPException(
                status_code=400,
                detail=f"images[{idx}]: 'data' is not valid base64",
            ) from None
        if len(raw_bytes) > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"images[{idx}]: decoded size {len(raw_bytes)} "
                    f"exceeds maximum {max_bytes}"
                ),
            )
        images.append((media_type, raw_bytes))

    return images


async def _stream_summary(agent: ChatAgent, prompt: str, error_msg: str) -> str:
    """Stream *agent* on *prompt* and return the joined result, or "" on failure."""
    try:
        reply_parts: list[str] = []
        async for token in agent.stream(
            prompt,
            history=None,
            session_id=None,
            client_id=None,
            trace_name="chat-summary",
        ):
            reply_parts.append(token)
        return "".join(reply_parts).strip()
    except Exception:
        logger.exception(error_msg)
        return ""


async def _generate_title(
    summary_agent: ChatAgent,
    user_message: str,
    assistant_reply: str,
) -> str:
    """Generate a short 3-5 word title for a new conversation.

    Returns an empty string on failure.
    """
    if not user_message.strip():
        return ""

    # Keep the prompt small — the first exchange is usually short.
    user_snippet = user_message[:500]
    asst_snippet = assistant_reply[:500] if assistant_reply else ""
    prompt = (
        "Write a very short title (3-5 words) that summarizes what the "
        "user is asking about below. Reply with ONLY the title — no "
        "quotes, no punctuation, no extra text.\n\n"
        f"User: {user_snippet}\n"
        f"Assistant: {asst_snippet}\n\n"
        "Title:"
    )

    title = await _stream_summary(summary_agent, prompt, "Title generation failed")
    if not title:
        return ""
    # Clean up common LLM artifacts.
    title = title.strip('"').strip("'").rstrip(".")
    # Truncate to a reasonable length.
    if len(title) > 80:
        title = title[:80].rstrip() + "\u2026"
    return title


async def _generate_idle_summary(
    summary_agent: ChatAgent,
    turns: list[tuple[str, str]],
    actions: list[list[str]] | None = None,
) -> str:
    """Generate the structured compaction summary of *turns*.

    *actions* is the per-turn actions log aligned with *turns* (see
    :meth:`ConversationStore.agent_history_actions`) so the summariser sees
    the steps performed, not only the replies.  Long transcripts are
    summarised map-reduce style — see
    :func:`robotsix_chat.chat.summarize.generate_idle_summary`.

    Returns an empty string when there are no turns or on failure.
    """

    async def _run(prompt: str) -> str:
        return await _stream_summary(
            summary_agent, prompt, "Idle-timeout summary generation failed"
        )

    return await generate_idle_summary(_run, turns, actions)


async def _generate_carryover_summary(
    summary_agent: ChatAgent,
    turns: list[tuple[str, str]],
) -> str:
    """Generate a brief action-plan summary for cross-session carryover.

    Focuses on what the assistant was planning to do next — actionable
    items, pending tasks, blocked items, and next steps.  Returns an
    empty string when there are no turns or on failure.
    """
    if not turns:
        return ""

    transcript = build_transcript(turns)

    prompt = (
        "Generate a brief action-plan summary from the conversation below. "
        "Focus on: specific action items the assistant mentioned, pending "
        "tasks, blocked items waiting for input, and any multi-step "
        "processes in progress. This will be shown to the assistant at "
        "the start of the next session, so make it actionable and "
        "concrete — include task IDs, ticket IDs, PR URLs, subsession IDs, "
        "file paths, and next steps the assistant should pick up. "
        "Preserve these identifiers verbatim so they can be used directly "
        "without re-deriving them. Plain text only, no markdown fences, "
        "no JSON.\n\nConversation:\n"
        f"{transcript}\n\nAction-plan summary:"
    )

    return await _stream_summary(
        summary_agent, prompt, "Carryover summary generation failed"
    )


async def chat_endpoint(
    request: Request,
) -> JSONResponse | StreamingResponse:
    """Accept a chat message and stream the agent's response as SSE."""
    agent: ChatAgent = request.app.state.agent
    store: ConversationStore = request.app.state.conversation_store

    # -- parse & validate JSON body ---------------------------------------
    body = await _parse_json_body(request)

    message = body.get("message")
    if message is not None and not isinstance(message, str):
        raise HTTPException(
            status_code=400, detail="message must be a string when present"
        )

    # -- parse & validate message_id (optional) ---------------------------
    message_id = body.get("message_id")
    if message_id is not None and not isinstance(message_id, str):
        raise HTTPException(status_code=400, detail="invalid 'message_id' field")
    if message_id is not None and len(message_id) > 128:
        raise HTTPException(
            status_code=400, detail="'message_id' exceeds maximum length"
        )

    # -- parse & validate images (optional) -------------------------------
    images = _parse_and_validate_images(
        body,
        max_per_msg=request.app.state.max_images_per_message,
        max_bytes=request.app.state.max_image_bytes,
        allowed_types=request.app.state.allowed_image_media_types,
    )

    # -- require at least one of message or images -----------------------
    if not message and not images:
        raise HTTPException(
            status_code=400,
            detail="either 'message' or at least one image is required",
        )
    if not message:
        message = ""

    # Reset the continuation guardrail counter on every operator-initiated
    # message so auto-continuations do not accumulate across normal sessions.
    continuation_store = getattr(request.app.state, "continuation_store", None)
    if continuation_store is not None:
        continuation_store.reset_consecutive()

    # Resolve session identity — accept session_id + owner_id (new) or
    # client_id (legacy fallback: client_id becomes both owner and session).
    session_id = body.get("session_id")
    owner_id = body.get("owner_id")
    client_id = body.get("client_id")

    if client_id is not None and not isinstance(client_id, str):
        raise HTTPException(status_code=400, detail="invalid 'client_id' field")
    if session_id is not None and not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="invalid 'session_id' field")
    if owner_id is not None and not isinstance(owner_id, str):
        raise HTTPException(status_code=400, detail="invalid 'owner_id' field")

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

    # LEGACY reroute: sessions compacted by the old design carry a
    # ``compacted_into`` pointer to the continuation session they were
    # replaced with — a client still posting to such an id is routed to the
    # live end of the chain.  In-place compaction never sets the pointer, so
    # this only fires for pre-existing persisted chains.
    resolved_session_id = store.resolve_session(session_id)
    if resolved_session_id != session_id:
        logger.info(
            "Session %s was compacted — routing message to continuation %s",
            session_id,
            resolved_session_id,
        )
        session_id = resolved_session_id

    # -- idle-timeout compaction (in place) --------------------------------
    # The session keeps its id: turns before this point are replaced by a
    # summary in the agent's replay, the UI transcript and the subsession
    # tree are untouched.  Skipped entirely for conversations with fewer
    # than ``compaction_min_turns`` fresh (not-yet-summarized) turns, so an
    # empty or tiny conversation never churns the summary agent.

    idle_timeout_minutes: int = request.app.state.idle_timeout_minutes
    compaction_min_turns: int = request.app.state.compaction_min_turns
    compaction_keep_recent_turns: int = request.app.state.compaction_keep_recent_turns
    if had_session and idle_timeout_minutes > 0:
        idle_session = store.get_session(session_id)
        # The evergoing session is exempt: its memory policy is the
        # subject-aware trim scheduler (robotsix_chat.evergoing), which only
        # drops turns when the subject clearly changed. Idle compaction here
        # would fold the ONGOING subject into a summary after any >=idle-gap
        # pause, defeating the session's whole point of verbatim continuity.
        if idle_session is not None and not idle_session.evergoing:
            idle_seconds = time.time() - idle_session.wall_last_active
            fresh_turns = len(idle_session.turns) - idle_session.compacted_turn_index
            if (
                idle_seconds > idle_timeout_minutes * 60
                and fresh_turns >= compaction_min_turns
                and fresh_turns > compaction_keep_recent_turns
            ):
                compaction_turns = store.agent_history(session_id)
                summary = await _generate_idle_summary(
                    request.app.state.summary_agent,
                    compaction_turns,
                    store.agent_history_actions(session_id),
                )
                if summary:
                    store.compact_session(
                        owner_id or "",
                        session_id,
                        summary,
                        keep_recent_turns=compaction_keep_recent_turns,
                    )
                    folded_turns = fresh_turns - compaction_keep_recent_turns
                    logger.info(
                        "Idle timeout (%d min): compacted session %s in place "
                        "(%d turns folded into summary, %d kept verbatim)",
                        idle_timeout_minutes,
                        session_id,
                        folded_turns,
                        compaction_keep_recent_turns,
                    )

                # Schedule a feedback run for the compacted session.
                feedback_runner = request.app.state.feedback_runner
                if feedback_runner is not None:
                    feedback_runner.schedule("compaction", session_id, compaction_turns)

                # Save a carryover action-plan summary so the assistant
                # can pick up pending work if the operator starts a new
                # session instead of continuing this compacted one.
                await _persist_carryover_for_compaction(request, store, session_id)

    lock_key = client_id or session_id

    # -- Flush pending subsession outcomes at this natural breakpoint ------
    # Subsessions that closed while the user was away are held in the
    # delivery queue.  Deliver them as one consolidated summary now —
    # before the agent processes the new message — so the reply sees a
    # single consolidated update rather than N separate reaction turns.
    subsession_delivery = request.app.state.subsession_delivery
    if subsession_delivery is not None:
        await subsession_delivery.flush_pending_reactions(session_id)

    # -- Submit to the message coalescer ----------------------------------

    autonomous_runner = request.app.state.autonomous_runner
    coalescer: MessageCoalescer = request.app.state.message_coalescer
    # Only use summary_agent for title generation when it's a dedicated
    # (cheaper) agent — not when it's the fallback-to-main-agent default.
    title_agent = request.app.state.summary_agent
    if title_agent is agent:
        title_agent = None

    # -- session carryover injection ---------------------------------------
    # When starting a new session, check for a carryover note from a
    # previous session and prepend it to the user's message so the
    # agent can pick up pending work.
    if not had_session:
        carryover = _load_carryover(request)
        if carryover:
            message = _CARRYOVER_HEADER + carryover + _CARRYOVER_FOOTER + "\n" + message

    # -- live subsession state injection for status queries ----------------
    # When the user asks a status-like question, inject the current live
    # state of all subsessions so the agent grounds its reply in the
    # canonical registry rather than stale conversation history.
    subsession_registry = request.app.state.subsession_registry
    if subsession_registry is not None and session_id and _is_status_query(message):
        subs_state = _build_subsession_status_context(
            subsession_registry, owner_id or session_id
        )
        if subs_state:
            message = subs_state + "\n" + message

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
        summary_agent=title_agent,
        autonomous_runner=autonomous_runner,
        event_bus=request.app.state.event_bus,
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
                    # session_id lets the client adopt the continuation
                    # session when compaction (or a stale-id reroute)
                    # changed it mid-request.
                    yield _sse_frame(
                        {
                            "type": SSE_DONE_TYPE,
                            "session_id": session_id,
                            "timestamp": time.time(),
                        }
                    )
                    finished_normally = True
                    break
                else:  # SSE_ERROR_TYPE
                    # payload carries the curated code/message/correlation_id;
                    # tolerate a bare string so an older producer degrades to
                    # the pre-existing message-only frame instead of crashing.
                    fields = (
                        payload
                        if isinstance(payload, dict)
                        else {"message": payload or ""}
                    )
                    yield _sse_frame({"type": SSE_ERROR_TYPE, **fields})
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


async def cancel_queued_endpoint(request: Request) -> JSONResponse:
    """Cancel queued (not-yet-processing) messages for a session.

    ``POST /chat/queue/cancel``

    Request body (JSON):
        ``session_id`` (str, required) — the session whose queue to cancel
            from.
        ``message_id`` (str | null, optional) — cancel only this specific
            message.  When absent or ``null``, cancel **every** pending
            message in the session's coalescer batch.

    Returns:
        200 — ``{"cancelled": N}`` when *N* messages were removed from the
            coalescer batch before processing started.
        200 — ``{"cancelled": 0, "processing": True}`` when the batch (or
            the specific message) has already been handed off to the agent
            and can no longer be cancelled.

    Race-safe: the check-and-remove happens inside the coalescer's guard
    lock.  If the batch was popped between the check and the cancel, the
    response indicates "already processing".

    """
    body = await _parse_json_body(request)

    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise HTTPException(status_code=400, detail="session_id (string) is required")

    message_id = body.get("message_id")
    if message_id is not None and not isinstance(message_id, str):
        raise HTTPException(
            status_code=400,
            detail="message_id must be a string when present",
        )

    coalescer: MessageCoalescer = request.app.state.message_coalescer
    result = await coalescer.cancel_message(session_id, message_id)
    return JSONResponse(result)


# -- session carryover injection -------------------------------------------

_CARRYOVER_TOPIC = "session-carryover"

_CARRYOVER_HEADER = (
    "# Action plan from your previous session\n"
    "Below is what you were working on in your last session. "
    "Review it for pending actions, incomplete tasks, and next steps:\n\n"
)
_CARRYOVER_FOOTER = "\n\n# End of previous session action plan"


def _load_carryover(request: Request) -> str:
    """Return the carryover note content for the request's owner, or ``""``.

    The carryover note is a knowledge-store entry persisted on session
    close/delete/idle-compaction.  Returns the empty string when knowledge
    is disabled or no carryover note exists.
    """
    knowledge_store = request.app.state.knowledge_store
    if knowledge_store is None:
        return ""

    try:
        existing = knowledge_store.list(_CARRYOVER_TOPIC)
    except Exception:
        logger.exception("Failed to load carryover note")
        return ""

    if not existing:
        return ""

    return existing[0].content  # type: ignore[no-any-return]


async def _persist_carryover_for_compaction(
    request: Request,
    store: ConversationStore,
    session_id: str,
) -> None:
    """Generate and persist a carryover summary on idle compaction."""
    knowledge_store = request.app.state.knowledge_store
    if knowledge_store is None:
        return

    summary_agent: ChatAgent | None = request.app.state.summary_agent
    if summary_agent is None:
        return

    turns = store.history(session_id)
    if not turns:
        return

    try:
        summary = await _generate_carryover_summary(summary_agent, turns)
    except Exception:
        logger.exception(
            "Carryover summary generation failed for compacted session %s",
            session_id,
        )
        return

    if not summary:
        return

    try:
        existing = knowledge_store.list(_CARRYOVER_TOPIC)
        if existing:
            knowledge_store.update(existing[0].id, summary)
        else:
            knowledge_store.add(_CARRYOVER_TOPIC, summary)
    except Exception:
        logger.exception(
            "Failed to persist carryover note for compacted session %s",
            session_id,
        )


# -- live subsession state injection for status queries ---------------------

_STATUS_QUERY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bwhat(?:'s| is) (?:the |my )?status\b", re.IGNORECASE),
    re.compile(r"\bstatus(?:\?)?\s*$", re.IGNORECASE),
    re.compile(
        r"\bhow (?:is|are) (?:things|it|this|that|the )(?:going|doing)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bany (?:update|progress|news)\b", re.IGNORECASE),
    re.compile(r"\bwhere (?:are|do) (?:we|things|it) stand\b", re.IGNORECASE),
    re.compile(r"\bcurrent (?:status|state|progress)\b", re.IGNORECASE),
]


def _is_status_query(message: str) -> bool:
    """Return True when *message* looks like a status/progress question."""
    if not message:
        return False
    # Strip leading whitespace and quotes the injection prepends.
    text = message.lstrip()
    return any(pat.search(text) for pat in _STATUS_QUERY_PATTERNS)


_STATUS_CONTEXT_HEADER = (
    "=== INTERNAL METADATA — NOT part of the conversation ===\n"
    "The following is the live, canonical state of all background subsessions "
    "for this conversation, fetched from the registry at request time.  "
    "Use THIS data — not stale conversation history — when reporting status.  "
    "The user never saw this block; treat it as system context only.\n"
)

_STATUS_CONTEXT_FOOTER = "=== END INTERNAL METADATA ===\n"


def _build_subsession_status_context(
    registry: Any,  # SubsessionRegistry
    owner_session_id: str,
) -> str:
    """Return a fenced block of live subsession state, or ``""`` if none."""
    try:
        infos = registry.list_for_owner(owner_session_id)
    except Exception:
        logger.exception("Failed to list subsessions for status context")
        return ""

    if not infos:
        return ""

    lines: list[str] = []
    for info in infos:
        parts = [
            f"- {info.kind.value} '{info.title}' (id={info.id[:8]}) "
            f"status={info.status.value}",
        ]
        if info.summary:
            # Truncate long summaries to keep context small.
            summary = info.summary
            if len(summary) > 300:
                summary = summary[:297] + "..."
            parts.append(f"  summary: {summary}")
        if info.last_result:
            result = info.last_result
            if len(result) > 200:
                result = result[:197] + "..."
            parts.append(f"  last_result: {result}")
        if info.error:
            parts.append(f"  error: {info.error}")
        if info.close_reason:
            parts.append(f"  close_reason: {info.close_reason}")
        lines.append("\n".join(parts))

    return _STATUS_CONTEXT_HEADER + "\n".join(lines) + "\n" + _STATUS_CONTEXT_FOOTER
