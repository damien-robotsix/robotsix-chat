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
    CentralDeploySettings,
    ComponentClientSettings,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    KnowledgeSettings,
    LangfuseSettings,
    LifecycleSettings,
    MailSettings,
    MemorySettings,
    RefDocsSettings,
    RepoStudySettings,
    SelfReviewSettings,
    SubsessionsSettings,
    VersionCheckSettings,
)

logger = logging.getLogger(__name__)

# Version stamp for the agent_instruction default literal.
# Bump on every change to Settings.agent_instruction and update
# docs/system_prompt_changelog.md with a new entry + SHA256.
SYSTEM_PROMPT_VERSION = 16

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
        log_json_format: When ``True`` (default), log lines are emitted as
            structured JSON via structlog.  Set to ``False`` for human-readable
            console output during local development.
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
        "Component access:\n"
        "– You have one generic tool for calling external components: "
        "component_request(component_id, method, path, json_body=None). "
        "Each component declares its own API surface as a skill — read "
        "the skill descriptions below for allowed operations.\n"
        "– Obey each component skill's safety section. When a skill marks "
        "an operation as requiring confirmation, ask the user in "
        "conversation before calling it.\n"
        "– If the roster is unavailable or a component returns an error, "
        "report the error clearly — do not retry in a loop.\n"
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
    server_host: str = "0.0.0.0"  # noqa: S104  # nosec B104
    server_port: int = 8000
    idle_timeout_minutes: int = 30
    log_level: str = "INFO"
    log_json_format: bool = True
    cors_allow_origins: list[str] = Field(default_factory=list)
    correlation_id_header: str = "X-Request-ID"
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    central_deploy: CentralDeploySettings = Field(default_factory=CentralDeploySettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    diagnostics: DiagnosticsSettings = Field(default_factory=DiagnosticsSettings)
    refdocs: RefDocsSettings = Field(default_factory=RefDocsSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    self_review: SelfReviewSettings = Field(default_factory=SelfReviewSettings)
    version_check: VersionCheckSettings = Field(default_factory=VersionCheckSettings)
    component_client: ComponentClientSettings = Field(
        default_factory=ComponentClientSettings
    )
    subsessions: SubsessionsSettings = Field(default_factory=SubsessionsSettings)
    direct_repo: DirectRepoSettings = Field(default_factory=DirectRepoSettings)
    repo_study: RepoStudySettings = Field(default_factory=RepoStudySettings)
    lifecycle: LifecycleSettings = Field(default_factory=LifecycleSettings)
    max_images_per_message: int = 8
    max_image_bytes: int = 5_242_880
    allowed_image_media_types: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"]
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
