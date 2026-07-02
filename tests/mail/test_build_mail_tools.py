"""Tests for the mail integration — :func:`build_mail_tools` and :class:`MailClient`.

Uses ``respx`` (httpx transport-layer mocking) so tests never touch a real
network and do not need the ``broker`` extra installed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import MailSettings
from robotsix_chat.mail import build_mail_tools


def _settings(**kw: Any) -> MailSettings:
    base: dict[str, Any] = {"enabled": True}
    base.update(kw)
    return MailSettings(**base)


# ---------------------------------------------------------------------------
# MailSettings
# ---------------------------------------------------------------------------


def test_mail_settings_defaults() -> None:
    """Default MailSettings has no broker fields."""
    s = MailSettings()
    assert s.enabled is False
    assert s.api_base_url == "http://127.0.0.1:8077"
    assert s.api_token == ""
    assert s.timeout == 30.0


def test_mail_settings_rejects_broker_fields() -> None:
    """Constructing MailSettings with broker YAML fields raises ValidationError."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MailSettings(broker_host="ai-broker.robotsix.net")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        MailSettings(broker_token="tok")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        MailSettings(board_manager_id="board-manager-robotsix-auto-mail")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# build_mail_tools
# ---------------------------------------------------------------------------


def test_build_mail_tools_disabled() -> None:
    """Verify that disabled mail returns no tools."""
    assert build_mail_tools(MailSettings(enabled=False)) == []


def test_build_mail_tools_returns_six_tools() -> None:
    """Verify that enabled mail returns six discrete tools."""
    tools = build_mail_tools(_settings())
    assert len(tools) == 6
    names = [t.__name__ for t in tools]
    assert names == [
        "get_mail_board",
        "get_mail_email_status",
        "move_mail_email",
        "delete_mail_email",
        "archive_mail_email",
        "run_mail_triage",
    ]


# ---------------------------------------------------------------------------
# MailClient — board_content (GET /board-content)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_board_content_success(respx_mock: respx.MockRouter) -> None:
    """GET /board-content returns the JSON body as text."""
    route = respx_mock.get("http://127.0.0.1:8077/board-content").mock(
        return_value=httpx.Response(200, text='{"columns": []}')
    )
    tools = build_mail_tools(_settings())
    get_board = tools[0]

    result = await get_board()

    assert route.called
    assert result == '{"columns": []}'


@pytest.mark.asyncio
async def test_board_content_error(respx_mock: respx.MockRouter) -> None:
    """GET /board-content on 500 returns an error string, never raises."""
    respx_mock.get("http://127.0.0.1:8077/board-content").mock(
        return_value=httpx.Response(500, text="Internal error")
    )
    tools = build_mail_tools(_settings())
    get_board = tools[0]

    result = await get_board()

    assert "Mail API error 500" in result


# ---------------------------------------------------------------------------
# MailClient — email_status (GET /email/{id}/status)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_email_status_success(respx_mock: respx.MockRouter) -> None:
    """GET /email/{id}/status returns the triage column name."""
    route = respx_mock.get("http://127.0.0.1:8077/email/msg-123/status").mock(
        return_value=httpx.Response(200, text="INBOX")
    )
    tools = build_mail_tools(_settings())
    get_status = tools[1]

    result = await get_status("msg-123")

    assert route.called
    assert result == "INBOX"


@pytest.mark.asyncio
async def test_email_status_url_encodes_message_id(
    respx_mock: respx.MockRouter,
) -> None:
    """Special characters in message_id are URL-encoded."""
    route = respx_mock.get(
        "http://127.0.0.1:8077/email/msg%20with%2Fslash/status"
    ).mock(return_value=httpx.Response(200, text="INBOX"))
    tools = build_mail_tools(_settings())
    get_status = tools[1]

    await get_status("msg with/slash")

    assert route.called


# ---------------------------------------------------------------------------
# MailClient — move_email (POST /move)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_email_success(respx_mock: respx.MockRouter) -> None:
    """POST /move with form fields and 302 → success."""
    route = respx_mock.post("http://127.0.0.1:8077/move").mock(
        return_value=httpx.Response(302, text="")
    )
    tools = build_mail_tools(_settings())
    move_email = tools[2]

    result = await move_email("msg-abc", "TO_ARCHIVE")

    assert route.called
    content = route.calls.last.request.content.decode()
    assert "message_id=msg-abc" in content
    assert "triage_action=TO_ARCHIVE" in content
    assert "OK (status 302)" in result


@pytest.mark.asyncio
async def test_move_email_invalid_action() -> None:
    """Invalid triage_action returns an error string without an HTTP call."""
    tools = build_mail_tools(_settings())
    move_email = tools[2]

    result = await move_email("msg-1", "INVALID_ACTION")

    assert "Invalid triage_action" in result
    assert "INBOX" in result  # lists valid actions


@pytest.mark.asyncio
async def test_move_email_400_error(respx_mock: respx.MockRouter) -> None:
    """POST /move with 400 returns the error body."""
    respx_mock.post("http://127.0.0.1:8077/move").mock(
        return_value=httpx.Response(400, text="Unknown message_id")
    )
    tools = build_mail_tools(_settings())
    move_email = tools[2]

    result = await move_email("bad-id", "TO_ARCHIVE")

    assert "Mail API error 400" in result
    assert "Unknown message_id" in result


# ---------------------------------------------------------------------------
# MailClient — delete_email (POST /delete)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_email_success(respx_mock: respx.MockRouter) -> None:
    """POST /delete with form-encoded message_id and 302 → success."""
    route = respx_mock.post("http://127.0.0.1:8077/delete").mock(
        return_value=httpx.Response(302, text="")
    )
    tools = build_mail_tools(_settings())
    delete_email = tools[3]

    result = await delete_email("msg-del")

    assert route.called
    assert b"message_id=msg-del" in route.calls.last.request.content
    assert "OK (status 302)" in result


# ---------------------------------------------------------------------------
# MailClient — archive_email (POST /archive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_email_success(respx_mock: respx.MockRouter) -> None:
    """POST /archive with form-encoded message_id and 302 → success."""
    route = respx_mock.post("http://127.0.0.1:8077/archive").mock(
        return_value=httpx.Response(302, text="")
    )
    tools = build_mail_tools(_settings())
    archive_email = tools[4]

    result = await archive_email("msg-arc")

    assert route.called
    assert b"message_id=msg-arc" in route.calls.last.request.content
    assert "OK (status 302)" in result


# ---------------------------------------------------------------------------
# MailClient — run_triage (POST /run-triage)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_triage_success(respx_mock: respx.MockRouter) -> None:
    """POST /run-triage with empty body and 302 → success."""
    route = respx_mock.post("http://127.0.0.1:8077/run-triage").mock(
        return_value=httpx.Response(302, text="")
    )
    tools = build_mail_tools(_settings())
    run_triage = tools[5]

    result = await run_triage()

    assert route.called
    assert "OK (status 302)" in result


# ---------------------------------------------------------------------------
# MailClient — auth token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sends_bearer_token(respx_mock: respx.MockRouter) -> None:
    """When api_token is set, the Authorization: Bearer header is sent."""
    route = respx_mock.get("http://127.0.0.1:8077/board-content").mock(
        return_value=httpx.Response(200, text="ok")
    )
    tools = build_mail_tools(_settings(api_token="secret-token"))
    get_board = tools[0]

    await get_board()

    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_no_auth_header_when_token_empty(
    respx_mock: respx.MockRouter,
) -> None:
    """When api_token is empty, no Authorization header is sent."""
    route = respx_mock.get("http://127.0.0.1:8077/board-content").mock(
        return_value=httpx.Response(200, text="ok")
    )
    tools = build_mail_tools(_settings(api_token=""))
    get_board = tools[0]

    await get_board()

    assert route.called
    assert "authorization" not in route.calls.last.request.headers


# ---------------------------------------------------------------------------
# MailClient — custom base URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_api_base_url(respx_mock: respx.MockRouter) -> None:
    """Custom api_base_url is used as the request prefix."""
    route = respx_mock.get("https://mail.example.com:9000/api/board-content").mock(
        return_value=httpx.Response(200, text="ok")
    )
    tools = build_mail_tools(
        _settings(api_base_url="https://mail.example.com:9000/api/")
    )
    get_board = tools[0]

    await get_board()

    assert route.called


# ---------------------------------------------------------------------------
# MailClient — network error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_network_error_returns_diagnostic(
    respx_mock: respx.MockRouter,
) -> None:
    """A network error is returned as an error string, never raised."""
    respx_mock.get("http://127.0.0.1:8077/board-content").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )
    tools = build_mail_tools(_settings())
    get_board = tools[0]

    result = await get_board()

    assert "timed out" in result


# ---------------------------------------------------------------------------
# No robotsix_agent_comm import in mail module
# ---------------------------------------------------------------------------


def test_no_broker_import_in_mail_module() -> None:
    """The mail module must not import robotsix_agent_comm."""
    import ast
    import os

    mail_dir = os.path.join(
        os.path.dirname(__file__), "..", "..", "src", "robotsix_chat", "mail"
    )
    for fname in os.listdir(mail_dir):
        if not fname.endswith(".py"):
            continue
        fpath = os.path.join(mail_dir, fname)
        with open(fpath) as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (
                    node.module
                    if isinstance(node, ast.ImportFrom)
                    else (node.names[0].name if node.names else "")
                )
                if module and "robotsix_agent_comm" in module:
                    pytest.fail(
                        f"{fname} imports robotsix_agent_comm — "
                        f"mail must not depend on the broker"
                    )
