"""Generic base class for JSON-persisted dataclass stores.

Provides atomic-write persistence and tolerant load behaviour shared
by the diagnostic, knowledge, and fix-proposal stores.  Default
``_to_dict`` / ``_from_dict`` implementations use ``dataclasses.fields()``
to auto-generate serialisation for any dataclass type so subclasses
typically only need to set ``_default_path`` and ``_store_name``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import typing
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JsonStoreBase[T]:
    """Generic base for JSON-persisted dataclass stores.

    Default ``_to_dict`` / ``_from_dict`` implementations use
    ``dataclasses.fields()`` to serialise any dataclass type.  Subclasses
    typically only need to declare ``_default_path`` and ``_store_name``.
    """

    # Override in subclasses for descriptive log messages.
    _store_name: str = "store"

    # Override in subclasses to set the default persistence path.
    _default_path: str = "/data/store.json"

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Create a store persisting to *path* (defaults to ``_default_path``).

        *clock* overrides the timestamp source so tests can pin time.
        """
        self._path = Path(path if path is not None else self._default_path)
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._init_storage()
        self._load()

    # ------------------------------------------------------------------
    # hooks — subclasses may override for non-dataclass types
    # ------------------------------------------------------------------

    def _to_dict(self, item: T) -> dict[str, object]:
        """Convert *item* to a JSON-serialisable dict via dataclass fields."""
        return {
            f.name: getattr(item, f.name)
            for f in dataclasses.fields(item)  # type: ignore[arg-type]
        }

    @classmethod
    def _from_dict(cls, d: dict[str, Any]) -> T:
        """Construct an item from a deserialised dict via dataclass fields."""
        item_type = typing.get_args(
            cls.__orig_bases__[0]  # type: ignore[attr-defined]
        )[0]
        kwargs: dict[str, Any] = {}
        for f in dataclasses.fields(item_type):
            default = f.default if f.default is not dataclasses.MISSING else ""
            kwargs[f.name] = d.get(f.name, default)
        return item_type(**kwargs)  # type: ignore[no-any-return]

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
