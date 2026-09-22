"""Agent configuration endpoint — query model tier for all agents.

Provides a consolidated view of model tier configuration for:
- The chat service itself (chat_default_model_level, summary_model_level)
- All registered component agents (mail, linkedin, implement, mill, ci_fix, etc.)

Enables cost analysis and model-tier optimization recommendations.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from robotsix_chat.component_client import ComponentAgentClient

logger = logging.getLogger(__name__)


async def agents_config_endpoint(request: Request) -> JSONResponse:
    """Return model tier configuration for all agents in the system.

    ``GET /agents/config`` returns::

        {
          "agents": {
            "chat": {
              "model_level": 2,
              "summary_model_level": 1,
              "component_id": "chat"
            },
            "mill": {
              "model_level": 2,
              "component_id": "mill",
              "error": null
            },
            "linkedin": {
              "model_level": 1,
              "component_id": "linkedin",
              "error": null
            },
            ...
          }
        }

    The response includes the chat service's own model configuration plus
    model tier data for each registered component agent. Missing or
    unreachable agents appear with an "error" field describing the issue.

    This endpoint enables cost-review and model-tier optimization analysis
    by providing a consolidated view of which agents are running on which
    capability levels.
    """
    # 1. Build chat's own agent configuration.
    chat_config = {
        "model_level": getattr(request.app.state, "chat_model_level", None),
        "summary_model_level": getattr(
            request.app.state, "chat_summary_model_level", None
        ),
        "component_id": "chat",
    }

    agents: dict[str, Any] = {"chat": chat_config}

    # 2. Query component agent configs from the component client.
    component_client: ComponentAgentClient | None = getattr(
        request.app.state, "component_client", None
    )
    if component_client is None:
        return JSONResponse({"agents": agents})

    # Get the list of registered component targets from the config.
    component_targets: dict[str, Any] = getattr(
        request.app.state, "component_client_config", {}
    )
    components = component_targets.get("components", [])

    # 3. Query each component's config asynchronously.
    for component in components:
        component_id = component.get("component_id") or component.get("label", "unknown")
        base_url = component.get("base_url")

        if not base_url:
            agents[component_id] = {
                "component_id": component_id,
                "error": "No base_url configured",
            }
            continue

        try:
            # POST to the component's config endpoint.
            config_response = await component_client.config_get(base_url)
            # Parse the response — it should be JSON.
            try:
                config_data = json.loads(config_response)
                # Extract model_level if present in the config.
                model_level = config_data.get("model_level")
                agents[component_id] = {
                    "component_id": component_id,
                    "model_level": model_level,
                    "error": None,
                }
            except json.JSONDecodeError:
                agents[component_id] = {
                    "component_id": component_id,
                    "error": f"Invalid JSON response: {config_response[:100]}",
                }
        except Exception as exc:
            logger.warning(
                "Failed to query config for component '%s': %s", component_id, exc
            )
            agents[component_id] = {
                "component_id": component_id,
                "error": str(exc),
            }

    return JSONResponse({"agents": agents})
