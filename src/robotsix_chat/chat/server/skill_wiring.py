"""Skill registry and system-prompt skill injection for the chat agent.

Builds the progressive-disclosure skill inventory, the prompt style and
current-datetime directives, and the ``read_skill`` tool.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from robotsix_chat.config import Settings
from robotsix_chat.continuation import load_continuation_skill
from robotsix_chat.docker_digest import load_docker_digest_skill
from robotsix_chat.epic import load_epic_skill
from robotsix_chat.file_hub_tools import load_file_hub_skill
from robotsix_chat.gateway_route import load_gateway_route_skill
from robotsix_chat.http_probe import load_http_probe_skill
from robotsix_chat.langfuse import load_langfuse_inspect_skill
from robotsix_chat.lifecycle import load_lifecycle_skill
from robotsix_chat.mill_workflow import load_mill_workflow_skill
from robotsix_chat.public_fetch import load_public_fetch_skill
from robotsix_chat.render_url import load_render_url_skill
from robotsix_chat.repo.actions import load_github_actions_skill
from robotsix_chat.repo.direct import load_direct_repo_skill
from robotsix_chat.repo.security import load_github_skill
from robotsix_chat.skill_index import build_skill_index
from robotsix_chat.ticket_poll import load_ticket_poll_skill
from robotsix_chat.volume_tools import load_volume_tools_skill

logger = logging.getLogger(__name__)


# Canonical path to the reply-style directive — the single source of truth
# for how the agent formats replies.  The file is read at agent construction
# time and appended to the system prompt on every build.  It lives INSIDE
# the package (like the per-component skill.md files) so the deployed image
# actually contains it: the old CWD-relative ``docs/prompt-style.md`` did
# not ship, and production silently ran without a style directive from the
# feature's introduction until 2026-09-01.
_PROMPT_STYLE_PATH = Path(__file__).parent / "prompt-style.md"


# Delimiter line that separates the header/description from the actual
# directive in the style file.  Everything after this line (exclusive)
# is injected into the system prompt.
_STYLE_DIRECTIVE_HEADER = "## Style directive"


def _load_prompt_style() -> str:
    """Read the canonical reply-style directive from disk.

    Returns the directive text (the content following the
    ``## Style directive`` header in the packaged ``prompt-style.md``), or
    an empty string if the file is missing — a missing file logs
    a warning but is not fatal (the agent runs without a style
    directive).
    """
    try:
        raw = _PROMPT_STYLE_PATH.read_text()
    except FileNotFoundError:
        logging.getLogger(__name__).warning(
            "Prompt style file not found at %s — agent will run "
            "without a reply-style directive.",
            _PROMPT_STYLE_PATH,
        )
        return ""
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Could not read prompt style file at %s: %s — agent will "
            "run without a reply-style directive.",
            _PROMPT_STYLE_PATH,
            exc,
        )
        return ""

    # Extract the directive section: everything after the
    # "## Style directive" header line.
    header_idx = raw.find(_STYLE_DIRECTIVE_HEADER)
    if header_idx == -1:
        logging.getLogger(__name__).warning(
            "Prompt style file at %s is missing the %r header — "
            "agent will run without a reply-style directive.",
            _PROMPT_STYLE_PATH,
            _STYLE_DIRECTIVE_HEADER,
        )
        return ""

    # Drop the header line itself and any blank lines immediately after it.
    body = raw[header_idx + len(_STYLE_DIRECTIVE_HEADER) :]
    body = body.lstrip("\n").strip()
    if not body:
        logging.getLogger(__name__).warning(
            "Prompt style file at %s has an empty directive section — "
            "agent will run without a reply-style directive.",
            _PROMPT_STYLE_PATH,
        )
        return ""

    return body


def _component_skill_registry(
    settings: Settings,
) -> list[tuple[bool, str, Callable[[], str]]]:
    """Return ``(enabled, name, loader)`` for every component skill on the roster.

    Same shape as :func:`_skill_registry` so both halves of progressive
    disclosure see the component skills too. The roster fetch is cached
    (``central_deploy.roster_cache_ttl``), so the index build and the
    ``read_skill`` tool build — seconds apart — hit the network once.
    Returns ``[]`` when component access is off or the roster is unreachable
    (the agent then only sees the bundled skills, as before).
    """
    if not settings.central_deploy.url:
        return []
    from robotsix_chat.component_access.roster import (
        component_skill_entries,
        fetch_roster_sync,
    )

    try:
        roster = fetch_roster_sync(settings.central_deploy)
    except Exception:
        logging.getLogger(__name__).warning(
            "component skill registry: roster unavailable", exc_info=True
        )
        return []
    return [(True, name, loader) for name, loader in component_skill_entries(roster)]


def _skill_registry(
    settings: Settings,
) -> list[tuple[bool, str, Callable[[], str]]]:
    """Return ``(enabled, name, loader)`` for every bundled and component skill.

    Single source of truth for both halves of progressive disclosure: the
    index built into the system prompt and the ``read_skill`` tool that
    serves bodies on demand. Keeping them on one list is what stops the
    index from advertising a skill the tool cannot fetch. Component skills
    (from the central-deploy roster, named by component id) come last.
    """
    from robotsix_chat.subsessions import load_subsessions_skill

    return _bundled_skill_registry(
        settings, load_subsessions_skill
    ) + _component_skill_registry(settings)


def _bundled_skill_registry(
    settings: Settings,
    load_subsessions_skill: Callable[[], str],
) -> list[tuple[bool, str, Callable[[], str]]]:
    """Return the in-repo skills; see :func:`_skill_registry`."""
    return [
        (True, "subsessions", load_subsessions_skill),
        (True, "mill_workflow", load_mill_workflow_skill),
        (settings.lifecycle.enabled, "lifecycle", load_lifecycle_skill),
        (settings.http_probe.enabled, "http_probe", load_http_probe_skill),
        (settings.docker_digest.enabled, "docker_digest", load_docker_digest_skill),
        (settings.gateway_route.enabled, "gateway_route", load_gateway_route_skill),
        (
            settings.continuation.enabled,
            "continuation",
            load_continuation_skill,
        ),
        (
            settings.langfuse_inspect.enabled,
            "langfuse_inspect",
            load_langfuse_inspect_skill,
        ),
        (settings.public_fetch.enabled, "public_fetch", load_public_fetch_skill),
        (settings.render_url.enabled, "render_url", load_render_url_skill),
        (settings.github_security.enabled, "github_security", load_github_skill),
        (settings.github_actions.enabled, "github_actions", load_github_actions_skill),
        (settings.direct_repo.enabled, "direct_repo", load_direct_repo_skill),
        (settings.volume_tools.enabled, "volume_tools", load_volume_tools_skill),
        (settings.file_hub_tools.enabled, "file_hub_tools", load_file_hub_skill),
        (
            bool(settings.direct_repo.board_api_base_url.strip())
            or bool(settings.central_deploy.url.strip()),
            "ticket_poll",
            load_ticket_poll_skill,
        ),
        (
            bool(settings.direct_repo.board_api_base_url.strip())
            or bool(settings.central_deploy.url.strip()),
            "epic",
            load_epic_skill,
        ),
    ]


def _current_datetime_directive(now: datetime | None = None) -> str:
    """Return the authoritative current-date/time directive.

    The agent has no reliable internal clock: date-relative reasoning
    ("no meeting in the last 7 days", "an upcoming earnings date", "the
    scheduler missed a trigger") is unreliable unless an authoritative
    timestamp is injected into its context.  This directive stamps the
    build-time UTC clock into the system prompt and tells the agent to treat
    it — not its own assumptions — as the source of truth for any
    date-relative conclusion, so it never raises a false "missed event"
    alarm when the scheduled time simply has not arrived yet.

    *now* defaults to :func:`datetime.now` in UTC; it is injectable so tests
    can pin a deterministic value.
    """
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        "## Current date and time (authoritative)\n"
        f"The current date and time is {stamp} (UTC). This clock signal was "
        "injected into your context when this session started; treat it — not "
        "your own assumptions or training-cutoff intuition — as the source of "
        "truth for every date-relative conclusion (e.g. days since an event, "
        "whether a deadline has passed, whether a scheduled meeting or "
        "earnings date is upcoming or overdue, whether a scheduler missed a "
        "trigger). Never conclude that a scheduled event was missed, overdue, "
        "or skipped unless this current date/time is strictly after the "
        "event's scheduled time."
    )


def _inject_skills(
    settings: Settings,
    instruction: str,
    *,
    bare: bool = False,
    now: datetime | None = None,
) -> str:
    """Augment *instruction* with component-access instructions and skill prompts.

    Skill injection is disabled when *bare* is ``True``, but the canonical
    reply-style directive and the authoritative current-date/time directive
    are always appended — they apply to every agent build, not just
    tool-enabled ones.  Each skill gate is independently gated by its own
    settings key (``central_deploy.url``, ``lifecycle.enabled``,
    ``github_security.enabled``).

    *now* is forwarded to :func:`_current_datetime_directive` so tests can pin
    the injected timestamp.
    """
    # Always append the authoritative current-date/time directive so the
    # agent has a reliable clock signal for date-relative reasoning on every
    # build (interactive, bare summariser, and unattended periodic agents).
    instruction = f"{instruction}\n\n{_current_datetime_directive(now)}"

    # Always append the canonical reply-style directive — this is a
    # formatting directive, not a skill, and applies to every agent build.
    style = _load_prompt_style()
    if style:
        instruction = f"{instruction}\n\n{style}"

    if bare:
        return instruction

    # Central-deploy roster — component-access instruction + skill prompts.
    if settings.central_deploy.url:
        instruction = (
            f"{instruction}\n\n"
            "Component access:\n"
            "– You have one generic tool for calling external components: "
            "component_request(component_id, method, path, json_body=None, "
            "params=None). "
            "Each component declares its own API surface as a skill named by "
            "its component id in the skill index at the end of this prompt — "
            "read_skill(<component id>) before the first component_request to "
            "it in a session, and follow it.\n"
            "– Obey each component skill's safety section. When a skill marks "
            "an operation as requiring confirmation, ask the user in "
            "conversation before calling it.\n"
            "– The component summary below and the skill index at the end of "
            "this prompt are the authoritative inventory of the components and "
            "skills you can use. A component or skill listed there is available "
            "to you even though its full instructions are not yet loaded into "
            "your context. Never tell the user that a listed capability does "
            "not exist, or that you have no tool for it, merely because its "
            "skill body is not currently loaded — read the relevant skill with "
            "read_skill(<id>) first, then act.\n"
            "– If the roster is unavailable or a component returns an error, "
            "report the error clearly — do not retry in a loop."
        )
        from robotsix_chat.component_access.roster import (
            build_skill_prompt,
            fetch_roster_sync,
        )

        roster = fetch_roster_sync(settings.central_deploy)
        skill_prompt = build_skill_prompt(roster)
        if skill_prompt:
            instruction = f"{instruction}\n\n{skill_prompt}"

    _skill_entries = _skill_registry(settings)

    index = build_skill_index(
        [(name, loader) for enabled, name, loader in _skill_entries if enabled]
    )
    if index:
        instruction = f"{instruction}\n\n{index}"

    return instruction


def build_skill_tools(settings: Settings) -> list[Callable[..., Any]]:
    """Return the ``read_skill`` tool — the read half of progressive disclosure.

    The system prompt carries only a summary per skill (see
    :mod:`robotsix_chat.skill_index`); this serves the full body when the agent
    decides it needs one. Both halves read the same registry, so the tool can
    always fetch anything the index advertises.
    """
    registry = {
        name: loader for enabled, name, loader in _skill_registry(settings) if enabled
    }
    # Cache successfully loaded skill bodies so we can serve a stale copy if a
    # later load fails (transient I/O error, file locked, etc.).
    _skill_body_cache: dict[str, str] = {}

    def read_skill(name: str) -> str:
        """Read the full instructions for one of your available skills.

        Call this before acting on a capability listed in "Available skills" —
        the prompt carries only a one-line summary of each, and the body holds
        the endpoints, arguments, safety rules and examples. The body stays in
        context afterwards, so read each skill at most once per session.

        Args:
            name: Skill name exactly as listed in "Available skills".

        Returns:
            The skill's full Markdown body, or an error naming the valid
            skills when *name* is not one of them.

        """
        loader = registry.get(name)
        if loader is None:
            available = ", ".join(sorted(registry)) or "(none)"
            return (
                f"No skill named {name!r}. Available skills: {available}. "
                "Use the name exactly as listed in 'Available skills'."
            )

        # Retry up to 2 times with exponential backoff for transient failures.
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                body = loader()
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    logger.warning(
                        "read_skill(%s) attempt %d failed, retrying",
                        name,
                        attempt + 1,
                        exc_info=True,
                    )
                    time.sleep(0.1 * (2**attempt))
                continue
            else:
                # Success — cache and return.
                if body:
                    _skill_body_cache[name] = body
                return body or f"Skill {name!r} is registered but its body is empty."

        # All retries exhausted — try the cache.
        logger.warning("read_skill(%s) failed after 3 attempts", name, exc_info=True)
        cached = _skill_body_cache.get(name)
        if cached:
            logger.info("read_skill(%s) serving stale cached body", name)
            return cached

        return (
            f"Skill {name!r} could not be loaded after multiple attempts: "
            f"{last_exc}. "
            "Inform the user that this skill's instructions are temporarily "
            "unavailable. Offer to proceed based on general knowledge or ask "
            "the user to retry later."
        )

    return [read_skill]
