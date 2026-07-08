"""Diagnostic event store — persists captured diagnostic bundles to JSON.

A :class:`DiagnosticStore` persists structured diagnostic events to a single
JSON file on disk — default ``/data/diagnostics.json`` — with best-effort
atomic-ish writes.  On load it tolerates a missing, empty, or corrupt file
by starting empty.

This is the backing store for the diagnostic capture pipeline; it is
queried by :class:`~robotsix_chat.diagnostics.fixes.RecurrenceDetector`
to detect recurring failure categories.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from robotsix_chat.common.json_store import JsonStoreBase

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


class DiagnosticStore(JsonStoreBase[DiagnosticBundle]):
    """Persist diagnostic bundles to ``/data/diagnostics.json`` (or custom path).

    Construct with an overridable ``path`` and ``clock`` injectable (defaults
    to ``datetime.now(timezone.utc)``) so tests can pin timestamps.

    Methods never raise unhandled exceptions — they return sentinel values
    or log warnings on persistence failures.
    """

    _store_name = "diagnostic store"
    _default_path = "/data/diagnostics.json"

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
        self._items[bundle.id] = bundle
        self._persist()
        return bundle

    def list_events(self, category: str = "") -> list[DiagnosticBundle]:
        """Return all events, optionally filtered by *category*."""
        if not category:
            return list(self._items.values())
        cat = category.strip().lower()
        return [e for e in self._items.values() if e.category.strip().lower() == cat]

    def get_event(self, event_id: str) -> DiagnosticBundle | None:
        """Return the event for *event_id*, or ``None`` if unknown."""
        return self._items.get(event_id)

    def events_since(
        self, since: datetime, category: str = ""
    ) -> list[DiagnosticBundle]:
        """Return events on or after *since*, optionally filtered by category."""
        result: list[DiagnosticBundle] = []
        for e in self._items.values():
            if category and e.category.strip().lower() != category.strip().lower():
                continue
            try:
                ts = datetime.fromisoformat(e.created_at)
            except ValueError, TypeError:
                continue
            if ts >= since:
                result.append(e)
        return result
