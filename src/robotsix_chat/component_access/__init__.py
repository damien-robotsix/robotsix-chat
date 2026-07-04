"""Generic component access via the central-deploy roster + chat-skill endpoints.

Exposes :func:`build_component_access_tools` — a factory that, when a
``central_deploy.url`` is configured, fetches the roster of allowed
components, loads each component's skill into the agent's system prompt,
and returns a single generic ``component_request`` tool that the LLM uses
to call any component's API.

When ``central_deploy.url`` is empty, returns no tools and adds no
system-prompt guidance.
"""

from __future__ import annotations

from .tools import build_component_access_tools

__all__ = ["build_component_access_tools"]
