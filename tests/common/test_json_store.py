"""Unit tests for ``robotsix_chat.common.json_store.JsonStoreBase``.

Covers the persistence edge cases (``OSError`` branches in ``_persist``
and ``_load``) that are not reachable through the subclass integration
tests.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch

from robotsix_chat.common.json_store import JsonStoreBase


@dataclasses.dataclass
class _TestItem:
    """Minimal dataclass for JsonStoreBase tests."""

    id: str
    name: str


class _TestStore(JsonStoreBase[_TestItem]):
    """Concrete JsonStoreBase subclass — simple dict wrappers."""

    _store_name: str = "test-store"

    def _to_dict(self, item: _TestItem) -> dict[str, object]:
        return {"id": item.id, "name": item.name}

    @classmethod
    def _from_dict(cls, d: dict[str, object]) -> _TestItem:
        return _TestItem(id=str(d["id"]), name=str(d["name"]))

    def add(self, item: _TestItem) -> None:
        """Store an item and persist to disk."""
        self._items[item.id] = item
        self._persist()


# ---------------------------------------------------------------------------
# Happy-path round-trip
# ---------------------------------------------------------------------------


def test_round_trip(tmp_path: Path) -> None:
    """Items persisted to one store survive reload from a second store."""
    path = tmp_path / "test.json"

    store1 = _TestStore(path)
    store1.add(_TestItem(id="a", name="Alice"))

    store2 = _TestStore(path)
    assert "a" in store2._items
    assert store2._items["a"].name == "Alice"


# ---------------------------------------------------------------------------
# _persist OSError on mkdir
# ---------------------------------------------------------------------------


def test_persist_mkdir_oserror_does_not_crash(tmp_path: Path) -> None:
    """When parent.mkdir raises OSError, _persist logs and returns cleanly."""
    store = _TestStore(tmp_path / "sub" / "test.json")
    store._items["x"] = _TestItem(id="x", name="X")

    with patch.object(Path, "mkdir", side_effect=OSError("disk full")):
        store._persist()  # should not raise


# ---------------------------------------------------------------------------
# _persist OSError on write_text
# ---------------------------------------------------------------------------


def test_persist_write_oserror_does_not_crash(tmp_path: Path) -> None:
    """When write_bytes raises OSError, _persist logs and returns cleanly."""
    store = _TestStore(tmp_path / "test.json")
    store._items["x"] = _TestItem(id="x", name="X")

    with patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
        store._persist()  # should not raise


# ---------------------------------------------------------------------------
# _persist OSError on replace
# ---------------------------------------------------------------------------


def test_persist_replace_oserror_does_not_crash(tmp_path: Path) -> None:
    """When tmp_path.replace raises OSError, _persist logs and returns cleanly."""
    store = _TestStore(tmp_path / "test.json")
    store._items["x"] = _TestItem(id="x", name="X")

    with patch.object(Path, "replace", side_effect=OSError("disk full")):
        store._persist()  # should not raise


# ---------------------------------------------------------------------------
# _load OSError on read_bytes
# ---------------------------------------------------------------------------


def test_load_oserror_starts_empty(tmp_path: Path) -> None:
    """When _path.read_bytes raises OSError, _load logs and starts empty."""
    path = tmp_path / "test.json"
    # File must exist so _path.exists() returns True, triggering the read.
    path.write_text("[]")

    with patch.object(Path, "read_bytes", side_effect=OSError("permission denied")):
        store = _TestStore(path)

    assert store._items == {}
