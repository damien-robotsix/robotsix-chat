"""Tests for ``POST /diagnostics/events`` and ``GET /diagnostics/events``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from starlette.testclient import TestClient

from robotsix_chat.chat.server.app import create_app
from robotsix_chat.diagnostics import DiagnosticStore


class _DummyAgent:
    """Minimal agent stub — only ``stream`` is called by the chat endpoint."""

    async def stream(self, message: str) -> Any:
        yield "ok"
        return


def _make_app(store: DiagnosticStore | None = None) -> TestClient:
    app = create_app(
        _DummyAgent(),  # type: ignore[arg-type]
        diagnostic_store=store,
        serve_ui=False,
    )
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# POST /diagnostics/events
# ---------------------------------------------------------------------------


def test_create_records_event_and_returns_201(tmp_path: Path) -> None:
    """POST records event and returns 201 with the created bundle."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    client = _make_app(store)

    resp = client.post(
        "/diagnostics/events",
        json={
            "category": "CI_FAILURE",
            "message": "pytest failed in CI stage verify",
            "details": {"ticket_id": "abc123", "stage": "verify", "exit_code": 1},
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["category"] == "CI_FAILURE"
    assert data["message"] == "pytest failed in CI stage verify"
    assert data["details"] == {
        "ticket_id": "abc123",
        "stage": "verify",
        "exit_code": 1,
    }
    assert "id" in data
    assert "created_at" in data

    # Event is immediately visible via list.
    events = store.list_events("CI_FAILURE")
    assert len(events) == 1
    assert events[0].id == data["id"]


def test_create_503_when_store_not_configured() -> None:
    """POST returns 503 when no diagnostic store is configured."""
    client = _make_app(store=None)
    resp = client.post(
        "/diagnostics/events",
        json={"category": "CI_FAILURE", "message": "test"},
    )
    assert resp.status_code == 503


def test_create_400_when_category_missing(tmp_path: Path) -> None:
    """POST returns 400 when 'category' field is missing."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    client = _make_app(store)
    resp = client.post(
        "/diagnostics/events",
        json={"message": "no category"},
    )
    assert resp.status_code == 400


def test_create_400_when_message_missing(tmp_path: Path) -> None:
    """POST returns 400 when 'message' field is missing."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    client = _make_app(store)
    resp = client.post(
        "/diagnostics/events",
        json={"category": "CI_FAILURE"},
    )
    assert resp.status_code == 400


def test_create_400_when_details_not_a_dict(tmp_path: Path) -> None:
    """POST returns 400 when 'details' is not a JSON object."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    client = _make_app(store)
    resp = client.post(
        "/diagnostics/events",
        json={
            "category": "CI_FAILURE",
            "message": "test",
            "details": "not a dict",
        },
    )
    assert resp.status_code == 400


def test_create_400_when_body_not_json(tmp_path: Path) -> None:
    """POST returns 400 when body is not valid JSON."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    client = _make_app(store)
    resp = client.post(
        "/diagnostics/events",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_create_record_without_details(tmp_path: Path) -> None:
    """POST accepts events without optional 'details' field."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    client = _make_app(store)
    resp = client.post(
        "/diagnostics/events",
        json={"category": "CI_FAILURE", "message": "no details"},
    )
    assert resp.status_code == 201
    assert resp.json()["details"] is None


# ---------------------------------------------------------------------------
# GET /diagnostics/events
# ---------------------------------------------------------------------------


def test_list_returns_all_events(tmp_path: Path) -> None:
    """GET returns all events when no category filter is given."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    store.record_event("CI_FAILURE", "first failure")
    store.record_event("CLONE_ERROR", "clone failed")

    client = _make_app(store)
    resp = client.get("/diagnostics/events")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


def test_list_filters_by_category(tmp_path: Path) -> None:
    """GET filters events by the 'category' query parameter."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    store.record_event("CI_FAILURE", "failure 1")
    store.record_event("CI_FAILURE", "failure 2")
    store.record_event("OTHER", "other event")

    client = _make_app(store)
    resp = client.get("/diagnostics/events?category=CI_FAILURE")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all(e["category"] == "CI_FAILURE" for e in data)


def test_list_empty_when_no_events(tmp_path: Path) -> None:
    """GET returns an empty list when no events exist."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    client = _make_app(store)
    resp = client.get("/diagnostics/events")
    assert resp.status_code == 200
    assert resp.json() == []


def test_list_503_when_store_not_configured() -> None:
    """GET returns 503 when no diagnostic store is configured."""
    client = _make_app(store=None)
    resp = client.get("/diagnostics/events")
    assert resp.status_code == 503


def test_list_case_insensitive_category(tmp_path: Path) -> None:
    """GET category filter is case-insensitive."""
    store = DiagnosticStore(str(tmp_path / "diag.json"))
    store.record_event("CI_FAILURE", "failure")

    client = _make_app(store)
    resp = client.get("/diagnostics/events?category=ci_failure")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
