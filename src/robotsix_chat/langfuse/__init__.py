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
            "totalTokens": usage.get("totalTokens"),
        },
        "observations": len(trace.get("observations", [])),
        "scores": [
            {"name": s.get("name", ""), "value": s.get("value")}
            for s in trace.get("scores", [])
        ],
    }


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
        langfuse_settings: The canonical Langfuse block; the main project's
            credentials (``PROJECT_MAIN``) are used for API authentication.

    Returns:
        A single-element list containing the ``inspect_langfuse_trace`` async
        callable, or ``[]`` when *inspect_settings.enabled* is ``False``.

    """
    if not inspect_settings.enabled:
        return []

    # Resolve secrets at build time so the closure captures plain strings.
    creds = langfuse_settings.creds(PROJECT_MAIN)
    pk = creds.public_key.get_secret_value()
    sk = creds.secret_key.get_secret_value()
    host = langfuse_settings.host.rstrip("/")
    max_traces = inspect_settings.max_traces

    async def inspect_langfuse_trace(
        trace_id: str = "",
        ticket_id: str = "",
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
            limit: Maximum number of traces to return (capped by the
                configured max).  Ignored when *trace_id* is set.
            from_timestamp: ISO 8601 start of time range (inclusive).
            to_timestamp: ISO 8601 end of time range (inclusive).

        Returns:
            A JSON string with a ``traces`` list of summarised trace
            objects, or an ``error`` field on failure.

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

        if not pk or not sk:
            return json.dumps(
                {
                    "traces": [],
                    "error": (
                        "Langfuse credentials (public_key + secret_key) are "
                        "not configured — the inspect tool cannot authenticate."
                    ),
                },
                ensure_ascii=False,
            )

        auth = _basic_auth_header(pk, sk)
        headers = {"Authorization": auth}

        # Clamp limit to configured max.
        effective_limit = min(max(1, limit), max_traces)

        if trace_id:
            # Fetch a single trace by id.
            url = f"{host}/api/public/traces/{trace_id}"
            result: HttpResult = await _retry_safe_http_request(
                "GET",
                url,
                headers=headers,
                timeout=30.0,
                label="Langfuse API",
            )
            if result.error:
                return json.dumps(
                    {"traces": [], "error": result.error},
                    ensure_ascii=False,
                )
            trace_data: dict[str, Any] = json.loads(result.text or "{}")
            return json.dumps(
                {"traces": [_summarise_trace(trace_data)]},
                ensure_ascii=False,
            )

        # Build query params for list endpoint.
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

        url = f"{host}/api/public/traces"
        result = await _retry_safe_http_request(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout=30.0,
            label="Langfuse API",
        )
        if result.error:
            return json.dumps(
                {"traces": [], "error": result.error},
                ensure_ascii=False,
            )

        page: dict[str, Any] = json.loads(result.text or "{}")
        raw_traces: list[dict[str, Any]] = page.get("data", [])
        traces = [_summarise_trace(t) for t in raw_traces]
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

    return [inspect_langfuse_trace]
