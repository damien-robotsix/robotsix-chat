# vulture whitelist — symbols listed here are excluded from dead-code detection.
# Each entry must include a brief reason so future maintainers know why it
# appears unused.

from src.robotsix_chat.chat.conversation import ConversationStore
from src.robotsix_chat.config import SYSTEM_PROMPT_VERSION, Settings
from src.robotsix_chat.diagnostics.fixes import FixProposalStore
from src.robotsix_chat.diagnostics.store import DiagnosticStore

# pydantic hook — called by pydantic's model initialisation machinery
Settings.model_post_init  # noqa: B018  # unused method (pydantic hook)
# pydantic __context parameter — required by pydantic's model_post_init signature
Settings.model_post_init.__context  # noqa: B018  # unused variable (pydantic param)

# conversation.py — retained for caller compatibility; parameter accepted but
# no longer triggers destructive history reset (docstring documents this)
ConversationStore._idle_reset_seconds  # noqa: B018  # unused attribute (caller compat)

# conversation.py — public API consumed from tests/test_conversation.py
ConversationStore.record_for_owner  # noqa: B018  # unused method (used from tests)

# config.py — public API imported and asserted in test_system_prompt_governance.py
SYSTEM_PROMPT_VERSION  # noqa: B018  # unused variable (used from tests)

# diagnostics/ — store + proposal accessors for the in-progress diagnostics
# feature; defined ahead of their callers (see the diagnostics child tickets)
FixProposalStore.get_proposal  # noqa: B018  # unused method (diagnostics, not yet wired)
DiagnosticStore.record_event  # noqa: B018  # unused method (diagnostics, not yet wired)
DiagnosticStore.get_event  # noqa: B018  # unused method (diagnostics, not yet wired)
