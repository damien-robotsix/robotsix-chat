"""Browser chat UI loader for the chat server."""

from __future__ import annotations

from importlib import resources

from robotsix_chat import PROJECT_TITLE

# ---------------------------------------------------------------------------
# UI HTML loader — reads the bundled browser UI and fills placeholders
# ---------------------------------------------------------------------------


def _load_ui_html(idle_timeout_minutes: int) -> str:
    """Read the bundled browser UI (``ui/index.html``) and fill placeholders."""
    raw = (resources.files("robotsix_chat") / "ui" / "index.html").read_text(
        encoding="utf-8"
    )
    return raw.replace("{{ PROJECT_TITLE }}", PROJECT_TITLE).replace(
        "{{ IDLE_TIMEOUT_MINUTES }}", str(idle_timeout_minutes)
    )
