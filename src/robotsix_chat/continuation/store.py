"""Durable store for pending post-restart continuations.

A :class:`ContinuationStore` persists a single pending continuation to a JSON
file on disk — default ``/data/continuation.json``.  It survives container
recreation and supports one-shot consumption with audit logging and a
consecutive-execution guardrail to prevent restart loops.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Maximum entries retained in the audit log.
_MAX_AUDIT_LOG_ENTRIES = 200


@dataclass
class _ContinuationState:
    """In-memory representation of the continuation store's state."""

    pending_session_id: str = ""
    pending_prompt: str = ""
    pending_created_at: str = ""
    consecutive_count: int = 0
    audit_log: list[dict[str, Any]] = field(default_factory=list)


class ContinuationStore:
    """Persist a pending continuation and fire it on the next boot.

    Thread-safe at the level of a single uvicorn worker: reads and writes
    are synchronous and the store is only ever touched from the main event
    loop.  Multiple workers would need file locking — not needed here.

    Args:
        path: Path to the JSON persistence file.  Defaults to
            ``/data/continuation.json``.
        max_consecutive: Maximum number of consecutive auto-continuations
            before the guardrail blocks and requires manual intervention.
            Default ``3``.

    """

    def __init__(
        self,
        path: str | Path = "/data/continuation.json",
        max_consecutive: int = 3,
    ) -> None:
        """Create a store persisting to *path*."""
        self._path = Path(path)
        self._max_consecutive = max_consecutive
        self._state = _ContinuationState()
        self._load()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    @property
    def consecutive_count(self) -> int:
        """Return the current consecutive auto-continuation count."""
        return self._state.consecutive_count

    def schedule(self, session_id: str, prompt: str) -> str:
        """Arm a continuation that fires after the next restart.

        Overwrites any previously pending continuation — only one can be
        pending at a time.

        Args:
            session_id: The session to continue after restart.
            prompt: The prompt to inject as if the operator sent it.

        Returns:
            A confirmation string.

        """
        now = datetime.now(UTC).isoformat()
        self._state.pending_session_id = session_id
        self._state.pending_prompt = prompt
        self._state.pending_created_at = now

        self._append_audit(
            "armed",
            session_id=session_id,
            prompt_preview=prompt[:200],
        )
        self._persist()
        logger.info(
            "Continuation armed: session_id=%s prompt_preview=%r",
            session_id,
            prompt[:80],
        )
        return (
            f"Continuation armed for session {session_id}. "
            "It will fire automatically after the next restart."
        )

    def cancel(self) -> str:
        """Cancel any pending continuation.

        Returns:
            A confirmation string.

        """
        if not self._state.pending_session_id:
            return "No pending continuation to cancel."
        sid = self._state.pending_session_id
        self._state.pending_session_id = ""
        self._state.pending_prompt = ""
        self._state.pending_created_at = ""
        self._append_audit("cancelled", session_id=sid)
        self._persist()
        logger.info("Continuation cancelled (was session_id=%s)", sid)
        return f"Pending continuation for session {sid} cancelled."

    def consume_pending(self) -> tuple[str | None, str | None]:
        """Return and consume the pending continuation, if any.

        Checks the guardrail: if ``consecutive_count >= max_consecutive``,
        the continuation is blocked, the pending entry is cleared, and an
        audit entry is recorded.  The operator must manually intervene to
        reset the counter.

        Returns:
            A ``(session_id, prompt)`` pair when a continuation is pending
            and the guardrail allows it, or ``(None, None)`` otherwise.

        """
        sid = self._state.pending_session_id
        if not sid:
            return None, None

        # Guardrail: block if we've hit the consecutive limit.
        if self._state.consecutive_count >= self._max_consecutive:
            logger.warning(
                "Continuation guardrail blocked: consecutive_count=%d >= "
                "max_consecutive=%d — clearing pending continuation "
                "(session_id=%s)",
                self._state.consecutive_count,
                self._max_consecutive,
                sid,
            )
            self._append_audit(
                "guardrail_blocked",
                session_id=sid,
                reason=(
                    f"consecutive_count ({self._state.consecutive_count}) "
                    f">= max_consecutive ({self._max_consecutive})"
                ),
            )
            self._state.pending_session_id = ""
            self._state.pending_prompt = ""
            self._state.pending_created_at = ""
            self._persist()
            return None, None

        prompt = self._state.pending_prompt
        self._state.pending_session_id = ""
        self._state.pending_prompt = ""
        self._state.pending_created_at = ""
        self._state.consecutive_count += 1

        self._append_audit("fired", session_id=sid)
        self._persist()
        logger.info(
            "Continuation fired: session_id=%s consecutive_count=%d",
            sid,
            self._state.consecutive_count,
        )
        return sid, prompt

    def reset_consecutive(self) -> None:
        """Reset the consecutive auto-continuation counter to zero.

        Call this when the operator manually interacts with the chat so
        the guardrail does not accumulate across normal sessions.
        """
        if self._state.consecutive_count > 0:
            logger.info(
                "Continuation consecutive count reset (was %d)",
                self._state.consecutive_count,
            )
            self._state.consecutive_count = 0
            self._append_audit("consecutive_reset")
            self._persist()

    def pending_info(self) -> dict[str, Any]:
        """Return a summary of the current pending continuation.

        Used for tool introspection (get_continuation_status).
        """
        if not self._state.pending_session_id:
            return {"pending": False}
        return {
            "pending": True,
            "session_id": self._state.pending_session_id,
            "prompt_preview": self._state.pending_prompt[:200],
            "created_at": self._state.pending_created_at,
            "consecutive_count": self._state.consecutive_count,
            "max_consecutive": self._max_consecutive,
        }

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _append_audit(self, event: str, **extra: Any) -> None:
        """Append an entry to the in-memory audit log."""
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event": event,
        }
        entry.update(extra)
        self._state.audit_log.append(entry)
        # Trim oldest entries when the log exceeds the cap.
        if len(self._state.audit_log) > _MAX_AUDIT_LOG_ENTRIES:
            self._state.audit_log = self._state.audit_log[-_MAX_AUDIT_LOG_ENTRIES:]

    def _persist(self) -> None:
        """Write state to the JSON file (best-effort atomic)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create parent dir for %s", self._path)

        data: dict[str, Any] = {
            "pending_session_id": self._state.pending_session_id,
            "pending_prompt": self._state.pending_prompt,
            "pending_created_at": self._state.pending_created_at,
            "consecutive_count": self._state.consecutive_count,
            "audit_log": self._state.audit_log,
        }
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except OSError:
            logger.exception("Failed to persist continuation store to %s", self._path)

    def _load(self) -> None:
        """Load state from disk; tolerate missing/empty/corrupt file."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            self._state.pending_session_id = raw.get("pending_session_id", "")
            self._state.pending_prompt = raw.get("pending_prompt", "")
            self._state.pending_created_at = raw.get("pending_created_at", "")
            self._state.consecutive_count = max(0, int(raw.get("consecutive_count", 0)))
            audit = raw.get("audit_log")
            if isinstance(audit, list):
                self._state.audit_log = audit
        except json.JSONDecodeError, OSError, ValueError:
            logger.warning(
                "Could not read continuation store %s; starting empty",
                self._path,
            )
