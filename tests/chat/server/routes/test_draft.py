"""Tests for the session draft GET/PUT endpoints.

Coverage: save, retrieve, overwrite, session isolation, missing draft.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from robotsix_chat.chat.server.app import create_app

# ---------------------------------------------------------------------------
# Dummy agent for TestClient
# ---------------------------------------------------------------------------


class _DummyAgent:
    """Minimal agent stub."""

    async def stream(self, message: str):
        yield "ok"
        return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(draft_path: Path) -> TestClient:
    """Build a Starlette TestClient with *draft_path* wired."""
    app = create_app(
        _DummyAgent(),
        draft_store_path=str(draft_path),
        serve_ui=False,
    )
    return TestClient(app, raise_server_exceptions=False)


def _put_draft(
    client: TestClient,
    session_id: str,
    queue: list | None = None,
    pending_images: list | None = None,
) -> None:
    """PUT a draft for *session_id*."""
    body: dict = {}
    if queue is not None:
        body["queue"] = queue
    if pending_images is not None:
        body["pending_images"] = pending_images
    resp = client.put(
        f"/sessions/{session_id}/draft",
        json=body,
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def _get_draft(client: TestClient, session_id: str) -> dict:
    """GET the draft for *session_id*."""
    resp = client.get(f"/sessions/{session_id}/draft")
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_draft_nonexistent(tmp_path: Path) -> None:
    """GET draft when none was saved returns empty object."""
    client = _make_app(tmp_path / "drafts.json")
    draft = _get_draft(client, "s1")
    assert draft == {}


def test_put_and_get_draft(tmp_path: Path) -> None:
    """PUT a draft and GET it back."""
    client = _make_app(tmp_path / "drafts.json")

    queue = [{"text": "hello", "images": [], "messageId": "abc"}]
    pending = [{"media_type": "image/png", "data": "aaaa", "filename": "x.png"}]

    _put_draft(client, "s1", queue=queue, pending_images=pending)

    draft = _get_draft(client, "s1")
    assert draft["queue"] == queue
    assert draft["pending_images"] == pending


def test_put_overwrites_existing_draft(tmp_path: Path) -> None:
    """PUT replaces an existing draft for the same session."""
    client = _make_app(tmp_path / "drafts.json")

    _put_draft(client, "s1", queue=[{"text": "first", "images": [], "messageId": "1"}])
    _put_draft(client, "s1", queue=[{"text": "second", "images": [], "messageId": "2"}])

    draft = _get_draft(client, "s1")
    assert len(draft["queue"]) == 1
    assert draft["queue"][0]["text"] == "second"


def test_drafts_isolated_per_session(tmp_path: Path) -> None:
    """Drafts for different sessions do not interfere."""
    client = _make_app(tmp_path / "drafts.json")

    _put_draft(client, "s1", queue=[{"text": "one", "images": [], "messageId": "1"}])
    _put_draft(client, "s2", queue=[{"text": "two", "images": [], "messageId": "2"}])

    d1 = _get_draft(client, "s1")
    d2 = _get_draft(client, "s2")

    assert d1["queue"][0]["text"] == "one"
    assert d2["queue"][0]["text"] == "two"


def test_put_empty_body_is_ok(tmp_path: Path) -> None:
    """PUT with an empty body clears the draft for that session."""
    client = _make_app(tmp_path / "drafts.json")

    _put_draft(client, "s1", queue=[{"text": "hi", "images": [], "messageId": "x"}])
    _put_draft(client, "s1", queue=[], pending_images=[])

    draft = _get_draft(client, "s1")
    assert draft == {"queue": [], "pending_images": []}


def test_put_ignores_unknown_keys(tmp_path: Path) -> None:
    """Unknown top-level keys are not stored."""
    client = _make_app(tmp_path / "drafts.json")

    resp = client.put(
        "/sessions/s1/draft",
        json={"queue": [{"text": "hi", "images": [], "messageId": "x"}], "garbage": 42},
    )
    assert resp.status_code == 200

    draft = _get_draft(client, "s1")
    assert "garbage" not in draft
    assert draft["queue"][0]["text"] == "hi"
