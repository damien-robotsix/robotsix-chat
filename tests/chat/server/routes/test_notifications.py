"""Tests for the unread-notification API endpoints.

Covers ``GET /notifications/unread`` (unread records, oldest first) and
``POST /notifications/read`` (mark specific ids or all unread as read),
driven by the shared :class:`NotificationStore`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request

from robotsix_chat.chat.server.routes.notifications import (
    notifications_read_endpoint,
    notifications_unread_endpoint,
)
from robotsix_chat.notification.store import NotificationStore
from tests.conftest import mock_app

from .conftest import _make_bare_request


def _days_ago(days: float) -> str:
    """Return an ISO-8601 UTC timestamp *days* days in the past."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _app_with_store(store: NotificationStore | None) -> SimpleNamespace:
    """Build a fake app exposing ``app.state.notification_store``."""
    return SimpleNamespace(state=SimpleNamespace(notification_store=store))


def _post_request(app: SimpleNamespace, body: dict[str, object]) -> Request:
    """Build a POST ``Request`` whose JSON body reads via the receive channel."""
    body_bytes = json.dumps(body).encode()
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 50000),
        "path": "/",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "app": app,
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive)


def _store(tmp_path: Path, records: list[dict[str, str]]) -> NotificationStore:
    """Build a store pre-populated with the given records."""
    store = NotificationStore(tmp_path / "notifications.json")
    for rec in records:
        store.append(
            title=rec["title"],
            body=rec["body"],
            source_session=rec["source_session"],
            ts=rec["ts"],
        )
    return store


def _unread_record(title: str, ts: str) -> dict[str, str]:
    """Build a single unread notification record dict for seeding."""
    return {"title": title, "body": "b", "source_session": "sess", "ts": ts}


# ---------------------------------------------------------------------------
# GET /notifications/unread
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unread_not_wired_returns_503() -> None:
    """Returns 503 when no notification store is wired into app.state."""
    async with mock_app() as f:
        response = await f.client.get("/notifications/unread")
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_unread_returns_only_unread_records(tmp_path: Path) -> None:
    """Only records with ``read=false`` are returned, read ones excluded."""
    store = _store(
        tmp_path,
        [
            _unread_record("unread", _days_ago(2)),
            _unread_record("read", _days_ago(1)),
        ],
    )
    read_one = [r for r in store.list() if r.title == "read"][0]
    store.mark_read([read_one.id])

    request = _make_bare_request(app=_app_with_store(store))
    response = await notifications_unread_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert [n["title"] for n in body] == ["unread"]
    assert [n["id"] for n in body] != [read_one.id]


@pytest.mark.asyncio
async def test_unread_ordered_oldest_first(tmp_path: Path) -> None:
    """Unread records are returned ordered by ``ts`` ascending."""
    store = _store(
        tmp_path,
        [
            _unread_record("newest", _days_ago(1)),
            _unread_record("oldest", _days_ago(3)),
            _unread_record("middle", _days_ago(2)),
        ],
    )

    request = _make_bare_request(app=_app_with_store(store))
    response = await notifications_unread_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert [n["title"] for n in body] == ["oldest", "middle", "newest"]


@pytest.mark.asyncio
async def test_unread_record_shape(tmp_path: Path) -> None:
    """Each returned record carries id, ts, title, body, source_session."""
    store = _store(
        tmp_path,
        [
            {
                "title": "Build failed",
                "body": "main broke on CI",
                "source_session": "sess-7",
                "ts": _days_ago(1),
            }
        ],
    )

    request = _make_bare_request(app=_app_with_store(store))
    response = await notifications_unread_endpoint(request)

    assert response.status_code == 200
    body = json.loads(response.body)  # type: ignore[arg-type]
    assert len(body) == 1
    notification = body[0]
    assert notification["title"] == "Build failed"
    assert notification["body"] == "main broke on CI"
    assert notification["source_session"] == "sess-7"
    datetime.fromisoformat(notification["ts"])
    assert notification["read"] is False
    assert notification["id"]


@pytest.mark.asyncio
async def test_unread_store_failure_returns_500() -> None:
    """A store that raises while listing yields a 500 response."""
    store = MagicMock()
    store.list.side_effect = RuntimeError("boom")
    request = _make_bare_request(app=_app_with_store(store))

    response = await notifications_unread_endpoint(request)

    assert response.status_code == 500


# ---------------------------------------------------------------------------
# POST /notifications/read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_not_wired_returns_503() -> None:
    """Returns 503 when no notification store is wired into app.state."""
    async with mock_app() as f:
        response = await f.client.post("/notifications/read", json={})
        assert response.status_code == 503


@pytest.mark.asyncio
async def test_read_marks_specific_ids(tmp_path: Path) -> None:
    """Marking specific ids clears them from subsequent unread responses."""
    store = _store(
        tmp_path,
        [
            _unread_record("a", _days_ago(2)),
            _unread_record("b", _days_ago(1)),
        ],
    )
    target = [r for r in store.list() if r.title == "a"][0]

    request = _post_request(_app_with_store(store), {"ids": [target.id]})

    response = await notifications_read_endpoint(request)

    assert response.status_code == 200
    assert json.loads(response.body)["marked"] == 1  # type: ignore[arg-type]

    remaining = json.loads((await notifications_unread_endpoint(request)).body)  # type: ignore[arg-type]
    assert [n["title"] for n in remaining] == ["b"]


@pytest.mark.asyncio
async def test_read_marks_all_unread_with_empty_body(tmp_path: Path) -> None:
    """An empty ``ids`` marks every currently unread notification read."""
    store = _store(
        tmp_path,
        [
            _unread_record("a", _days_ago(2)),
            _unread_record("b", _days_ago(1)),
        ],
    )
    request = _post_request(_app_with_store(store), {})

    response = await notifications_read_endpoint(request)

    assert response.status_code == 200
    assert json.loads(response.body)["marked"] == 2  # type: ignore[arg-type]

    remaining = json.loads((await notifications_unread_endpoint(request)).body)  # type: ignore[arg-type]
    assert remaining == []


@pytest.mark.asyncio
async def test_read_rejects_non_string_ids(tmp_path: Path) -> None:
    """A non-string entry in ``ids`` yields a 400."""
    store = NotificationStore(tmp_path / "notifications.json")
    store.append(title="a", body="b", source_session="sess")
    async with mock_app(notification_store=store) as f:
        response = await f.client.post("/notifications/read", json={"ids": [1, 2]})
        assert response.status_code == 400


@pytest.mark.asyncio
async def test_read_store_failure_returns_500() -> None:
    """A store that raises while marking read yields a 500 response."""
    store = MagicMock()
    store.mark_read.side_effect = RuntimeError("boom")
    request = _post_request(_app_with_store(store), {})

    response = await notifications_read_endpoint(request)

    assert response.status_code == 500
