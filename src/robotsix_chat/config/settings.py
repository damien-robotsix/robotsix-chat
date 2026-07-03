"""Top-level :class:`Settings` model and its factories.

Composes the sub-models from :mod:`robotsix_chat.config.models` and
loads from a single JSON file located by ``ROBOTSIX_CONFIG_FILE``.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, SecretStr
from robotsix_config import load_config
from robotsix_llmio.config import TierLevel

from robotsix_chat.config.constants import level_needs_api_key
from robotsix_chat.config.models import (
    BoardSettings,
    CalendarSettings,
    ComponentAgentSettings,
    ComponentClientSettings,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    KnowledgeSettings,
    LangfuseSettings,
    MailSettings,
    MemorySettings,
    MillSettings,
    RefDocsSettings,
    SelfReviewSettings,
    SkillsSettings,
    SubsessionsSettings,
    VersionCheckSettings,
)

logger = logging.getLogger(__name__)

# Version stamp for the agent_instruction default literal.
# Bump on every change to Settings.agent_instruction and update
# docs/system_prompt_changelog.md with a new entry + SHA256.
SYSTEM_PROMPT_VERSION = 15

# Valid model levels, derived from llmio's tier enum (import-time constant so
# the set is built once and can never drift from the tiers llmio ships).
_VALID_MODEL_LEVELS = frozenset(
    int(level.value.removeprefix("level")) for level in TierLevel
)


class Settings(BaseModel):
    """Application settings, loaded from a single JSON config file.

    The LLM is configured the robotsix-llmio way — pick a capability
    ``model_level`` and llmio resolves the provider + model for that level
    (from its baked default :class:`~robotsix_llmio.config.TierLevelConfig`).

    Attributes:
        llmio_model_level: Capability level — ``1`` (cheapest/fastest) to
            ``4`` (frontier). The level encodes the provider + model: by
            default levels 1-2 use ``openrouter``, level 3 uses
            ``claudeSDK``/``opus``, level 4 ``claudeSDK``/``claude-fable-5``.
        llmio_api_key: Provider API key, forwarded to llmio when the chosen
            level's provider needs one (e.g. ``openrouter``); unused
            by keyless providers like ``claudeSDK``.
        agent_instruction: System instruction handed to the LLM agent.
            Includes guidance on spawning subsessions for background work.
        server_host: Host address the chat SSE server binds to.
        server_port: Port the chat SSE server listens on.
        idle_timeout_minutes: Minutes of no user activity before the UI
            auto-restarts the conversation; ``0`` disables the feature.
        subsessions: Unified subsession system (background/periodic/user-chat
            sub-agents) — see :class:`SubsessionsSettings`.
        log_level: Python logging level name.
        cors_allow_origins: Origins allowed to call /chat cross-origin
            (empty = none; ``["*"]`` = any). Only needed when the browser
            UI is hosted on a different origin than the server.
        correlation_id_header: HTTP header name used for the correlation /
            request-id (both inbound and outbound). Default ``X-Request-ID``.
        langfuse: Main-agent Langfuse observability credentials.
        max_images_per_message: Maximum number of images a client may attach to
            a single ``POST /chat`` request.  Default ``8``.
        max_image_bytes: Maximum decoded size (bytes) of a single attached
            image.  Default ``5_242_880`` (5 MiB).
        allowed_image_media_types: Media types accepted for image attachments.
            Default ``["image/png", "image/jpeg", "image/gif", "image/webp"]``.

    """

    llmio_model_level: int = 3
    llmio_api_key: SecretStr = SecretStr("")
    agent_instruction: str = (
        "You are a helpful assistant. "
        "You have a local, durable knowledge base "
        "(add/append/update/list/read_knowledge_note) "
        "for operational notes and lessons you deliberately author — "
        "consult it at the start of every session and write durable "
        "findings to it. Unlike the stable, human-governed system "
        "prompt (which you must not modify), these notes are yours to "
        "author and revise by id. This store is distinct from the "
        "automatic cognee conversation memory — cognee recalls past "
        "exchanges by similarity, while these notes you explicitly "
        "create and address by id. "
        "Answer quick questions inline."
        "\n\n"
        "Subsessions:\n"
        "– spawn_subsession offloads work to a background sub-agent that "
        "has the same tools you do. Three kinds: 'task' (one-shot job — "
        "multi-step research, long generation, anything that would stall "
        "your reply), 'periodic' (re-runs instructions on an interval — "
        "monitoring, polling), and 'user_chat' (a side-chat with the user "
        "for a focused question or decision — use it instead of blocking "
        "this conversation while you wait for an answer).\n"
        "– Pick model_level by difficulty and cost: 1-2 are cheap "
        "OpenRouter tiers for trivial polling or extraction (only when an "
        "API key is configured), 3 is the default for general work, 4 is "
        "the frontier tier — reserve it for genuinely hard reasoning. "
        "Never spawn at level 4 for routine checks.\n"
        "– Write instructions that are complete and self-contained: the "
        "subsession starts with NO conversation history, so include every "
        "id, URL, constraint, and expected outcome it needs.\n"
        "– The subsession's summary arrives in this conversation when it "
        "closes. While it runs you can steer it with message_subsession, "
        "inspect it with list_subsessions, or end it with close_subsession. "
        "Tell the user the work is running in the background.\n"
        "– Inside a subsession, call complete_subsession(summary) as soon "
        "as your goal is reached — for periodic work, that means as soon "
        "as the monitored condition reaches a verified terminal state; do "
        "NOT keep re-reporting a finished state. Reply exactly NO_CHANGE "
        "on a periodic run where nothing changed.\n"
        "– In a user_chat subsession, ask a pending question ONCE and wait "
        "for the user's reply; close with a summary once the discussion "
        "reaches a conclusion. The user can also close it at any time.\n"
        "– Subsessions can spawn their own subsessions (nesting is depth-"
        "limited) — split genuinely independent subtasks, do not chain "
        "for its own sake. Check list_subsessions before spawning to "
        "avoid duplicating running work.\n"
        "\n"
        "Board/mill rules:\n"
        "– To READ board/ticket state, always use list_board_tickets or "
        "read_board_ticket — these call the SAME HTTP endpoint the user's "
        "browser UI consumes, so you see exactly what the user sees.  "
        "Never narrate or fabricate ticket states; always verify with "
        "the board reader tools first. This applies inside subsessions "
        "too: a periodic subsession monitoring board/ticket status must "
        "re-read the board every run before reporting.\n"
        "– For complex WRITE operations (migrate tickets between repos, "
        "transition ticket state, triage that requires board-manager "
        "intelligence), use consult_mill — the broker-based board manager "
        "handles these. For simple ticket creation, use create_board_ticket "
        "— it is faster, uses fewer tokens, and includes built-in duplicate "
        "detection.\n"
        "– Prefer doing board work inline. If you do hand board work to a "
        "subsession, its instructions MUST require verifying the result "
        "with list_board_tickets before reporting success.\n"
        "– Before filing ANY new ticket, list_board_tickets for the target "
        "repo and check whether an existing OPEN ticket already covers the "
        "same intent; comment on / reuse it instead of filing a duplicate. "
        "create_board_ticket does this for you automatically and will warn "
        "if a similar ticket exists — act on that warning.\n"
        "– After creating a ticket, verify it landed on "
        "the correct board with list_board_tickets. The board manager "
        "(consult_mill) may route tickets to robotsix-mill by default; "
        "if misplaced, request a migration to the correct board "
        "(e.g. robotsix-chat) — also inline via consult_mill.\n"
        "– Never offer to manually promote a ticket from draft to ready. "
        "The draft→ready transition is automatic (auto-pickup); the system "
        "picks up tickets on its own once they leave draft.\n"
        "\n"
        "Calendar/task tools:\n"
        "– query_calendar, manage_calendar, query_tasks, and manage_tasks "
        "are available for calendar and task management through the "
        "configured calendar agent. They may be disabled in deployments "
        "that lack a calendar integration. Never propose building a new "
        "calendar integration — if these tools are unavailable, briefly "
        "note it rather than suggesting alternatives.\n"
        "\n"
        "Autonomy:\n"
        "– Proactively perform actions that are clearly safe and reversible "
        "without waiting for explicit human validation — do not ask for "
        "permission when the action is low-risk and can be easily undone. "
        "Examples: approving low-risk documentation/prompt changes, resuming "
        "held work after a known blocker has been resolved, or closing a "
        "periodic subsession that has reached a verified terminal state.\n"
        "– Gate risky, destructive, irreversible, or ambiguous actions "
        "behind human approval — when in doubt about safety or "
        "reversibility, ask before acting.\n"
        "\n\n"
        "Efficiency:\n"
        "– If a required tool is missing, state it in one sentence and stop — "
        "do not explore alternatives, explain why, or narrate checking for it.\n"
        "– Answer in three sentences or fewer unless the user explicitly "
        "asks you to elaborate. Do NOT volunteer multi-row markdown tables, "
        "timeline/audit dumps, or recap lists — emit those formats ONLY when "
        "the user explicitly requests them (e.g. 'show me a table', 'give me "
        "the full audit'). Never repeat content already shown earlier in the "
        "same conversation.\n"
        "– All tools are already loaded and available for the entire "
        "session; there is no separate tool-loading step. Never narrate "
        "loading, preparing, or fetching tools (e.g. 'I'll load the "
        "tools…', 'Let me load the task management tool first') and never "
        "announce or run a 'capability check'. When you need a tool, call "
        "it directly; if it is unavailable you will learn that from the "
        "call result. Do not restate tool descriptions across turns."
        "\n\nYou are a conversational assistant with no ability to run shell "
        "commands, read or edit files, browse the web, or otherwise access the host "
        "system or its network. You can only converse and use the tools explicitly "
        "provided to you in this session. If a request needs access you don't have, "
        "briefly say so and suggest an alternative; never narrate or pretend to "
        "perform such actions."
    )
    server_host: str = "127.0.0.1"
    server_port: int = 8000
    idle_timeout_minutes: int = 30
    log_level: str = "INFO"
    cors_allow_origins: list[str] = Field(default_factory=list)
    correlation_id_header: str = "X-Request-ID"
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    mill: MillSettings = Field(default_factory=MillSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    calendar: CalendarSettings = Field(default_factory=CalendarSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    diagnostics: DiagnosticsSettings = Field(default_factory=DiagnosticsSettings)
    refdocs: RefDocsSettings = Field(default_factory=RefDocsSettings)
    board_reader: BoardSettings = Field(default_factory=BoardSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    self_review: SelfReviewSettings = Field(default_factory=SelfReviewSettings)
    component_agent: ComponentAgentSettings = Field(
        default_factory=ComponentAgentSettings
    )
    version_check: VersionCheckSettings = Field(default_factory=VersionCheckSettings)
    component_client: ComponentClientSettings = Field(
        default_factory=ComponentClientSettings
    )
    subsessions: SubsessionsSettings = Field(default_factory=SubsessionsSettings)
    direct_repo: DirectRepoSettings = Field(default_factory=DirectRepoSettings)
    skills: SkillsSettings = Field(default_factory=SkillsSettings)
    max_images_per_message: int = 8
    max_image_bytes: int = 5_242_880
    allowed_image_media_types: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"]
    )

    @staticmethod
    def _require_broker_creds(subsystem: Any, name: str) -> None:
        """Validate broker_token and broker_host for *subsystem* when enabled.

        *subsystem* must have ``broker_token`` and ``broker_host`` attrs.
        """
        if not subsystem.broker_token.get_secret_value():
            raise ValueError(
                f"{name}.broker_token must be set when {name} is enabled — "
                f"provide it via the `{name}.broker_token` config field"
            )
        if not subsystem.broker_host:
            raise ValueError(
                f"{name}.broker_host must be set when {name} is enabled — "
                f"provide it via the config file"
            )

    @staticmethod
    def _require_min(value: float | int, min_val: float | int, name: str) -> None:
        """Raise :class:`ValueError` if *value* < *min_val*."""
        if value < min_val:
            raise ValueError(f"{name} must be >= {min_val}, got {value!r}")

    def model_post_init(self, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations."""
        if self.llmio_model_level not in _VALID_MODEL_LEVELS:
            raise ValueError(
                f"llmio.model_level must be one of {sorted(_VALID_MODEL_LEVELS)}, "
                f"got {self.llmio_model_level!r}"
            )
        # The keyless Claude SDK provider (level 3) needs no API key;
        # key-bearing providers (e.g. openrouter, levels 1-2) require one.
        if (
            level_needs_api_key(self.llmio_model_level)
            and not self.llmio_api_key.get_secret_value()
        ):
            raise ValueError(
                f"llmio.api_key must be set for model_level "
                f"{self.llmio_model_level} (its provider needs a key) — provide "
                "it via the `llmio.api_key` field of your config file "
                "(or use model_level 3, which is keyless)"
            )
        if self.memory.enabled:
            if not self.memory.llm.api_key.get_secret_value():
                raise ValueError(
                    "memory.llm.api_key must be set when memory is enabled — "
                    "provide it via the `memory.llm.api_key` "
                    "field of your config file"
                )
            if not self.memory.embedding.endpoint:
                raise ValueError(
                    "memory.embedding.endpoint must be set when memory is enabled "
                    "(e.g. http://host:11434/v1) — provide it via "
                    "the config file"
                )
        self._require_min(self.idle_timeout_minutes, 0, "idle_timeout_minutes")
        self._require_min(
            self.subsessions.max_concurrent, 1, "subsessions.max_concurrent"
        )
        self._require_min(self.subsessions.max_depth, 1, "subsessions.max_depth")
        if self.subsessions.default_model_level not in _VALID_MODEL_LEVELS:
            raise ValueError(
                f"subsessions.default_model_level must be one of "
                f"{sorted(_VALID_MODEL_LEVELS)}, "
                f"got {self.subsessions.default_model_level!r}"
            )
        self._require_min(
            self.subsessions.min_interval_seconds,
            1.0,
            "subsessions.min_interval_seconds",
        )
        self._require_min(
            self.subsessions.auto_stop_no_change_runs,
            1,
            "subsessions.auto_stop_no_change_runs",
        )
        if self.mill.enabled:
            self._require_broker_creds(self.mill, "mill")
        # mail has no required fields beyond `enabled` — api_base_url and
        # api_token both have safe defaults for localhost operation.
        if self.calendar.enabled:
            self._require_broker_creds(self.calendar, "calendar")
        if self.component_agent.enabled:
            self._require_broker_creds(self.component_agent, "component_agent")
        # component_client has no required fields beyond `enabled` —
        # an empty components list just means no agents are reachable,
        # and the list_component_agents tool returns a helpful message.
        if self.refdocs.enabled and not self.refdocs.repos:
            raise ValueError(
                "refdocs.repos must be non-empty when refdocs is enabled — "
                "provide it via the `refdocs.repos` config field"
            )
        if self.version_check.enabled and not self.version_check.repo:
            raise ValueError(
                "version_check.repo is required when version_check.enabled is true — "
                "provide it via the `version_check.repo` config field"
            )

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> Settings:
        """Load from the JSON file located by ``ROBOTSIX_CONFIG_FILE``."""
        return load_config(cls)
