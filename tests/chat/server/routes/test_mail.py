"""Tests for ``GET /mail/archive-root-check``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from robotsix_chat.chat.server.app import create_app
from robotsix_chat.mail.client import MailClient


class _DummyAgent:
    """Minimal agent stub — only ``stream`` is called by the chat endpoint."""

    async def stream(self, message: str, **kwargs: object) -> Any:
        yield "ok"
        return


def _write_config(tmp_path: Path, mail: dict[str, Any]) -> str:
    """Write a config file containing only the ``mail`` section."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"mail": mail}), encoding="utf-8")
    return str(config_path)


def _make_app(config_path: str) -> TestClient:
    app = create_app(
        _DummyAgent(),  # type: ignore[arg-type]
        config_path=config_path,
        serve_ui=False,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_archive_root_check_reports_populated_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET returns 200 with folder list when the archive root is populated."""
    config_path = _write_config(
        tmp_path,
        {"enabled": True, "api_base_url": "http://127.0.0.1:8077"},
    )

    async def _fake_archive_folders(self: MailClient) -> str:
        return json.dumps({"delimiter": "/", "folders": ["2024/01", "2024/02"]})

    monkeypatch.setattr(MailClient, "archive_folders", _fake_archive_folders)

    client = _make_app(config_path)
    resp = client.get("/mail/archive-root-check")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["folders_count"] == 2
    assert data["folders"] == ["2024/01", "2024/02"]
    assert data["archive_root_empty"] is False
    assert data["expected_ovh_archive_root"] == "INBOX/robotsix-mail-archive"
    assert data["suggestion"] == ""


def test_archive_root_check_flags_empty_archive_with_ovh_suggestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET flags an empty archive root and suggests the OVH path."""
    config_path = _write_config(
        tmp_path,
        {"enabled": True, "api_base_url": "http://127.0.0.1:8077"},
    )

    async def _fake_archive_folders(self: MailClient) -> str:
        return json.dumps({"delimiter": "/", "folders": []})

    monkeypatch.setattr(MailClient, "archive_folders", _fake_archive_folders)

    client = _make_app(config_path)
    resp = client.get("/mail/archive-root-check")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["archive_root_empty"] is True
    assert "INBOX/robotsix-mail-archive" in data["suggestion"]


def test_archive_root_check_returns_503_when_mail_disabled(
    tmp_path: Path,
) -> None:
    """GET returns 503 when the mail integration is disabled."""
    config_path = _write_config(tmp_path, {"enabled": False})
    client = _make_app(config_path)

    resp = client.get("/mail/archive-root-check")

    assert resp.status_code == 503
    assert resp.json()["status"] == "error"
    assert resp.json()["detail"] == "mail integration is disabled"


def test_archive_root_check_returns_502_on_non_json_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET returns 502 when the mail server returns a non-JSON body."""
    config_path = _write_config(
        tmp_path,
        {"enabled": True, "api_base_url": "http://127.0.0.1:8077"},
    )

    async def _fake_archive_folders(self: MailClient) -> str:
        return "Mail API error: connection refused"

    monkeypatch.setattr(MailClient, "archive_folders", _fake_archive_folders)

    client = _make_app(config_path)
    resp = client.get("/mail/archive-root-check")

    assert resp.status_code == 502
    assert resp.json()["status"] == "error"


def test_archive_root_check_returns_503_on_malformed_config_json(
    tmp_path: Path,
) -> None:
    """GET returns 503 when the on-disk config is not valid JSON."""
    config_path = tmp_path / "config.json"
    config_path.write_text("{not valid json", encoding="utf-8")
    client = _make_app(str(config_path))

    resp = client.get("/mail/archive-root-check")

    assert resp.status_code == 503
    assert resp.json()["status"] == "error"
    assert "failed to load mail config" in resp.json()["detail"]


def test_archive_root_check_returns_502_on_non_dict_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET returns 502 when the mail server returns JSON that is not an object."""
    config_path = _write_config(
        tmp_path,
        {"enabled": True, "api_base_url": "http://127.0.0.1:8077"},
    )

    async def _fake_archive_folders(self: MailClient) -> str:
        return json.dumps(["not", "a", "dict"])

    monkeypatch.setattr(MailClient, "archive_folders", _fake_archive_folders)

    client = _make_app(config_path)
    resp = client.get("/mail/archive-root-check")

    assert resp.status_code == 502
    assert resp.json()["status"] == "error"
    assert resp.json()["detail"] == "mail server returned an unexpected response"


def test_archive_root_check_returns_502_when_folders_is_not_a_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET returns 502 when the mail server's folders field is not a list."""
    config_path = _write_config(
        tmp_path,
        {"enabled": True, "api_base_url": "http://127.0.0.1:8077"},
    )

    async def _fake_archive_folders(self: MailClient) -> str:
        return json.dumps({"delimiter": "/", "folders": "not-a-list"})

    monkeypatch.setattr(MailClient, "archive_folders", _fake_archive_folders)

    client = _make_app(config_path)
    resp = client.get("/mail/archive-root-check")

    assert resp.status_code == 502
    assert resp.json()["status"] == "error"
    assert resp.json()["detail"] == "mail server response missing 'folders' list"
