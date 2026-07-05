"""Generic base class for JSON-persisted dataclass stores.

Provides atomic-write persistence and tolerant load behaviour shared
by the diagnostic, knowledge, and fix-proposal stores.  Subclasses
define ``_to_dict`` and ``_from_dict`` serialisation hooks and inherit
the constructor, ``_persist``, and ``_load``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonStoreBase[T]:
    """Generic base for JSON-persisted dataclass stores.

    Subclasses define ``_to_dict`` and ``_from_dict`` serialisation
    hooks and inherit the atomic-write persistence and tolerant load
    behaviour.
    """

    # Override in subclasses for descriptive log messages.
    _store_name: str = "store"

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a store persisting to *path*.

        *clock* overrides the timestamp source so tests can pin time.
        """
        self._path = Path(path)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._init_storage()
        self._load()

    # ------------------------------------------------------------------
    # hooks — subclasses MUST override these
    # ------------------------------------------------------------------

    def _to_dict(self, item: T) -> dict[str, object]:
        """Convert *item* to a JSON-serialisable dict."""
        raise NotImplementedError

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> T:
        """Construct an item from a deserialised dict."""
        raise NotImplementedError

    def _item_key(self, item: T) -> str:
        """Return the storage key for *item* (default: ``item.id``)."""
        return getattr(item, "id", "")

    def _init_storage(self) -> None:
        """Initialise internal storage dict(s).

        Override in subclasses that manage more than one dict.
        """
        self._items: dict[str, T] = {}

    # ------------------------------------------------------------------
    # serialisation / deserialisation hooks
    # ------------------------------------------------------------------

    def _serialize(self) -> bytes:
        """Serialize all items to bytes. Default: flat JSON list."""
        entries = [self._to_dict(item) for item in self._items.values()]
        return json.dumps(entries, indent=2).encode("utf-8")

    def _deserialize(self, data: bytes) -> None:
        """Deserialize bytes into self._items. Default: flat JSON list."""
        raw = json.loads(data)
        if not isinstance(raw, list):
            return
        for item in raw:
            if not isinstance(item, dict):
                continue
            obj = self._from_dict(item)
            key = self._item_key(obj)
            if key:
                self._items[key] = obj

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Write all items to the JSON store (best-effort atomic)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("Could not create parent dir for %s", self._path)
            return

        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp_path.write_bytes(self._serialize())
            tmp_path.replace(self._path)
        except OSError:
            logger.exception("Failed to persist %s to %s", self._store_name, self._path)

    def _load(self) -> None:
        """Load items from disk; tolerate missing/empty/corrupt file."""
        if not self._path.exists():
            return
        try:
            data = self._path.read_bytes()
            self._deserialize(data)
        except json.JSONDecodeError, OSError:
            logger.warning(
                "Could not read %s file %s; starting empty",
                self._store_name,
                self._path,
            )
            return
