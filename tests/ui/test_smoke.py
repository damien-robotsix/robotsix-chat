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

    def test_config_panel_mount(self, ui_html: str) -> None:
        """The shared ConfigPanel mount point exists.

        Settings renderer is delegated to @robotsix/ui; the bespoke form is gone.
        """
        assert 'id="config-panel-mount"' in ui_html
        assert 'id="settings-form"' not in ui_html
        assert 'id="settings-error"' not in ui_html
        assert 'id="settings-actions"' not in ui_html

    def test_settings_body_single_child(self, ui_html: str) -> None:
        """``#settings-body`` holds exactly one child — the ConfigPanel mount."""
        match = re.search(
            r'<div id="settings-body">\s*(.*?)\s*</div>', ui_html, re.DOTALL
        )
        assert match is not None
        body = match.group(1)
        assert body.count('id="config-panel-mount"') == 1
        assert "<button" not in body
        assert "<span" not in body
        assert "settings-save-btn" not in body
        assert "settings-save-status" not in body

    def test_header_bar(self, ui_html: str) -> None:
        """The AppShell mount point and header controls exist."""
        assert 'id="appshell-mount"' in ui_html
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

    def test_subsessions_announce_region(self, ui_html: str) -> None:
        """A live region announces new subsession messages to screen readers."""
        assert 'id="subs-announce"' in ui_html
        assert 'aria-live="polite"' in ui_html

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
        """An SSE stream parser exists (``processSSEStream``).

        ``processSSEStream`` is extracted to ``sse-parser.js`` and
        imported at the top of ``chat.js`` — verify the import is present.
        """
        assert 'import { processSSEStream } from "./sse-parser.js"' in static_js

    def test_append_token_function(self, static_js: str) -> None:
        """Token append function exists (``appendToken``)."""
        assert "appendToken" in self._functions_in(static_js)

    def test_owner_for_helper_exists(self, static_js: str) -> None:
        """``ownerFor`` + ``PERIODIC_OWNER`` scope per-session requests.

        Periodic sessions are owned by the fixed ``"periodic"`` owner,
        not the browser's clientId; per-session requests must resolve the
        real owner so the operator can view and reply to them.
        """
        assert "ownerFor" in self._functions_in(static_js)
        assert "PERIODIC_OWNER" in self._vars_in(static_js)
        assert '"periodic"' in static_js

    def test_fetch_sessions_includes_periodic(self, static_js: str) -> None:
        """``fetchSessions`` also fetches the periodic-owned sessions.

        Regression: previously it only queried ``owner_id=<clientId>``, so
        scheduler-owned sessions were invisible in the UI.
        """
        assert "PERIODIC_OWNER" in static_js
        # both the client's own list and the periodic list are fetched
        assert static_js.count("/sessions?owner_id=") >= 2

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

    def test_subsession_focus_mode(self, static_js: str) -> None:
        """Subsession focus-mode functions and state exist."""
        funcs = self._functions_in(static_js)
        assert "toggleSubsFocus" in funcs
        assert "enterSubsFocus" in funcs
        assert "exitSubsFocus" in funcs
        assert "getSelectedSub" in funcs
        vars_ = self._vars_in(static_js)
        assert "focusedSubId" in vars_
        assert "selectedSubId" in vars_

    def test_subsession_focus_controls(self, ui_html: str) -> None:
        """Focus-mode exit button and screen-reader announcer exist."""
        assert 'id="subs-focus-exit"' in ui_html
        assert 'id="sr-announce"' in ui_html
        assert 'aria-live="polite"' in ui_html

    def test_subsession_unread_functions(self, static_js: str) -> None:
        """Nested subsession unread-propagation helpers exist."""
        funcs = self._functions_in(static_js)
        assert "subsUnreadTotal" in funcs
        assert "markSubsessionRead" in funcs
        assert "applyPendingUnread" in funcs

    def test_relative_time_function(self, static_js: str) -> None:
        """Relative time formatting function exists."""
        assert "relativeTime" in self._functions_in(static_js)

    def test_owner_is_a_fixed_constant(self, static_js: str) -> None:
        """The UI must not mint a per-browser identity.

        A localStorage-backed random client id made every new computer,
        browser, and private window its own owner — each served an empty
        session list.  Single-user deployment: the owner is a constant.
        """
        funcs = self._functions_in(static_js)
        assert "getClientId" not in funcs
        assert "var clientId = OPERATOR_OWNER;" in static_js
        assert 'var OPERATOR_OWNER = "operator";' in static_js
        # randomId survives only as the offline fallback session id.
        assert "randomId" in funcs
        assert "-client-id" not in static_js

    def test_presets_editor_functions(self, static_js: str) -> None:
        """Presets editor functions for periodic.sessions exist."""
        funcs = self._functions_in(static_js)
        assert "renderPresetsEditor" in funcs
        assert "rebuildPresetRows" in funcs
        assert "renderPresetRow" in funcs
        assert "showPresetForm" in funcs
        assert "makeFormRow" in funcs
        assert "savePresetForm" in funcs
        assert "deletePreset" in funcs
        assert "addPreset" in funcs

    def test_config_panel_initialiser(self, static_js: str) -> None:
        """The one-time ConfigPanel initialiser exists in chat.js."""
        assert "_initConfigPanel" in self._functions_in(static_js)
        assert "mountConfigPanel" in static_js

    def test_config_panel_degradation_message(self, static_js: str) -> None:
        """The missing-vendor fallback message is present in-source.

        Confirms the failure path (vanilla.js absent) shows the operator a
        clear next step instead of a blank settings panel.
        """
        assert "Settings panel unavailable — vendor assets missing." in static_js


class TestChatCssUnreadStyles:
    """chat.css must style nested subsession unread state accessibly."""

    def test_subsession_unread_styles(self, static_css: str) -> None:
        """Unread row + count-badge styles exist (color is not the only cue)."""
        assert ".subs-row.subs-row-unread" in static_css
        assert ".unread-badge" in static_css

    def test_screen_reader_only_utility(self, static_css: str) -> None:
        """The visually-hidden utility used by the announce region exists."""
        assert ".sr-only" in static_css


class TestNavItemsNoDeadLinks:
    """The header nav must not advertise paths the chat server does not serve."""

    _DEAD_HREFS = ("/board/", "/file-hub/", "/central-deploy/")

    def test_dead_nav_links_absent(self, static_js: str) -> None:
        """``chat.js`` does not pass dead-link hrefs to ``mountAppShell``."""
        for href in self._DEAD_HREFS:
            assert href not in static_js, (
                f"Dead nav link {href!r} still present in chat.js"
            )
