"""Startup component-connectivity probe: URL selection and self-skip.

Regression tests for the 2026-09-01 boot-noise pair: langfuse warned on
every boot because the blind ``<base_url>/health`` convention 404s on
Next.js (its liveness endpoint is ``/api/public/health``), and the chat
probed ITSELF before uvicorn was listening — a guaranteed-false warning.
Fixtures mirror the real roster shape (list of dicts with ``id`` /
``base_url``, as returned by ``fetch_roster``).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from robotsix_chat import startup_checks


@dataclass
class _Result:
    ok: bool = True
    error: str = ""


@pytest.mark.asyncio
async def test_probe_urls_health_path_override_and_self_skip(monkeypatch):
    roster = [
        {"id": "chat", "base_url": "http://chat:8080"},
        {"id": "langfuse", "base_url": "https://langfuse.example.net"},
        {"id": "mail", "base_url": "http://mail:8080/"},
        {"id": "broken", "base_url": "", "_error": "unresolvable"},
    ]

    async def fake_fetch_roster(settings):
        return roster

    probed: list[str] = []

    async def fake_request(method, url, timeout, label):
        probed.append(url)
        return _Result()

    monkeypatch.setattr(startup_checks, "fetch_roster", fake_fetch_roster)
    monkeypatch.setattr(startup_checks, "safe_http_request", fake_request)

    await startup_checks.check_component_connectivity(settings=None)

    # Self entry is never probed; langfuse uses its real liveness path;
    # everything else keeps the /health convention (trailing slash trimmed).
    assert probed == [
        "https://langfuse.example.net/api/public/health",
        "http://mail:8080/health",
    ]
