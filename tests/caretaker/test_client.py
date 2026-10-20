"""Tests for the caretaker /caretaker/status HTTP client."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from robotsix_chat.caretaker import client as client_mod
from robotsix_chat.caretaker.client import fetch_caretaker_status
from robotsix_chat.config.deploy_models import CentralDeploySettings


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _FakeClient:
    def __init__(self, payload: Any, captured: dict[str, Any]) -> None:
        self._payload = payload
        self._captured = captured

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self._captured["url"] = url
        self._captured["headers"] = headers or {}
        return _FakeResponse(self._payload)


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, payload: Any, captured: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        client_mod.httpx,
        "AsyncClient",
        lambda *a, **k: _FakeClient(payload, captured),
    )


@pytest.mark.asyncio
async def test_builds_status_url_and_sends_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_client(
        monkeypatch,
        [{"kind": "claude_auth", "severity": "error", "detail": "re-login"}],
        captured,
    )
    central = CentralDeploySettings(
        url="https://deploy.robotsix.net/",
        deploy_api_key="secret-key",  # pragma: allowlist secret
    )

    findings = await fetch_caretaker_status(central, timeout=5.0)

    assert captured["url"] == "https://deploy.robotsix.net/caretaker/status"
    assert captured["headers"]["X-API-Key"] == "secret-key"  # pragma: allowlist secret
    assert [f.kind for f in findings] == ["claude_auth"]


@pytest.mark.asyncio
async def test_no_api_key_header_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_client(monkeypatch, [], captured)
    central = CentralDeploySettings(url="https://deploy.robotsix.net")

    await fetch_caretaker_status(central, timeout=5.0)

    assert "X-API-Key" not in captured["headers"]


@pytest.mark.asyncio
async def test_malformed_body_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_client(monkeypatch, {"unexpected": "shape"}, captured)
    central = CentralDeploySettings(url="https://deploy.robotsix.net")

    assert await fetch_caretaker_status(central, timeout=5.0) == []


@pytest.mark.asyncio
async def test_http_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom(_FakeClient):
        async def get(
            self, url: str, headers: dict[str, str] | None = None
        ) -> _FakeResponse:
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(client_mod.httpx, "AsyncClient", lambda *a, **k: _Boom([], {}))
    central = CentralDeploySettings(url="https://deploy.robotsix.net")

    with pytest.raises(httpx.HTTPError):
        await fetch_caretaker_status(central, timeout=5.0)
