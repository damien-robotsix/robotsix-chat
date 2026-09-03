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
from typing import Any

import httpx

from robotsix_chat.memory.base import RecoverCallback

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

    def status(self) -> dict[str, Any]:
        """Health snapshot for ``GET /health``."""
        degraded = self._consecutive_failures >= _DEGRADED_AFTER
        return {
            "backend": "memory-component",
            "degraded": degraded,
            "reason": (
                f"{self._consecutive_failures} consecutive recall failures"
                if degraded
                else None
            ),
            "consecutive_recall_failures": self._consecutive_failures,
        }

    def set_recovery_callback(
        self,
        callback: RecoverCallback | None,  # noqa: ARG002 — ChatMemory protocol
    ) -> None:
        """No recovery path — the component restarts itself via Docker."""
        return None

    def _note_failure(self, reason: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "memory recall failed (%s); continuing without memory (consecutive=%d)",
            reason,
            self._consecutive_failures,
        )


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
