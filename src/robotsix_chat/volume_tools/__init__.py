"""Local volume-directory listing tool for the agent.

Exposes :func:`build_volume_tools` — a factory returning a single
read-only ``list_volume_files`` tool that enumerates a directory
under a configured root path.  Returns no tools when disabled.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config.models import VolumeToolsSettings

logger = logging.getLogger(__name__)

__all__ = ["build_volume_tools", "load_volume_tools_skill"]


def build_volume_tools(
    settings: VolumeToolsSettings,
) -> list[Callable[..., Any]]:
    """Return the local volume-listing tool, or an empty list when disabled.

    Args:
        settings: VolumeToolsSettings (``enabled`` master switch,
            ``root_path``).

    Returns:
        A list containing ``list_volume_files``, or ``[]`` when
        *settings.enabled* is ``False``.

    """
    if not settings.enabled:
        return []

    root = Path(settings.root_path).resolve()

    async def list_volume_files(
        path: str = "",
    ) -> str:
        """List the contents of a directory under the configured volume root.

        Returns one line per entry: ``[DIR] name/`` or ``[FILE] name (N bytes)``.
        When *path* is empty or ``"."``, lists the volume root itself.
        Paths that attempt to escape the root are refused.

        Args:
            path: A path relative to the volume root.  Must resolve to a
                directory under the root; file paths and paths outside the
                root are rejected.

        Returns:
            A newline-separated listing, or an error description.

        """
        resolved = _resolve_path(root, path)
        if resolved is None:
            return (
                f"Path {path!r} is outside the allowed root {root!s} — access denied."
            )

        if not resolved.exists():
            return f"Path does not exist: {resolved!s}"

        if not resolved.is_dir():
            return f"Not a directory: {resolved!s}"

        try:
            entries: list[str] = []
            with os.scandir(resolved) as it:
                for entry in sorted(it, key=lambda e: e.name):
                    if entry.is_dir(follow_symlinks=False):
                        entries.append(f"[DIR]  {entry.name}/")
                    elif entry.is_file(follow_symlinks=False):
                        size_str: str
                        try:
                            size_str = str(entry.stat(follow_symlinks=False).st_size)
                        except OSError:
                            size_str = "?"
                        entries.append(f"[FILE] {entry.name} ({size_str} bytes)")
                    else:
                        entries.append(f"[???]  {entry.name}")
            if not entries:
                return f"(empty directory: {resolved!s})"
            return "\n".join(entries)
        except OSError as exc:
            logger.warning("list_volume_files(%r): %s", path, exc)
            return f"Error reading directory {resolved!s}: {exc}"

    return [list_volume_files]


def _resolve_path(root: Path, path: str) -> Path | None:
    """Resolve *path* relative to *root*, rejecting escapes.

    Returns the resolved ``Path``, or ``None`` when *path* escapes the root.
    """
    if not path or path == ".":
        return root

    candidate = (root / path).resolve()
    if root not in candidate.parents and candidate != root:
        return None
    return candidate


def load_volume_tools_skill() -> str:
    """Return the volume-tools skill doc for injection into the system prompt.

    Returns the content of ``skill.md`` as a string, or an empty string
    when the file is missing.
    """
    skill_path = resources.files(__package__) / "skill.md"
    if not skill_path.is_file():
        return ""
    return skill_path.read_text(encoding="utf-8")
