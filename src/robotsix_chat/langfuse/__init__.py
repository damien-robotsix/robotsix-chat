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
    ) -> str:
        """Fetch and summarise Langfuse traces for a given trace or ticket id.

        Fetches one or more Langfuse traces from the configured Langfuse
        host: either a single trace by its *trace_id*, or the most recent
        traces whose tags include ``ticket_id:<value>`` up to *limit*.

        Exactly one of *trace_id* or *ticket_id* must be provided.

        Args:
            trace_id: A specific Langfuse trace id to fetch.
            ticket_id: A ticket id to search for in trace tags.
            limit: Maximum number of traces to return when searching by
                ticket id (capped by the configured max).  Ignored when
                *trace_id* is set.

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
        if not trace_id and not ticket_id:
            return json.dumps(
                {"traces": [], "error": "Provide either trace_id or ticket_id."},
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
            result: HttpResult = await safe_http_request(
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

        # Search by ticket_id tag.
        tag_value = f"ticket_id:{ticket_id}"
        params = {
            "tags": tag_value,
            "limit": str(effective_limit),
            "orderBy": "timestamp.desc",
        }
        url = f"{host}/api/public/traces"
        result = await safe_http_request(
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
        return json.dumps(
            {
                "traces": traces,
                "ticket_id": ticket_id,
                "limit": effective_limit,
            },
            ensure_ascii=False,
        )

    return [inspect_langfuse_trace]
