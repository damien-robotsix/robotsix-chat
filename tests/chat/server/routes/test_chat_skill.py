"""Tests for GET /chat-skill — this component's own skill document.

central-deploy's roster drops any component that does not serve a skill
body, which is why chat was absent from its own roster and had no route to
its own config API.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from robotsix_http.fastapi import (
    assert_chat_skill_route_parity,
)

from tests.conftest import ClientFactory


def _write_empty_config(tmp_path: Path) -> Path:
    """Write an empty committed-config template and return its path."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({}))
    return config_path


def test_chat_skill_returns_markdown(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    config_path = _write_empty_config(tmp_path)
    resp = client_factory(config_path=str(config_path), serve_ui=False).get(
        "/chat-skill"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert resp.text.strip()


def test_chat_skill_has_frontmatter(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """The roster and the consuming agent both key off the frontmatter."""
    body = (
        client_factory(config_path=str(_write_empty_config(tmp_path)), serve_ui=False)
        .get("/chat-skill")
        .text
    )
    assert body.startswith("---\n")
    head = body.split("---", 2)[1]
    assert "name: robotsix-chat" in head
    assert "description:" in head


def test_chat_skill_documents_the_config_endpoints(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """An endpoint missing from the skill is one the agent will not call."""
    body = (
        client_factory(config_path=str(_write_empty_config(tmp_path)), serve_ui=False)
        .get("/chat-skill")
        .text
    )
    for route in (
        "GET /config",
        "PUT /config",
        "GET /config/versions",
        "POST /config/rollback",
    ):
        assert route in body, f"{route} missing from the skill document"


def test_chat_skill_steers_away_from_the_deploy_plane(
    tmp_path: Path, client_factory: ClientFactory
) -> None:
    """Writes must not go through the deploy plane's template-derived path."""
    body = (
        client_factory(config_path=str(_write_empty_config(tmp_path)), serve_ui=False)
        .get("/chat-skill")
        .text
    )
    assert "/chat/config/{name}" in body


def test_chat_skill_route_parity() -> None:
    """The routes the skill documents are real, registered routes.

    ``assert_chat_skill_route_parity`` is strict in both directions, so it
    runs against a focused FastAPI app carrying exactly the config routes
    this self-config skill describes — the chat server itself is a Starlette
    app whose non-config routes are deliberately outside the skill's
    contract.
    """
    from robotsix_http.fastapi import create_chat_skill_router

    from robotsix_chat.chat.server.routes.chat_skill import _CHAT_SKILL_TEXT
    from robotsix_chat.chat.server.routes.config import (
        config_get_endpoint,
        config_rollback_endpoint,
        config_save_endpoint,
        config_versions_endpoint,
    )

    app = FastAPI()
    app.add_api_route("/config", config_get_endpoint, methods=["GET"])
    app.add_api_route("/config", config_save_endpoint, methods=["PUT"])
    app.add_api_route("/config/versions", config_versions_endpoint, methods=["GET"])
    app.add_api_route("/config/rollback", config_rollback_endpoint, methods=["POST"])
    # The chat server serves /chat-skill through a Starlette-native endpoint;
    # the shared factory (FastAPI-only) is exercised here to keep the
    # validated-descriptor contract in scope.
    chat_skill_router = create_chat_skill_router(_CHAT_SKILL_TEXT, name="robotsix-chat")
    app.include_router(chat_skill_router)
    assert_chat_skill_route_parity(
        app,
        _CHAT_SKILL_TEXT,
        # Query-parameter variants and the deploy-plane restart route are
        # documented prose referring to other endpoints, not registered
        # route paths.
        ignore={
            "/config?keys_only=true",
            "/config?path=periodic",
            "/config?include_schema=false",
            "/chat/services/{your-component-id}/restart",
        },
    )
