"""ChatMemory backed by the robotsix-memory component.

Recall queries the component's ``GET /recall`` (Hindsight engine behind a
stable fleet contract) and renders the ranked memories into the context
block the agent appends at the end of the prompt. The write path is
deliberately a no-op here: durable memory is written by the evergoing
summary pipeline (:mod:`robotsix_chat.memory_push`) and by the agent
calling the component's skill explicitly — per-exchange auto-writes were
cognee's design and are retired with it.

Safe by construction: recall never raises into the chat request path — any
failure degrades to "no memory" and flips the status snapshot to degraded
after :data:`_DEGRADED_AFTER` consecutive failures.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from robotsix_chat.memory.base import NotifyCallback, RecoverCallback

logger = logging.getLogger(__name__)

#: Consecutive recall failures after which ``status()`` reports degraded.
_DEGRADED_AFTER = 3

#: Hard cap on the rendered recall block, mirroring the old backend's
#: guard against prompt bloat.
_MAX_BLOCK_CHARS = 6000


class ComponentMemory:
    """:class:`~robotsix_chat.memory.base.ChatMemory` via the memory component."""

    def __init__(
        self,
        url: str,
        *,
        owner_id: str = "operator",
        recall_limit: int = 8,
        timeout_seconds: float = 20.0,
    ) -> None:
        """Bind to the component at *url*, scoped to *owner_id*'s bank.

        Args:
            url: Base URL of the robotsix-memory component.
            owner_id: Memory scope for automatic recall. The chat is a
                single-operator system, so all automatic recall reads the
                operator bank; per-owner scoping stays available to the
                agent through the component's skill.
            recall_limit: Maximum ranked memories per recall.
            timeout_seconds: Per-recall HTTP timeout.

        """
        self._url = url.rstrip("/")
        self._owner_id = owner_id
        self._recall_limit = recall_limit
        self._timeout = timeout_seconds
        self._consecutive_failures = 0
        # Write-path (retain) health cached from GET /health/hindsight — the
        # component retains asynchronously, so a dead write path never
        # surfaces on recall. Refreshed by the periodic health run; "unknown"
        # (not failing) until first read.
        self._write_checked = False
        self._write_failing = False
        self._write_last_error: str | None = None
        # Operator-panel escalation: opened once per distinct last_error while
        # writes fail, closed when they recover.
        self._on_write_failing: Callable[[str], Awaitable[None]] | None = None
        self._on_write_recovered: Callable[[], Awaitable[None]] | None = None
        self._alerted_write_error: str | None = None

    async def setup(self) -> None:
        """Probe the component once so a dead memory is visible at boot."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self._url}/health")
            payload = resp.json() if resp.status_code < 500 else {}
            logger.info(
                "memory component at %s: %s", self._url, payload.get("hindsight", "?")
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("memory component unreachable at setup: %s", exc)

    async def recall(
        self,
        query: str,
        *,
        session_id: str | None = None,  # noqa: ARG002 — ChatMemory protocol
    ) -> str:
        """Return the rendered recall block for *query* (``""`` on any failure)."""
        if not query.strip():
            return ""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._url}/recall",
                    params={
                        "query": query[:2000],
                        "owner_id": self._owner_id,
                        "limit": self._recall_limit,
                    },
                )
        except httpx.HTTPError as exc:
            self._note_failure(f"recall request failed: {exc}")
            return ""
        if resp.status_code >= 400:
            self._note_failure(f"recall rejected: HTTP {resp.status_code}")
            return ""
        try:
            payload = resp.json()
        except ValueError:
            self._note_failure("recall returned non-JSON")
            return ""
        self._consecutive_failures = 0
        return _render_recall_block(payload)

    async def remember(
        self,
        user_message: str,  # noqa: ARG002 — ChatMemory protocol
        assistant_message: str,  # noqa: ARG002 — ChatMemory protocol
        *,
        session_id: str | None = None,  # noqa: ARG002 — ChatMemory protocol
    ) -> None:
        """No-op — the summary pipeline owns automatic writes."""
        return None

    async def recall_deep(
        self,
        query: str,
        *,
        session_id: str | None = None,  # noqa: ARG002 — tool-call parity
    ) -> str:
        """LLM-grounded deep memory search via the component's ``/reflect``.

        Slower than :meth:`recall` (the engine reasons over the bank), so it
        is exposed as an agent tool rather than run automatically. Returns
        ``""`` on any failure — same safe-by-construction contract.
        """
        if not query.strip():
            return ""
        try:
            async with httpx.AsyncClient(timeout=max(self._timeout, 120.0)) as client:
                resp = await client.post(
                    f"{self._url}/reflect",
                    json={"query": query[:2000], "owner_id": self._owner_id},
                )
        except httpx.HTTPError as exc:
            self._note_failure(f"reflect request failed: {exc}")
            return ""
        if resp.status_code >= 400:
            self._note_failure(f"reflect rejected: HTTP {resp.status_code}")
            return ""
        try:
            payload = resp.json()
        except ValueError:
            return ""
        self._consecutive_failures = 0
        reflection = payload.get("reflection") if isinstance(payload, dict) else None
        if isinstance(reflection, dict):
            for key in ("answer", "text", "reflection", "output"):
                val = reflection.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:_MAX_BLOCK_CHARS]
            return str(reflection)[:_MAX_BLOCK_CHARS]
        if isinstance(reflection, str):
            return reflection.strip()[:_MAX_BLOCK_CHARS]
        return ""

    async def refresh_write_health(self) -> dict[str, Any] | None:
        """Read ``GET /health/hindsight`` and cache the retain (write) path.

        robotsix-memory retains asynchronously, so a dead write path (e.g.
        the extraction LLM's balance exhausted) never surfaces on the recall
        path this backend already tracks, and ``POST /remember`` still
        answers 201. The component reports it on ``GET /health/hindsight``
        instead (top-level ``status`` + ``retain.status``/``retain.last_error``).
        Reads it so :meth:`status` and the periodic health run report a failing
        write path, and escalates to the operator on a transition.

        Safe by construction: never raises. An unreachable or malformed
        response leaves the cached state unchanged (write health is
        "unknown", not "failing"). Returns the raw parsed payload, or
        ``None`` when the probe did not yield a usable response.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self._url}/health/hindsight")
        except httpx.HTTPError as exc:
            logger.debug("memory write-health probe failed: %s", exc)
            return None
        if resp.status_code >= 500:
            logger.debug("memory write-health probe: HTTP %d", resp.status_code)
            return None
        try:
            payload = resp.json()
        except ValueError:
            logger.debug("memory write-health probe returned non-JSON")
            return None
        if not isinstance(payload, dict):
            return None
        self._write_failing, self._write_last_error = _parse_write_health(payload)
        self._write_checked = True
        await self._escalate_write_health()
        return payload

    async def _escalate_write_health(self) -> None:
        """Open/close the operator panel on write-health transitions.

        Opens ONE panel per distinct ``last_error`` while writes are failing
        (so the operator learns the cause without a model needing to notice),
        and closes it when writes recover. Best effort: a callback error never
        propagates into the health run.
        """
        try:
            if self._write_failing:
                cause = self._write_last_error or "unknown error"
                if self._alerted_write_error != cause:
                    self._alerted_write_error = cause
                    if self._on_write_failing is not None:
                        await self._on_write_failing(cause)
            elif self._write_checked and self._alerted_write_error is not None:
                self._alerted_write_error = None
                if self._on_write_recovered is not None:
                    await self._on_write_recovered()
        except Exception:
            logger.debug("memory write-health escalation failed", exc_info=True)

    def status(self) -> dict[str, Any]:
        """Health snapshot for ``GET /health``."""
        recall_degraded = self._consecutive_failures >= _DEGRADED_AFTER
        degraded = recall_degraded or self._write_failing
        reasons: list[str] = []
        if recall_degraded:
            reasons.append(f"{self._consecutive_failures} consecutive recall failures")
        if self._write_failing:
            reasons.append(
                "memory writes failing: " + (self._write_last_error or "unknown error")
            )
        return {
            "backend": "memory-component",
            "degraded": degraded,
            "reason": "; ".join(reasons) if reasons else None,
            "consecutive_recall_failures": self._consecutive_failures,
            "writes_failing": self._write_failing,
            "write_last_error": self._write_last_error,
        }

    def set_recovery_callback(
        self,
        callback: RecoverCallback | None,  # noqa: ARG002 — ChatMemory protocol
    ) -> None:
        """No recovery path — the component restarts itself via Docker."""
        return None

    def set_notify_callback(
        self,
        callback: NotifyCallback | None,  # noqa: ARG002 — ChatMemory protocol
    ) -> None:
        """No escalation path — component health is on GET /health."""
        return None

    def set_write_alert_callbacks(
        self,
        *,
        on_failing: Callable[[str], Awaitable[None]] | None,
        on_recovered: Callable[[], Awaitable[None]] | None,
    ) -> None:
        """Register (or clear) the write-failure operator-panel callbacks.

        ``on_failing(last_error)`` opens the panel; ``on_recovered()`` closes
        it. Invoked at most once per distinct ``last_error`` until writes
        recover — see :meth:`_escalate_write_health`.
        """
        self._on_write_failing = on_failing
        self._on_write_recovered = on_recovered

    def _note_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "memory recall failed (%s); continuing without memory (consecutive=%d)",
            reason,
            self._consecutive_failures,
        )


def _parse_write_health(payload: dict[str, Any]) -> tuple[bool, str | None]:
    """Return ``(writes_failing, last_error)`` from a ``/health/hindsight`` body.

    Writes are failing when the top-level ``status`` is ``degraded`` or the
    ``retain.status`` is ``failing``. ``last_error`` is the engine's message
    from the ``retain`` block when present.
    """
    retain = payload.get("retain")
    retain = retain if isinstance(retain, dict) else {}
    retain_status = str(retain.get("status") or "").lower()
    top_status = str(payload.get("status") or "").lower()
    failing = retain_status == "failing" or top_status == "degraded"
    last_error = retain.get("last_error")
    last_error = last_error if isinstance(last_error, str) and last_error else None
    return failing, last_error


def _render_recall_block(payload: Any) -> str:
    """Render the component's recall response into a compact context block.

    Expects ``{"results": {"results": [{"text": …, "type": …}, …],
    "entities": {name: {"observations": […]}}}`` (the component passes the
    engine response through). Unknown shapes render to ``""`` rather than
    leaking JSON into the prompt.
    """
    if not isinstance(payload, dict):
        return ""
    engine = payload.get("results")
    if not isinstance(engine, dict):
        return ""
    lines: list[str] = []
    results = engine.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            kind = str(item.get("type") or "").strip()
            lines.append(f"- [{kind}] {text}" if kind else f"- {text}")
    entities = engine.get("entities")
    if isinstance(entities, dict):
        obs_lines: list[str] = []
        for name, ent in entities.items():
            if not isinstance(ent, dict):
                continue
            for obs in ent.get("observations") or []:
                text = (
                    str(obs.get("text") or "").strip()
                    if isinstance(obs, dict)
                    else str(obs).strip()
                )
                if text:
                    obs_lines.append(f"- {name}: {text}")
        if obs_lines:
            lines.append("")
            lines.append("Consolidated observations:")
            lines.extend(obs_lines)
    block = "\n".join(lines).strip()
    if len(block) > _MAX_BLOCK_CHARS:
        block = block[:_MAX_BLOCK_CHARS] + "\n… (recall truncated)"
    return block
