"""Local, durable diagnostics store for captured ticket states.

A :class:`DiagnosticStore` persists :class:`DiagnosticRecord` entries and
ticket known-states to a single JSON file on disk — default
``.data/diagnostics.json`` — with best-effort atomic-ish writes. On load it
tolerates a missing, empty, or corrupt file by starting empty, and
forward-compatibly defaults missing keys to ``None``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class DiagnosticRecord:
    """A single captured diagnostic snapshot for a ticket."""

    ticket_id: str
    block_reason: str
    langfuse_trace: str
    ticket_history: str
    branch_pr_links: str
    clone_repo_info: str
    captured_at: str


class DiagnosticStore:
    """Persist diagnostic records to ``.data/diagnostics.json`` (or custom path).

    Construct with an overridable ``path`` and ``clock`` injectable (defaults
    to ``datetime.now(timezone.utc)``) so tests can pin timestamps.
    """

    def __init__(
        self,
        path: str | Path = ".data/diagnostics.json",
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a store persisting to *path*.

        *clock* overrides the timestamp source (default ``datetime.now(UTC)``)
        so tests can pin time deterministically.
        """
        self._path = Path(path)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._records: list[DiagnosticRecord] = []
        self._known_states: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # public API — records
    # ------------------------------------------------------------------

    def add(self, record: DiagnosticRecord) -> None:
        """Add a diagnostic record to the store and persist."""
        self._records.append(record)
        self._persist()

    def list(self) -> list[DiagnosticRecord]:
        """Return all stored diagnostic records."""
        return list(self._records)

    def get(self, ticket_id: str) -> DiagnosticRecord | None:
        """Return the most recent record for *ticket_id*, or ``None``."""
        for rec in reversed(self._records):
            if rec.ticket_id == ticket_id:
                return rec
        return None

    def has_ticket(self, ticket_id: str) -> bool:
        """Check whether any record exists for *ticket_id*."""
        return any(rec.ticket_id == ticket_id for rec in self._records)

    # ------------------------------------------------------------------
    # public API — known states
    # ------------------------------------------------------------------

    def get_known_state(self, ticket_id: str) -> str | None:
        """Return the last-known state for *ticket_id*, or ``None``."""
        return self._known_states.get(ticket_id)

    def set_known_state(self, ticket_id: str, state: str) -> None:
        """Record the current state of a ticket and persist."""
        self._known_states[ticket_id] = state
        self._persist()

    def get_known_states(self) -> dict[str, str]:
        """Return a copy of all known ticket states."""
        return dict(self._known_states)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write all records and known states to the JSON store (best-effort atomic)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create parent dir for %s", self._path)
            return

        data = {
            "records": [
                {
                    "ticket_id": r.ticket_id,
                    "block_reason": r.block_reason,
                    "langfuse_trace": r.langfuse_trace,
                    "ticket_history": r.ticket_history,
                    "branch_pr_links": r.branch_pr_links,
                    "clone_repo_info": r.clone_repo_info,
                    "captured_at": r.captured_at,
                }
                for r in self._records
            ],
            "known_states": dict(self._known_states),
        }

        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_path.replace(self._path)
        except OSError:
            logger.exception("Failed to persist diagnostics store to %s", self._path)

    def _load(self) -> None:
        """Load records and known states from disk.

        Tolerates missing/empty/corrupt file.
        """
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Could not read diagnostics store file %s; starting empty", self._path
            )
            return

        if not isinstance(raw, dict):
            return

        # Load records
        records_raw = raw.get("records")
        if isinstance(records_raw, list):
            for item in records_raw:
                if not isinstance(item, dict):
                    continue
                record = DiagnosticRecord(
                    ticket_id=item.get("ticket_id", ""),
                    block_reason=item.get("block_reason", ""),
                    langfuse_trace=item.get("langfuse_trace", ""),
                    ticket_history=item.get("ticket_history", ""),
                    branch_pr_links=item.get("branch_pr_links", ""),
                    clone_repo_info=item.get("clone_repo_info", ""),
                    captured_at=item.get("captured_at", ""),
                )
                if record.ticket_id:
                    self._records.append(record)

        # Load known states
        states_raw = raw.get("known_states")
        if isinstance(states_raw, dict):
            for k, v in states_raw.items():
                if isinstance(k, str) and isinstance(v, str):
                    self._known_states[k] = v
