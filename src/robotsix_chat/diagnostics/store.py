"""Diagnostic event store — persists captured diagnostic bundles to JSON.

A :class:`DiagnosticStore` persists structured diagnostic events to a single
JSON file on disk — default ``.data/diagnostics.json`` — with best-effort
atomic-ish writes.  On load it tolerates a missing, empty, or corrupt file
by starting empty.

This is the backing store for the diagnostic capture pipeline; it is
queried by :class:`~robotsix_chat.diagnostics.fixes.RecurrenceDetector`
to detect recurring failure categories.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class DiagnosticBundle:
    """A single captured diagnostic event.

    Attributes:
        id: Unique identifier (uuid4 hex).
        category: Failure category (e.g. ``CLONE_TARGET``, ``CI_FAILURE``).
        message: Human-readable description of the event.
        details: Optional free-form JSON-serializable dict with extra context.
        created_at: ISO-8601 timestamp of the event.

    """

    id: str
    category: str
    message: str
    details: dict[str, Any] | None = None
    created_at: str = ""


class DiagnosticStore:
    """Persist diagnostic bundles to ``.data/diagnostics.json`` (or custom path).

    Construct with an overridable ``path`` and ``clock`` injectable (defaults
    to ``datetime.now(timezone.utc)``) so tests can pin timestamps.

    Methods never raise unhandled exceptions — they return sentinel values
    or log warnings on persistence failures.
    """

    def __init__(
        self,
        path: str | Path = ".data/diagnostics.json",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a store persisting to *path*.

        *clock* overrides the timestamp source so tests can pin time.
        """
        self._path = Path(path)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._events: dict[str, DiagnosticBundle] = {}
        self._load()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def record_event(
        self,
        category: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> DiagnosticBundle:
        """Record a new diagnostic event; returns the bundle."""
        bundle = DiagnosticBundle(
            id=uuid.uuid4().hex,
            category=category,
            message=message,
            details=details,
            created_at=self._clock().isoformat(),
        )
        self._events[bundle.id] = bundle
        self._persist()
        return bundle

    def list_events(self, category: str = "") -> list[DiagnosticBundle]:
        """Return all events, optionally filtered by *category*."""
        if not category:
            return list(self._events.values())
        cat = category.strip().lower()
        return [e for e in self._events.values() if e.category.strip().lower() == cat]

    def get_event(self, event_id: str) -> DiagnosticBundle | None:
        """Return the event for *event_id*, or ``None`` if unknown."""
        return self._events.get(event_id)

    def events_since(
        self, since: datetime, category: str = ""
    ) -> list[DiagnosticBundle]:
        """Return events on or after *since*, optionally filtered by category."""
        result: list[DiagnosticBundle] = []
        for e in self._events.values():
            if category and e.category.strip().lower() != category.strip().lower():
                continue
            try:
                ts = datetime.fromisoformat(e.created_at)
            except (ValueError, TypeError):
                continue
            if ts >= since:
                result.append(e)
        return result

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write all events to the JSON store (best-effort atomic)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create parent dir for %s", self._path)
            return

        entries = [
            {
                "id": e.id,
                "category": e.category,
                "message": e.message,
                "details": e.details,
                "created_at": e.created_at,
            }
            for e in self._events.values()
        ]
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except OSError:
            logger.exception("Failed to persist diagnostic store to %s", self._path)

    def _load(self) -> None:
        """Load events from disk; tolerate missing/empty/corrupt file."""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Could not read diagnostic store file %s; starting empty",
                self._path,
            )
            return

        if not isinstance(raw, list):
            return

        for item in raw:
            if not isinstance(item, dict):
                continue
            bundle = DiagnosticBundle(
                id=item.get("id", ""),
                category=item.get("category", ""),
                message=item.get("message", ""),
                details=item.get("details"),
                created_at=item.get("created_at", ""),
            )
            if bundle.id:
                self._events[bundle.id] = bundle
