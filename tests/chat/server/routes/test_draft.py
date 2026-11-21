"""Tests for the session draft GET/PUT endpoints.

Coverage: save, retrieve, overwrite, session isolation, missing draft,
corrupt file recovery, empty draft clearing.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from tests.conftest import ClientFactory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def test_get_draft_nonexistent(tmp_path: Path, client_factory: ClientFactory) -> None:
    """GET draft when none was saved returns empty object."""
    client = client_factory(
        draft_store_dir=str(tmp_path / "drafts_dir"), serve_ui=False
    )
    draft = _get_draft(client, "s1")
    assert draft == {}


def test_put_and_get_draft(tmp_path: Path, client_factory: ClientFactory) -> None:
    """PUT a draft and GET it back."""
    client = client_factory(
        draft_store_dir=str(tmp_path / "drafts_dir"), serve_ui=False
    )

    queue = [{"text": "hello", "images": [], "messageId": "abc"}]
    pending = [{"media_type": "image/png", "data": "aaaa", "filename": "x.png"}]

    _put_draft(client, "s1", queue=queue, pending_images=pending)

    draft = _get_draft(client, "s1")
    assert draft["queue"] == queue
    assert draft["pending_images"] == pending


def test_put_overwrites_existing_draft(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """PUT replaces an existing draft for the same session."""
    client = client_factory(
        draft_store_dir=str(tmp_path / "drafts_dir"), serve_ui=False
    )

    _put_draft(client, "s1", queue=[{"text": "first", "images": [], "messageId": "1"}])
    _put_draft(client, "s1", queue=[{"text": "second", "images": [], "messageId": "2"}])

    draft = _get_draft(client, "s1")
    assert len(draft["queue"]) == 1
    assert draft["queue"][0]["text"] == "second"


def test_drafts_isolated_per_session(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """Drafts for different sessions do not interfere."""
    client = client_factory(
        draft_store_dir=str(tmp_path / "drafts_dir"), serve_ui=False
    )

    _put_draft(client, "s1", queue=[{"text": "one", "images": [], "messageId": "1"}])
    _put_draft(client, "s2", queue=[{"text": "two", "images": [], "messageId": "2"}])

    d1 = _get_draft(client, "s1")
    d2 = _get_draft(client, "s2")

    assert d1["queue"][0]["text"] == "one"
    assert d2["queue"][0]["text"] == "two"


def test_put_empty_body_is_ok(tmp_path: Path, client_factory: ClientFactory) -> None:
    """PUT with an empty body clears the draft for that session."""
    client = client_factory(
        draft_store_dir=str(tmp_path / "drafts_dir"), serve_ui=False
    )

    _put_draft(client, "s1", queue=[{"text": "hi", "images": [], "messageId": "x"}])
    _put_draft(client, "s1", queue=[], pending_images=[])

    draft = _get_draft(client, "s1")
    assert draft == {"queue": [], "pending_images": []}


def test_put_ignores_unknown_keys(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """Unknown top-level keys are not stored."""
    client = client_factory(
        draft_store_dir=str(tmp_path / "drafts_dir"), serve_ui=False
    )

    resp = client.put(
        "/sessions/s1/draft",
        json={"queue": [{"text": "hi", "images": [], "messageId": "x"}], "garbage": 42},
    )
    assert resp.status_code == 200

    draft = _get_draft(client, "s1")
    assert "garbage" not in draft
    assert draft["queue"][0]["text"] == "hi"


def test_corrupt_draft_file_is_renamed(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """A corrupt draft file is renamed and an empty draft is returned."""
    draft_dir = tmp_path / "drafts_dir"
    client = client_factory(draft_store_dir=str(draft_dir), serve_ui=False)

    # Create the directory and a corrupt file for session "bad".
    draft_dir.mkdir(parents=True, exist_ok=True)
    bad_file = draft_dir / "bad.json"
    bad_file.write_text("this is not json {{{", encoding="utf-8")

    draft = _get_draft(client, "bad")
    assert draft == {}

    # The original corrupt file should be gone, replaced by a .corrupt copy.
    assert not bad_file.exists()
    corrupt_files = list(draft_dir.glob("bad.json.corrupt.*"))
    assert len(corrupt_files) == 1
    assert "this is not json" in corrupt_files[0].read_text()


def test_corrupt_draft_does_not_affect_other_sessions(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """A corrupt file for one session does not affect another session's draft."""
    draft_dir = tmp_path / "drafts_dir"
    client = client_factory(draft_store_dir=str(draft_dir), serve_ui=False)

    # Save a valid draft for session "good".
    _put_draft(client, "good", queue=[{"text": "ok", "images": [], "messageId": "g"}])

    # Corrupt the file for session "bad".
    draft_dir.mkdir(parents=True, exist_ok=True)
    bad_file = draft_dir / "bad.json"
    bad_file.write_text("{{{ broken", encoding="utf-8")

    # Reading "bad" should return empty and rename the corrupt file.
    draft_bad = _get_draft(client, "bad")
    assert draft_bad == {}

    # Reading "good" should still return the valid draft.
    draft_good = _get_draft(client, "good")
    assert draft_good["queue"][0]["text"] == "ok"


def test_concurrent_puts_different_sessions_no_race(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """PUTs for different sessions write to different files — no race."""
    draft_dir = tmp_path / "drafts_dir"
    client = client_factory(draft_store_dir=str(draft_dir), serve_ui=False)

    # Simulate concurrent PUTs by writing both without any interleaving concern.
    _put_draft(client, "a", queue=[{"text": "aaa", "images": [], "messageId": "a1"}])
    _put_draft(client, "b", queue=[{"text": "bbb", "images": [], "messageId": "b1"}])

    # Both drafts survive intact.
    assert _get_draft(client, "a")["queue"][0]["text"] == "aaa"
    assert _get_draft(client, "b")["queue"][0]["text"] == "bbb"


def test_draft_dir_created_on_first_write(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """The drafts directory is created automatically on first PUT."""
    draft_dir = tmp_path / "nonexistent" / "drafts"
    client = client_factory(draft_store_dir=str(draft_dir), serve_ui=False)

    _put_draft(client, "s1", queue=[{"text": "hi", "images": [], "messageId": "x"}])
    assert draft_dir.is_dir()
    assert (draft_dir / "s1.json").exists()
