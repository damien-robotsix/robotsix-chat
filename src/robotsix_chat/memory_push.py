"""Push conversation summaries to the robotsix-memory component.

The evergoing summary scheduler is the fleet's ONE summarising mechanism;
this module gives its output a second destination: the long-term memory
component (``/remember``). Each push carries a stable
``document_id`` (``chat-session-<id>``) with ``update_mode="replace"``, so
re-summarising the same conversation *supersedes* the facts previously
retained for it instead of piling up near-duplicates — the dedup story is
the engine's document semantics, not string matching here.

Writes are strictly best-effort: a memory outage must never break chat.
Failures are logged and dropped — the next compaction pass pushes a newer,
more complete summary of the same session anyway, so the write path is
self-healing on the scheduler's cadence.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

#: Tag stamped on every summary memory, for filtered recall on the other side.
SESSION_SUMMARY_TAG = "chat-session-summary"


def session_document_id(session_id: str) -> str:
    """Stable per-session document id — the dedup anchor."""
    return f"chat-session-{session_id}"


class MemoryPush:
    """Best-effort client for the robotsix-memory component's ``/remember``."""

    def __init__(self, url: str, *, timeout_seconds: float = 60.0) -> None:
        """Bind the client to the component's base *url*.

        Args:
            url: Base URL of the robotsix-memory component.
            timeout_seconds: Per-push HTTP timeout.

        """
        self._url = url.rstrip("/")
        self._timeout = timeout_seconds

    async def push_session_summary(
        self,
        *,
        owner_id: str,
        session_id: str,
        title: str,
        summary: str,
        final: bool = False,
    ) -> bool:
        """POST one session summary; returns True when stored.

        ``final=True`` marks the close-of-conversation push (same document,
        still ``replace`` — the last summary simply wins).
        """
        if not summary.strip():
            return False
        kind = "final summary" if final else "rolling summary"
        payload = {
            "content": summary,
            "owner_id": owner_id,
            "tags": [SESSION_SUMMARY_TAG],
            "context": f"{kind} of chat session '{title}' ({session_id})",
            "document_id": session_document_id(session_id),
            "update_mode": "replace",
            # The engine's fact extraction is a multi-second LLM pipeline;
            # background mode returns as soon as the item is queued (live
            # 2026-09-03T23:46Z: a synchronous push timed out at 60s).
            "background": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self._url}/remember", json=payload)
        except httpx.HTTPError as exc:
            logger.warning(
                "memory push failed for session %s (%s) — dropped, next "
                "compaction re-pushes a newer summary: %s",
                session_id,
                kind,
                exc,
            )
            return False
        if resp.status_code >= 400:
            logger.warning(
                "memory push rejected for session %s (%s): HTTP %d %s",
                session_id,
                kind,
                resp.status_code,
                resp.text[:200],
            )
            return False
        logger.info("memory push stored %s for session %s", kind, session_id)
        return True

    def schedule(
        self,
        *,
        owner_id: str,
        session_id: str,
        title: str,
        summary: str,
        final: bool = False,
    ) -> None:
        """Fire-and-forget push — never blocks or raises into the caller."""

        async def _run() -> None:
            try:
                await self.push_session_summary(
                    owner_id=owner_id,
                    session_id=session_id,
                    title=title,
                    summary=summary,
                    final=final,
                )
            except Exception:
                logger.exception("memory push task crashed for session %s", session_id)

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("no running loop — memory push for %s skipped", session_id)
            return
        task = asyncio.create_task(_run())
        # Keep a reference so the task is not garbage-collected mid-flight.
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


_background_tasks: set[asyncio.Task[None]] = set()
