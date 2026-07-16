"""Smoke tests for the browser UI.

HTML structure, static assets, and template substitution.
"""

from __future__ import annotations

import re

import pytest

# ---------------------------------------------------------------------------
# HTML template rendering
# ---------------------------------------------------------------------------


class TestHtmlTemplateSubstitution:
    """Template variables substituted by ``_load_ui_html``."""

    def test_project_title_replaced(self, ui_html: str) -> None:
        """``{{ PROJECT_TITLE }}`` is replaced (no raw placeholder remains)."""
        assert "{{ PROJECT_TITLE }}" not in ui_html
        assert 'content="' in ui_html  # meta tag populated

    def test_idle_timeout_replaced(self, ui_html: str) -> None:
        """``{{ IDLE_TIMEOUT_MINUTES }}`` is replaced with the configured value."""
        assert "{{ IDLE_TIMEOUT_MINUTES }}" not in ui_html
        assert 'content="30"' in ui_html  # default

    def test_idle_timeout_zero(self, ui_html_no_idle: str) -> None:
        """A zero timeout renders ``content="0"``."""
        assert 'content="0"' in ui_html_no_idle


class TestHtmlDomStructure:
    """Required DOM elements for the SPA to function."""

    def test_doctype_html(self, ui_html: str) -> None:
        """Document starts with ``<!DOCTYPE html>``."""
        assert ui_html.strip().startswith("<!DOCTYPE html>")

    def test_chat_container(self, ui_html: str) -> None:
        """The main chat scroll area exists."""
        assert 'id="chat"' in ui_html

    def test_composer(self, ui_html: str) -> None:
        """The message composer (textarea + send button) exists."""
        assert 'id="composer"' in ui_html
        assert 'id="msg-input"' in ui_html
        assert 'id="send-btn"' in ui_html

    def test_error_banner(self, ui_html: str) -> None:
        """The error banner with dismiss button exists."""
        assert 'id="error-banner"' in ui_html

    def test_header_bar(self, ui_html: str) -> None:
        """The header with session/subsession toggles exists."""
        assert 'id="header"' in ui_html
        assert 'id="sessions-toggle"' in ui_html
        assert 'id="subsessions-toggle"' in ui_html
        assert 'id="connection-dot"' in ui_html

    def test_sessions_panel(self, ui_html: str) -> None:
        """The left sessions sidebar exists."""
        assert 'id="sessions-panel"' in ui_html
        assert 'id="sessions-list"' in ui_html
        assert 'id="new-chat-btn"' in ui_html
        assert 'id="sessions-resize-handle"' in ui_html

    def test_subsessions_panel(self, ui_html: str) -> None:
        """The right subsessions sidebar exists."""
        assert 'id="subsessions-panel"' in ui_html
        assert 'id="subsessions-list"' in ui_html
        assert 'id="subsessions-resize-handle"' in ui_html

    def test_preview_tray(self, ui_html: str) -> None:
        """The image preview tray exists."""
        assert 'id="preview-tray"' in ui_html
        assert 'id="attach-error"' in ui_html

    def test_file_input(self, ui_html: str) -> None:
        """A hidden file input for image upload exists."""
        assert 'id="file-input"' in ui_html
        assert 'type="file"' in ui_html

    def test_cancel_queued_button(self, ui_html: str) -> None:
        """Cancel-queued button exists (hidden by default)."""
        assert 'id="cancel-queued-btn"' in ui_html

    def test_summary_container(self, ui_html: str) -> None:
        """Summary container exists above chat."""
        assert 'id="summary-container"' in ui_html


class TestHtmlMetaAndScripts:
    """Meta tags and external script loading."""

    def test_viewport_meta(self, ui_html: str) -> None:
        """Responsive viewport meta is present."""
        assert 'name="viewport"' in ui_html
        assert "width=device-width" in ui_html

    def test_charset_meta(self, ui_html: str) -> None:
        """UTF-8 charset meta is present."""
        assert 'charset="utf-8"' in ui_html

    def test_project_title_meta(self, ui_html: str) -> None:
        """Project-title meta exists for JS bootstrapping."""
        assert 'name="project-title"' in ui_html

    def test_idle_timeout_meta(self, ui_html: str) -> None:
        """Idle-timeout meta exists for JS bootstrapping."""
        assert 'name="idle-timeout-minutes"' in ui_html

    def test_css_linked(self, ui_html: str) -> None:
        """chat.css is linked via a stylesheet link."""
        assert 'href="/static/chat.css"' in ui_html

    def test_js_loaded(self, ui_html: str) -> None:
        """chat.js is loaded via a script tag."""
        assert 'src="/static/chat.js"' in ui_html


# ---------------------------------------------------------------------------
# Static file serving — HTTP-level
# ---------------------------------------------------------------------------


class TestStaticFileServing:
    """chat.css and chat.js are served with correct content types."""

    @pytest.mark.asyncio
    async def test_css_served_with_correct_content_type(self) -> None:
        """``GET /static/chat.css`` returns ``text/css``."""
        from tests.conftest import mock_app

        async with mock_app() as f:
            response = await f.client.get("/static/chat.css")

        assert response.status_code == 200
        assert "text/css" in response.headers["content-type"]

    @pytest.mark.asyncio
    async def test_js_served_with_correct_content_type(self) -> None:
        """``GET /static/chat.js`` returns a JavaScript content type."""
        from tests.conftest import mock_app

        async with mock_app() as f:
            response = await f.client.get("/static/chat.js")

        assert response.status_code == 200
        ct = response.headers["content-type"].lower()
        assert "javascript" in ct or "ecmascript" in ct

    @pytest.mark.asyncio
    async def test_css_contains_dark_theme_variables(self) -> None:
        """chat.css defines dark-theme CSS custom properties."""
        from tests.conftest import mock_app

        async with mock_app() as f:
            response = await f.client.get("/static/chat.css")

        assert "color-scheme: dark" in response.text
        assert "--chat-bg" in response.text
        assert "--composer-bg" in response.text

    @pytest.mark.asyncio
    async def test_js_starts_with_use_strict(self) -> None:
        """chat.js starts with an IIFE in strict mode."""
        from tests.conftest import mock_app

        async with mock_app() as f:
            response = await f.client.get("/static/chat.js")

        assert '"use strict"' in response.text


# ---------------------------------------------------------------------------
# chat.js — function presence (static analysis)
# ---------------------------------------------------------------------------


class TestChatJsFunctions:
    """Key functions and variables exist in chat.js (static regex scan)."""

    _FUNCTION_RE = re.compile(r"function\s+(\w+)", re.MULTILINE)
    _VAR_RE = re.compile(r"\bvar\s+(\w+)", re.MULTILINE)

    def _functions_in(self, js: str) -> set[str]:
        return set(self._FUNCTION_RE.findall(js))

    def _vars_in(self, js: str) -> set[str]:
        return set(self._VAR_RE.findall(js))

    def test_submit_message_function(self, static_js: str) -> None:
        """``submitMessage`` function exists."""
        assert "submitMessage" in self._functions_in(static_js)

    def test_sse_parser(self, static_js: str) -> None:
        """An SSE stream parser exists (``processSSEStream``)."""
        funcs = self._functions_in(static_js)
        assert "processSSEStream" in funcs

    def test_append_token_function(self, static_js: str) -> None:
        """Token append function exists (``appendToken``)."""
        assert "appendToken" in self._functions_in(static_js)

    def test_show_error_function(self, static_js: str) -> None:
        """Error display function exists (``showError``)."""
        assert "showError" in self._functions_in(static_js)

    def test_show_typing_indicator(self, static_js: str) -> None:
        """Typing indicator functions exist."""
        funcs = self._functions_in(static_js)
        assert "showTypingIndicator" in funcs
        assert "hideTypingIndicator" in funcs

    def test_session_management_functions(self, static_js: str) -> None:
        """Session management functions exist."""
        funcs = self._functions_in(static_js)
        assert "switchSession" in funcs
        assert "fetchSessions" in funcs
        assert "renderSessionList" in funcs
        assert "deleteSession" in funcs

    def test_image_attachment_functions(self, static_js: str) -> None:
        """Image attachment functions exist."""
        funcs = self._functions_in(static_js)
        assert "validateAndAddFiles" in funcs
        assert "renderPreviewTray" in funcs
        assert "removeAttachment" in funcs
        assert "encodeImage" in funcs

    def test_message_queue_variable(self, static_js: str) -> None:
        """The ``messageQueue`` variable exists for busy-state queuing."""
        assert "messageQueue" in self._vars_in(static_js)

    def test_event_stream_lifecycle(self, static_js: str) -> None:
        """Event stream open/close functions exist."""
        funcs = self._functions_in(static_js)
        assert "openEventStream" in funcs
        assert "closeEventStream" in funcs

    def test_idle_timeout_functions(self, static_js: str) -> None:
        """Idle timeout functions exist."""
        funcs = self._functions_in(static_js)
        assert "resetIdleTimer" in funcs
        assert "restartConversation" in funcs

    def test_subsession_functions(self, static_js: str) -> None:
        """Subsession rendering functions exist."""
        funcs = self._functions_in(static_js)
        assert "renderSubsessionsList" in funcs
        assert "upsertSubsession" in funcs
        assert "loadSubsTranscript" in funcs
        assert "closeSubsession" in funcs

    def test_relative_time_function(self, static_js: str) -> None:
        """Relative time formatting function exists."""
        assert "relativeTime" in self._functions_in(static_js)

    def test_client_id_functions(self, static_js: str) -> None:
        """Client id functions exist."""
        funcs = self._functions_in(static_js)
        assert "getClientId" in funcs
        assert "randomId" in funcs
