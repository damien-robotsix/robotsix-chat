# vulture whitelist — symbols listed here are excluded from dead-code detection.
# Each entry must include a brief reason so future maintainers know why it
# appears unused.

from src.robotsix_chat.config import Settings

# pydantic hook — called by pydantic's model initialisation machinery
Settings.model_post_init  # noqa: B018  # unused method (pydantic hook)
# pydantic __context parameter — required by pydantic's model_post_init signature
Settings.model_post_init.__context  # noqa: B018  # unused variable (pydantic param)

# public API — used from tests/test_config.py
Settings.from_env  # noqa: B018  # unused method (used from tests)
