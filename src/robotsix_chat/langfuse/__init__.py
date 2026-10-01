"""Langfuse trace-inspection tool — query and summarise agent traces.

Lets the agent fetch recent Langfuse traces by id or by ticket id (tag
search) via the Langfuse public REST API.  Uses HTTP Basic auth with the
main ``langfuse`` credentials — no separate credential fields needed.

Exposes :func:`build_langfuse_inspect_tools` — a factory returning the LLM
tool.  Returns no tools when disabled, so the chat runs exactly as before.
Also exposes :func:`load_langfuse_inspect_skill` which returns the
component skill markdown for injection into the agent instruction.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import LangfuseInspectSettings, LangfuseSettings

from robotsix_chat.common.http import HttpResult, safe_http_request
from robotsix_chat.config.models import PROJECT_MAIN

__all__ = ["build_langfuse_inspect_tools", "load_langfuse_inspect_skill"]


def load_langfuse_inspect_skill() -> str:
    """Return the Langfuse-inspect component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _basic_auth_header(public_key: str, secret_key: str) -> str:
    """Build a Basic auth header value from a public/secret key pair."""
    credentials = f"{public_key}:{secret_key}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


# Retry constants for transient HTTP failures (timeouts, 5xx, network errors).
# Auth/client errors (401, 403, 4xx) are never retried — they indicate a
# configuration problem, not a blip.
_MAX_RETRIES = 2
_RETRY_BACKOFF_BASE = 1.0  # seconds
_RETRY_BACKOFF_CAP = 5.0  # seconds


async def _retry_safe_http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    params: dict[str, str] | None = None,
    label: str = "Langfuse API",
) -> HttpResult:
    """Call ``safe_http_request`` with retry on transient errors.

    Transient errors (timeouts, 5xx, bare network failures) are retried
    up to ``_MAX_RETRIES`` times with exponential backoff.  Auth/client
    errors (401, 403, 4xx) are returned immediately — they are not
    transient and retrying wastes time.
    """
    for attempt in range(_MAX_RETRIES + 1):
        result = await safe_http_request(
            method,
            url,
            headers=headers,
            timeout=timeout,
            params=params,
            label=label,
        )
        if result.ok:
            return result
        # Auth / client errors are not transient — surface immediately.
        if result.status_code is not None and 400 <= result.status_code < 500:
            return result
        if attempt < _MAX_RETRIES:
            delay = min(_RETRY_BACKOFF_BASE * (2**attempt), _RETRY_BACKOFF_CAP)
            await asyncio.sleep(delay)
    return result  # last attempt's error (all retries exhausted)


def _summarise_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Extract a compact summary from a single Langfuse API trace object."""
    usage = trace.get("metrics", {}).get("usage", {}) or {}
    return {
        "id": trace.get("id", ""),
        "name": trace.get("name", ""),
        "timestamp": trace.get("timestamp", ""),
        "userId": trace.get("userId", ""),
        "latency": trace.get("latency"),
        "totalCost": trace.get("totalCost"),
        "usage": {
            "promptTokens": usage.get("promptTokens"),
            "completionTokens": usage.get("completionTokens"),
            "cachedPromptTokens": usage.get("cachedPromptTokens"),
            "totalTokens": usage.get("totalTokens"),
        },
        "observations": len(trace.get("observations", [])),
        "scores": [
            {"name": s.get("name", ""), "value": s.get("value")}
            for s in trace.get("scores", [])
        ],
    }


def _resolve_project_list(project: str | list[str], configured: list[str]) -> list[str]:
    """Resolve the ``project`` selector into an ordered list of project names.

    Accepts the flexible forms the ``inspect_langfuse_trace`` tool exposes:

    - ``""`` / empty list — defaults to ``[PROJECT_MAIN]`` (backward compatible).
    - ``"all"`` (a bare string, or present inside a list) — expands to every
      configured project name, falling back to ``[PROJECT_MAIN]`` when none are
      configured.
    - ``"a,b,c"`` — a comma-separated string split into names.
    - ``["a", "b"]`` — a list of names (also split on commas, trimmed).

    Duplicate names are removed while preserving first-seen order.
    """
    raw: list[str] = []
    if isinstance(project, str):
        raw.extend(part.strip() for part in project.split(","))
    else:
        for entry in project:
            if isinstance(entry, str):
                raw.extend(part.strip() for part in entry.split(","))
    names = [name for name in raw if name]
    if any(name.lower() == "all" for name in names):
        return list(configured) or [PROJECT_MAIN]
    if not names:
        return [PROJECT_MAIN]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def build_langfuse_inspect_tools(
    inspect_settings: LangfuseInspectSettings,
    langfuse_settings: LangfuseSettings,
) -> list[Callable[..., Any]]:
    """Return the ``inspect_langfuse_trace`` tool, or an empty list when disabled.

    Args:
        inspect_settings: LangfuseInspect configuration (``enabled`` master
            switch, ``max_traces`` cap).
        langfuse_settings: The canonical Langfuse block; credentials are
            resolved per-project at call time based on the ``project``
            parameter. Defaults to PROJECT_MAIN when no project is specified.

    Returns:
        A single-element list containing the ``inspect_langfuse_trace`` async
        callable, or ``[]`` when *inspect_settings.enabled* is ``False``.

    """
    if not inspect_settings.enabled:
        return []

    # Resolve settings at build time so the closure captures them.
    host = langfuse_settings.host.rstrip("/")
    max_traces = inspect_settings.max_traces

    async def inspect_langfuse_trace(
        trace_id: str = "",
        ticket_id: str = "",
        project: str | list[str] = "",
        limit: int = 5,
        from_timestamp: str = "",
        to_timestamp: str = "",
    ) -> str:
        """Fetch and summarise Langfuse traces.

        Fetches traces from the configured Langfuse host with one of these
        search modes:

        - *trace_id* — fetch a single trace by its id.
        - *ticket_id* — search for traces tagged ``ticket_id:<value>``.
        - *from_timestamp* / *to_timestamp* — time-range search (ISO 8601,
          e.g. ``2026-08-01T00:00:00Z``).  Either or both may be provided.
        - Combine *ticket_id* with time-range filters to narrow results.

        At least one search criterion (*trace_id*, *ticket_id*,
        *from_timestamp*, or *to_timestamp*) must be provided.  *trace_id*
        is mutually exclusive with the other criteria.

        Args:
            trace_id: A specific Langfuse trace id to fetch.
            ticket_id: A ticket id to search for in trace tags.
            project: Langfuse project selector (optional, defaults to the chat
                project when empty). Accepts a single project name
                (``"robotsix-mill"``), a comma-separated string or list of
                names (``["robotsix-mill", "robotsix-ci-fix"]``) to query
                several projects at once, or the special value ``"all"`` to
                query every configured project. When more than one project is
                queried, each summary carries a ``project`` field and the
                combined ``traces`` list is sorted by ``totalCost`` descending.
            limit: Maximum number of traces to return per project (capped by
                the configured max).  Ignored when *trace_id* is set.
            from_timestamp: ISO 8601 start of time range (inclusive).
            to_timestamp: ISO 8601 end of time range (inclusive).

        Returns:
            A JSON string with a ``traces`` list of summarised trace
            objects, or an ``error`` field on failure.  Multi-project queries
            also echo the resolved ``projects`` list and, when some projects
            failed, a per-project ``errors`` map.

        """
        if trace_id and ticket_id:
            return json.dumps(
                {
                    "traces": [],
                    "error": (
                        "Provide exactly one of trace_id or ticket_id, not both."
                    ),
                },
                ensure_ascii=False,
            )
        if trace_id and (from_timestamp or to_timestamp):
            return json.dumps(
                {
                    "traces": [],
                    "error": (
                        "trace_id is mutually exclusive with from_timestamp "
                        "and to_timestamp."
                    ),
                },
                ensure_ascii=False,
            )
        if not trace_id and not ticket_id and not from_timestamp and not to_timestamp:
            return json.dumps(
                {
                    "traces": [],
                    "error": (
                        "Provide at least one of trace_id, ticket_id, "
                        "from_timestamp, or to_timestamp."
                    ),
                },
                ensure_ascii=False,
            )

        # Clamp limit to configured max (applied per project).
        effective_limit = min(max(1, limit), max_traces)

        # Resolve the project selector into an ordered list of project names.
        # An empty selector defaults to the main chat project (backward
        # compatible); "all" expands to every configured project.
        projects = _resolve_project_list(
            project, list(langfuse_settings.projects.keys())
        )

        async def _fetch_for_project(
            project_name: str,
        ) -> tuple[list[dict[str, Any]] | None, str | None]:
            """Query one project; return ``(summaries, None)`` or ``(None, error)``."""
            creds = langfuse_settings.creds(project_name)
            pk = creds.public_key.get_secret_value()
            sk = creds.secret_key.get_secret_value()
            if not pk or not sk:
                return None, (
                    f"Langfuse project '{project_name}' is not configured — "
                    "the inspect tool cannot authenticate."
                )
            headers = {"Authorization": _basic_auth_header(pk, sk)}

            if trace_id:
                # Fetch a single trace by id.
                single_url = f"{host}/api/public/traces/{trace_id}"
                single: HttpResult = await _retry_safe_http_request(
                    "GET",
                    single_url,
                    headers=headers,
                    timeout=30.0,
                    label="Langfuse API",
                )
                if single.error:
                    return None, single.error
                trace_data: dict[str, Any] = json.loads(single.text or "{}")
                return [_summarise_trace(trace_data)], None

            # Build query params for the list endpoint.
            params: dict[str, str] = {
                "limit": str(effective_limit),
                "orderBy": "timestamp.desc",
            }
            if ticket_id:
                params["tags"] = f"ticket_id:{ticket_id}"
            if from_timestamp:
                params["fromTimestamp"] = from_timestamp
            if to_timestamp:
                params["toTimestamp"] = to_timestamp

            list_url = f"{host}/api/public/traces"
            listed: HttpResult = await _retry_safe_http_request(
                "GET",
                list_url,
                headers=headers,
                params=params,
                timeout=30.0,
                label="Langfuse API",
            )
            if listed.error:
                return None, listed.error
            page: dict[str, Any] = json.loads(listed.text or "{}")
            raw_traces: list[dict[str, Any]] = page.get("data", [])
            return [_summarise_trace(t) for t in raw_traces], None

        # Single-project query — preserve the original response shape exactly.
        if len(projects) == 1:
            traces, error = await _fetch_for_project(projects[0])
            if error is not None:
                return json.dumps(
                    {"traces": [], "error": error},
                    ensure_ascii=False,
                )
            if trace_id:
                return json.dumps(
                    {"traces": traces},
                    ensure_ascii=False,
                )
            response: dict[str, Any] = {
                "traces": traces,
                "limit": effective_limit,
            }
            if ticket_id:
                response["ticket_id"] = ticket_id
            if from_timestamp:
                response["from_timestamp"] = from_timestamp
            if to_timestamp:
                response["to_timestamp"] = to_timestamp
            return json.dumps(response, ensure_ascii=False)

        # Multi-project query — aggregate, tag each summary with its project,
        # and sort the combined list by totalCost descending.
        all_traces: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        for project_name in projects:
            traces, error = await _fetch_for_project(project_name)
            if error is not None:
                errors[project_name] = error
                continue
            for summary in traces or []:
                summary["project"] = project_name
                all_traces.append(summary)
        all_traces.sort(
            key=lambda summary: summary.get("totalCost") or 0.0,
            reverse=True,
        )
        multi_response: dict[str, Any] = {
            "traces": all_traces,
            "limit": effective_limit,
            "projects": projects,
        }
        if ticket_id:
            multi_response["ticket_id"] = ticket_id
        if from_timestamp:
            multi_response["from_timestamp"] = from_timestamp
        if to_timestamp:
            multi_response["to_timestamp"] = to_timestamp
        if errors:
            multi_response["errors"] = errors
        return json.dumps(multi_response, ensure_ascii=False)

    return [inspect_langfuse_trace]
