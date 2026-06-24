"""Component agent package for robotsix-chat.

Contains the config contract + validation module (``config_contract``)
and the broker responder (``responder``) that registers this component
as an embedded agent on the agent-comm broker, serving ``monitor``,
``config-get``, and ``config-set`` request kinds.

This package is importable without ``robotsix_agent_comm`` installed.
"""
