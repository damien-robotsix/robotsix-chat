# vulture whitelist — symbols listed here are excluded from dead-code detection.
# Each entry must include a brief reason so future maintainers know why it
# appears unused.

from src.robotsix_chat.chat.conversation import ConversationStore
from src.robotsix_chat.chat.events import (  # noqa: F811
    SSE_PENDING_QUESTION_ADDED_TYPE,
    SSE_PENDING_QUESTION_REMOVED_TYPE,
    SSE_PENDING_QUESTION_THREAD_MESSAGE_TYPE,
    SSE_PENDING_QUESTION_UPDATED_TYPE,
)
from src.robotsix_chat.config import SYSTEM_PROMPT_VERSION, Settings

# pydantic hook — called by pydantic's model initialisation machinery
Settings.model_post_init  # noqa: B018  # unused method (pydantic hook)
# pydantic __context parameter — required by pydantic's model_post_init signature
Settings.model_post_init.__context  # noqa: B018  # unused variable (pydantic param)

# public API — used from tests/test_config.py
Settings.from_env  # noqa: B018  # unused method (used from tests)

# conversation.py — retained for caller compatibility; parameter accepted but
# no longer triggers destructive history reset (docstring documents this)
ConversationStore._idle_reset_seconds  # noqa: B018  # unused attribute (caller compat)

# conversation.py — public API consumed from tests/test_conversation.py
ConversationStore.record_for_owner  # noqa: B018  # unused method (used from tests)

# events.py — event-type constants whose string values are used inline in
# store.py, server.py, and index.html; the constant names are kept for
# discoverability/documentation
SSE_PENDING_QUESTION_ADDED_TYPE  # noqa: B018  # unused variable (doc/discoverability)
SSE_PENDING_QUESTION_UPDATED_TYPE  # noqa: B018  # unused variable (doc/discoverability)
SSE_PENDING_QUESTION_REMOVED_TYPE  # noqa: B018  # unused variable (doc/discoverability)
SSE_PENDING_QUESTION_THREAD_MESSAGE_TYPE  # noqa: B018  # unused variable (doc/discoverability)

# config.py — public API imported and asserted in test_system_prompt_governance.py
SYSTEM_PROMPT_VERSION  # noqa: B018  # unused variable (used from tests)
