"""Remote-component tooling for the chat agent.

Groups the two component subpackages:

- :mod:`robotsix_chat.component.client` — :func:`build_component_tools`,
  direct-HTTP tools for the ``component_client.components`` allowlist.
- :mod:`robotsix_chat.component.access` — :func:`build_component_access_tools`,
  the central-deploy roster + generic ``component_request`` tool.

The factories are re-exported here for convenience.
"""

from __future__ import annotations

from .access import build_component_access_tools
from .client import build_component_tools

__all__ = ["build_component_access_tools", "build_component_tools"]
