"""Tests for GET /chat-skill — this component's own skill document.

central-deploy's roster drops any component that does not serve a skill
body, which is why chat was absent from its own roster and had no route to
its own config API.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from robotsix_chat.chat.server.app import create_app


class _DummyAgent:
    """Minimal agent stub — ``stream`` is the only method the app calls."""

    async def stream(self, message: str):
        yield "ok"
        return


def _make_client(tmp_path: Path) -> TestClient:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({}))
    app = create_app(_DummyAgent(), config_path=str(config_path), serve_ui=False)
    return TestClient(app, raise_server_exceptions=False)


def test_chat_skill_returns_plain_text(tmp_path: Path) -> None:
    resp = _make_client(tmp_path).get("/chat-skill")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text.strip()


def test_chat_skill_has_frontmatter(tmp_path: Path) -> None:
    """The roster and the consuming agent both key off the frontmatter."""
    body = _make_client(tmp_path).get("/chat-skill").text
    assert body.startswith("---\n")
    head = body.split("---", 2)[1]
    assert "name: robotsix-chat-self" in head
    assert "description:" in head


def test_chat_skill_documents_the_config_endpoints(tmp_path: Path) -> None:
    """An endpoint missing from the skill is one the agent will not call."""
    body = _make_client(tmp_path).get("/chat-skill").text
    for route in (
        "GET /config",
        "PUT /config",
        "GET /config/versions",
        "POST /config/rollback",
    ):
        assert route in body, f"{route} missing from the skill document"


def test_chat_skill_steers_away_from_the_deploy_plane(tmp_path: Path) -> None:
    """Writes must not go through the deploy plane's template-derived path."""
    body = _make_client(tmp_path).get("/chat-skill").text
    assert "/chat/config/{name}" in body
