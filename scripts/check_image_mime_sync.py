#!/usr/bin/env python3
"""CI gate: verify the image-upload MIME allowlist agrees across all sites.

The allowlist ``["image/png", "image/jpeg", "image/gif", "image/webp"]`` is
the authoritative ``DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES`` constant in
``src/robotsix_chat/config/settings.py`` (consumed by the
``Settings.allowed_image_media_types`` default and the runtime fallback in
``chat/server/app.py``).  The two static frontend copies are not derived from
config, so they can silently drift:

- ``src/robotsix_chat/ui/static/chat.js`` — ``var ALLOWED_TYPES = [...]``
  (client-side upload gate).
- ``src/robotsix_chat/ui/index.html`` — the ``<input accept="...">`` attribute.

This script extracts the three literals and exits non-zero when any of the
frontend sites disagrees with the Python constant, so an operator-facing
allowlist change cannot ship with the browser offering/validating a different
set than the server accepts.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES = [ ... ] in settings.py (authoritative).
_PY_CONST_RE = re.compile(
    r"DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES\s*:\s*list\[str\]\s*=\s*\[(?P<body>.*?)\]",
    re.DOTALL,
)

# var ALLOWED_TYPES = [ ... ] in chat.js.
_JS_CONST_RE = re.compile(
    r"ALLOWED_TYPES\s*=\s*\[(?P<body>.*?)\]",
    re.DOTALL,
)

# <input ... accept="image/png,image/jpeg,..."> in index.html.
_HTML_ACCEPT_RE = re.compile(r'accept\s*=\s*"(?P<body>[^"]*)"')

# "image/<subtype>" string literal.
_MIME_RE = re.compile(r'"(image/[a-z0-9.+-]+)"')


def _parse_python(path: Path) -> set[str]:
    """Return the MIME strings in the ``DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES`` list."""
    m = _PY_CONST_RE.search(path.read_text(encoding="utf-8"))
    if not m:
        return set()
    return set(_MIME_RE.findall(m.group("body")))


def _parse_js(path: Path) -> set[str]:
    """Return the MIME strings in the chat.js ``ALLOWED_TYPES`` array."""
    m = _JS_CONST_RE.search(path.read_text(encoding="utf-8"))
    if not m:
        return set()
    return set(_MIME_RE.findall(m.group("body")))


def _parse_html_accept(path: Path) -> set[str]:
    """Return the MIME strings in the index.html file-input ``accept`` attribute."""
    for m in _HTML_ACCEPT_RE.finditer(path.read_text(encoding="utf-8")):
        tokens = {t.strip() for t in m.group("body").split(",") if t.strip()}
        if any(t.startswith("image/") for t in tokens):
            return tokens
    return set()


def main() -> int:
    """Check image-MIME allowlist consistency; return 0 (ok) or 1 (violations)."""
    repo_root = Path(__file__).resolve().parent.parent
    settings_py = repo_root / "src" / "robotsix_chat" / "config" / "settings.py"
    chat_js = repo_root / "src" / "robotsix_chat" / "ui" / "static" / "chat.js"
    index_html = repo_root / "src" / "robotsix_chat" / "ui" / "index.html"

    canonical = _parse_python(settings_py)
    if not canonical:
        print(
            "ERROR: DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES not found in"
            f" {settings_py.relative_to(repo_root)}",
            file=sys.stderr,
        )
        return 1

    js_types = _parse_js(chat_js)
    html_types = _parse_html_accept(index_html)

    violations = False
    for label, path, values in (
        ("chat.js ALLOWED_TYPES", chat_js, js_types),
        ("index.html accept attribute", index_html, html_types),
    ):
        if not values:
            violations = True
            print(
                f"ERROR: could not extract image MIME allowlist from"
                f" {path.relative_to(repo_root)} ({label})",
                file=sys.stderr,
            )
            continue
        missing = canonical - values
        extra = values - canonical
        if missing or extra:
            violations = True
            print(
                f"MISMATCH: {label} disagrees with"
                " DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES in settings.py:",
                file=sys.stderr,
            )
            if missing:
                print(f"  missing from {label}: {sorted(missing)}", file=sys.stderr)
            if extra:
                print(f"  extra in {label}: {sorted(extra)}", file=sys.stderr)

    if violations:
        print(
            "\nUpdate the frontend literals to match"
            " DEFAULT_ALLOWED_IMAGE_MEDIA_TYPES, then run"
            " `python scripts/check_image_mime_sync.py` locally to reproduce.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
