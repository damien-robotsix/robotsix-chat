"""Top-level :class:`Settings` model and its factories.

Composes the sub-models from :mod:`robotsix_chat.config.models` and
implements the full defaults → YAML → environment cascade.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from robotsix_yaml_config import (
    YamlConfigError,
    flatten_config,
    read_yaml_file,
)

from robotsix_chat.config.constants import (
    _YAML_PATH_TO_FIELD,
    CONFIG_PATH_ENV,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    _parse_bool,
    level_needs_api_key,
)
from robotsix_chat.config.env_builders import (
    _build_board_reader_raw,
    _build_calendar_raw,
    _build_component_agent_raw,
    _build_component_client_raw,
    _build_conversation_raw,
    _build_diagnostics_raw,
    _build_direct_repo_raw,
    _build_knowledge_raw,
    _build_mail_raw,
    _build_memory_raw,
    _build_mill_raw,
    _build_pending_questions_raw,
    _build_refdocs_raw,
    _build_self_review_raw,
    _build_version_check_raw,
)
from robotsix_chat.config.models import (
    AuthSettings,
    BoardReaderSettings,
    CalendarSettings,
    ComponentAgentSettings,
    ComponentClientSettings,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    KnowledgeSettings,
    MailSettings,
    MemorySettings,
    MillSettings,
    PendingQuestionsSettings,
    RefDocsSettings,
    SelfReviewSettings,
    VersionCheckSettings,
)

logger = logging.getLogger(__name__)

# Version stamp for the agent_instruction default literal.
# Bump on every change to Settings.agent_instruction and update
# docs/system_prompt_changelog.md with a new entry + SHA256.
SYSTEM_PROMPT_VERSION = 14

# Valid model levels (import-time constant so the set is built once).
_VALID_MODEL_LEVELS = frozenset({1, 2, 3})


class Settings(BaseModel):
    """Application settings, resolved from defaults → YAML → environment.

    The LLM is configured the robotsix-llmio way — pick a capability
    ``model_level`` and llmio resolves the provider + model for that level
    (from its baked default :class:`~robotsix_llmio.config.TierLevelConfig`).

    Attributes:
        llmio_model_level: Capability level — ``1`` (cheapest/fastest), ``2``,
            or ``3`` (most capable). The level encodes the provider + model:
            by default levels 1-2 use ``openrouter`` and level 3 uses
            ``claudeSDK``/``opus``.
        llmio_api_key: Provider API key, forwarded to llmio when the chosen
            level's provider needs one (e.g. ``openrouter``); unused
            by keyless providers like ``claudeSDK``.
        subagent_model: Bare Claude model name override for background
            sub-agents spawned via ``delegate_task``.  Only applied when the
            foreground level uses the keyless ``claudeSDK`` provider
            (level 3).  ``"sonnet"`` (default) is cheaper than Opus and
            stays on the subscription; set ``"haiku"`` for cheaper
            delegation, or ``null`` to disable the downgrade (delegation
            tasks then match the foreground model).  Ignored for levels 1–2
            (OpenRouter).  Env override: ``LLMIO_SUBAGENT_MODEL``.

            Check-loop workers use :attr:`check_loop_model` instead; the two
            pools have independent model overrides.
        check_loop_model: Bare Claude model name override for check-loop
            workers (recurring monitoring / status-check ticks).  Only
            applied when the foreground level uses the keyless ``claudeSDK``
            provider (level 3).  ``"haiku"`` (default) is the cheapest
            subscription tier — ideal for binary "is it done yet?" polling.
            Set ``"sonnet"`` for more nuanced monitoring, or ``null`` to
            disable the downgrade (check-loop ticks then match the foreground
            model).  Ignored for levels 1–2 (OpenRouter).
            Env override: ``LLMIO_CHECK_LOOP_MODEL``.
        agent_instruction: System instruction handed to the LLM agent.
            Includes delegate-vs-inline guidance for background tasks.
        server_host: Host address the chat SSE server binds to.
        server_port: Port the chat SSE server listens on.
        idle_timeout_minutes: Minutes of no user activity before the UI
            auto-restarts the conversation; ``0`` disables the feature.
            Env override: ``IDLE_TIMEOUT_MINUTES``.
        max_background_tasks: Maximum number of concurrently-running background
            sub-agent tasks per process.  Env override: ``MAX_BACKGROUND_TASKS``.
        max_check_loops: Maximum number of concurrent check loops per process.
            Env override: ``MAX_CHECK_LOOPS``.
        min_check_loop_interval_seconds: Minimum allowed interval between
            check-loop iterations, in seconds. Env override:
            ``MIN_CHECK_LOOP_INTERVAL_SECONDS``.
        log_level: Python logging level name.
        cors_allow_origins: Origins allowed to call /chat cross-origin
            (empty = none; ``["*"]`` = any). Only needed when the browser
            UI is hosted on a different origin than the server.
        correlation_id_header: HTTP header name used for the correlation /
            request-id (both inbound and outbound). Default ``X-Request-ID``.
        auth: HTTP Basic Auth settings gating the UI and ``/chat``.
        max_images_per_message: Maximum number of images a client may attach to
            a single ``POST /chat`` request.  Default ``8``.
            Env override: ``MAX_IMAGES_PER_MESSAGE``.
        max_image_bytes: Maximum decoded size (bytes) of a single attached
            image.  Default ``5_242_880`` (5 MiB).  Env override:
            ``MAX_IMAGE_BYTES``.
        allowed_image_media_types: Media types accepted for image attachments.
            Default ``["image/png", "image/jpeg", "image/gif", "image/webp"]``.
            Env override: ``ALLOWED_IMAGE_MEDIA_TYPES`` (comma-separated).

    """

    llmio_model_level: int = 3
    llmio_api_key: str = ""
    subagent_model: str | None = "sonnet"
    check_loop_model: str | None = "haiku"
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
        "Answer quick questions inline. "
        "When a request is judged to take a while — multi-step research, "
        "long generation, or anything that would stall your reply — call "
        "the delegate_task tool to offload it to a background sub-agent. "
        "The tool returns a task id immediately; tell the user the work "
        "is running in the background and they'll be notified when it "
        "finishes."
        "\n\n"
        "Board/mill rules:\n"
        "– To READ board/ticket state, always use list_board_tickets or "
        "read_board_ticket — these call the SAME HTTP endpoint the user's "
        "browser UI consumes, so you see exactly what the user sees.  "
        "Never narrate or fabricate ticket states; always verify with "
        "the board reader tools first.\n"
        "– For complex WRITE operations (migrate tickets between repos, "
        "transition ticket state, triage that requires board-manager "
        "intelligence), use consult_mill — the broker-based board manager "
        "handles these. For simple ticket creation, use create_board_ticket "
        "— it is faster, uses fewer tokens, and includes built-in duplicate "
        "detection.\n"
        "– Do all board work inline — never offload board actions through "
        "delegate_task. Delegate-task results are never returned, so a "
        "ticket filed that way may silently fail with no feedback. "
        "This is now enforced — delegate_task will refuse board/ticket "
        "work and direct you to consult_mill.\n"
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
        "– When launching a check loop (start_check_loop) that monitors "
        "mill/board/thread/ticket status, set verify_via_board=True. "
        "Never assert board/thread status without a fresh consult_mill read "
        "— fabricating or narrating status without reading the board is "
        "prohibited.\n"
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
        "held work after a known blocker has been resolved, or stopping a "
        "check loop that has reached a verified terminal state.\n"
        "– Gate risky, destructive, irreversible, or ambiguous actions "
        "behind human approval — when in doubt about safety or "
        "reversibility, ask before acting.\n"
        "– When running inside a check loop (start_check_loop) and you "
        "confirm the monitored condition has reached a verified terminal/"
        "completion state (e.g. a watched ticket is closed/done), call "
        "stop_check_loop to self-stop the loop immediately. Do NOT emit "
        "repeated COMPLETED/NO_CHANGE reports — the check loop's job is "
        "done once the terminal state is verified.\n"
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
    max_background_tasks: int = 5
    max_check_loops: int = 5
    min_check_loop_interval_seconds: float = 60.0
    log_level: str = "INFO"
    cors_allow_origins: list[str] = Field(default_factory=list)
    correlation_id_header: str = "X-Request-ID"
    auth: AuthSettings = Field(default_factory=AuthSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    mill: MillSettings = Field(default_factory=MillSettings)
    mail: MailSettings = Field(default_factory=MailSettings)
    calendar: CalendarSettings = Field(default_factory=CalendarSettings)
    conversation: ConversationSettings = Field(default_factory=ConversationSettings)
    diagnostics: DiagnosticsSettings = Field(default_factory=DiagnosticsSettings)
    refdocs: RefDocsSettings = Field(default_factory=RefDocsSettings)
    board_reader: BoardReaderSettings = Field(default_factory=BoardReaderSettings)
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    self_review: SelfReviewSettings = Field(default_factory=SelfReviewSettings)
    component_agent: ComponentAgentSettings = Field(
        default_factory=ComponentAgentSettings
    )
    version_check: VersionCheckSettings = Field(default_factory=VersionCheckSettings)
    component_client: ComponentClientSettings = Field(
        default_factory=ComponentClientSettings
    )
    pending_questions: PendingQuestionsSettings = Field(
        default_factory=PendingQuestionsSettings
    )
    direct_repo: DirectRepoSettings = Field(default_factory=DirectRepoSettings)
    max_images_per_message: int = 8
    max_image_bytes: int = 5_242_880
    allowed_image_media_types: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"]
    )

    def model_post_init(self, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations."""
        if self.llmio_model_level not in _VALID_MODEL_LEVELS:
            raise ValueError(
                f"llmio.model_level must be one of {sorted(_VALID_MODEL_LEVELS)}, "
                f"got {self.llmio_model_level!r}"
            )
        # The keyless Claude SDK provider (level 3) needs no API key;
        # key-bearing providers (e.g. openrouter, levels 1-2) require one.
        if level_needs_api_key(self.llmio_model_level) and not self.llmio_api_key:
            raise ValueError(
                f"llmio.api_key must be set for model_level "
                f"{self.llmio_model_level} (its provider needs a key) — provide "
                "it via LLMIO_API_KEY, a .env file, or the `llmio.api_key` field "
                "of your config file (or use model_level 3, which is keyless)"
            )
        if self.auth.enabled and not self.auth.password:
            raise ValueError(
                "auth.password must be set when auth is enabled — provide it "
                "via AUTH_PASSWORD or the `auth.password` field of your config file"
            )
        if self.memory.enabled:
            if not self.memory.llm.api_key:
                raise ValueError(
                    "memory.llm.api_key must be set when memory is enabled — "
                    "provide it via MEMORY_LLM_API_KEY or the `memory.llm.api_key` "
                    "field of your config file"
                )
            if not self.memory.embedding.endpoint:
                raise ValueError(
                    "memory.embedding.endpoint must be set when memory is enabled "
                    "(e.g. http://host:11434/v1) — provide it via "
                    "MEMORY_EMBEDDING_ENDPOINT or the config file"
                )
        if self.idle_timeout_minutes < 0:
            raise ValueError(
                f"idle_timeout_minutes must be >= 0, got {self.idle_timeout_minutes!r}"
            )
        if self.max_background_tasks < 1:
            raise ValueError(
                f"max_background_tasks must be >= 1, got {self.max_background_tasks!r}"
            )
        if self.max_check_loops < 1:
            raise ValueError(
                f"max_check_loops must be >= 1, got {self.max_check_loops!r}"
            )
        if self.min_check_loop_interval_seconds < 1.0:
            raise ValueError(
                f"min_check_loop_interval_seconds must be >= 1.0, "
                f"got {self.min_check_loop_interval_seconds!r}"
            )
        if self.mill.enabled:
            if not self.mill.broker_token:
                raise ValueError(
                    "mill.broker_token must be set when mill is enabled — provide "
                    "it via MILL_BROKER_TOKEN or the `mill.broker_token` config field"
                )
            if not self.mill.broker_host:
                raise ValueError(
                    "mill.broker_host must be set when mill is enabled — provide it "
                    "via MILL_BROKER_HOST or the config file"
                )
        if self.mail.enabled:
            if not self.mail.broker_token:
                raise ValueError(
                    "mail.broker_token must be set when mail is enabled — provide "
                    "it via MAIL_BROKER_TOKEN or the `mail.broker_token` config field"
                )
            if not self.mail.broker_host:
                raise ValueError(
                    "mail.broker_host must be set when mail is enabled — provide it "
                    "via MAIL_BROKER_HOST or the config file"
                )
        if self.calendar.enabled:
            if not self.calendar.broker_token:
                raise ValueError(
                    "calendar.broker_token must be set when calendar is enabled — "
                    "provide it via CALENDAR_BROKER_TOKEN or the "
                    "`calendar.broker_token` config field"
                )
            if not self.calendar.broker_host:
                raise ValueError(
                    "calendar.broker_host must be set when calendar is enabled — "
                    "provide it via CALENDAR_BROKER_HOST or the config file"
                )
        if self.component_agent.enabled:
            if not self.component_agent.broker_token:
                raise ValueError(
                    "component_agent.broker_token must be set when "
                    "component_agent is enabled — provide it via "
                    "COMPONENT_AGENT_BROKER_TOKEN or the "
                    "`component_agent.broker_token` config field"
                )
            if not self.component_agent.broker_host:
                raise ValueError(
                    "component_agent.broker_host must be set when "
                    "component_agent is enabled — provide it via "
                    "COMPONENT_AGENT_BROKER_HOST or the config file"
                )
        if self.component_client.enabled:
            if not self.component_client.broker_token:
                raise ValueError(
                    "component_client.broker_token must be set when "
                    "component_client is enabled — provide it via "
                    "COMPONENT_CLIENT_BROKER_TOKEN or the "
                    "`component_client.broker_token` config field"
                )
            if not self.component_client.broker_host:
                raise ValueError(
                    "component_client.broker_host must be set when "
                    "component_client is enabled — provide it via "
                    "COMPONENT_CLIENT_BROKER_HOST or the config file"
                )
        if self.refdocs.enabled and not self.refdocs.repos:
            raise ValueError(
                "refdocs.repos must be non-empty when refdocs is enabled — "
                "provide it via REFDOCS_REPOS or the `refdocs.repos` config field"
            )
        if self.version_check.enabled and not self.version_check.repo:
            raise ValueError(
                "version_check.repo is required when version_check.enabled is true — "
                "provide it via VERSION_CHECK_REPO or the "
                "`version_check.repo` config field"
            )

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, config_path: str | Path | None = None) -> Settings:
        """Load settings through the full cascade (YAML file + environment).

        Resolution order (highest priority last):

        1. pydantic field defaults
        2. the YAML config file — explicit *config_path* arg, else the
           ``CHAT_CONFIG_PATH`` env var, else ``config/chat.local.yaml`` if
           present (otherwise no file)
        3. environment variables (with ``.env`` support)

        Pass ``config_path=""`` to skip the YAML file entirely (used by the
        test suite).
        """
        _load_dotenv()
        nested = cls._read_yaml(config_path)
        flat = flatten_config(nested, _YAML_PATH_TO_FIELD)
        return cls._build(flat)

    @classmethod
    def from_env(cls) -> Settings:
        """Load settings from environment variables only (no YAML file).

        Calls ``load_dotenv()`` first so a ``.env`` file in the working
        directory (or any parent) is picked up automatically. Equivalent
        to ``Settings.load(config_path="")``.
        """
        _load_dotenv()
        return cls._build({})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @classmethod
    def _read_yaml(cls, config_path: str | Path | None) -> dict[str, Any]:
        """Resolve the YAML config path and read it into a nested dict.

        Returns an empty dict when no file applies. Raises
        :class:`FileNotFoundError` when an explicit path (arg or
        ``CHAT_CONFIG_PATH``) points at a missing file, and
        :class:`ConfigError` on malformed YAML.
        """
        explicit = True
        if config_path is not None:
            if config_path == "":
                return {}
            path = Path(config_path)
        else:
            env_path = os.getenv(CONFIG_PATH_ENV)
            if env_path:
                path = Path(env_path)
            elif DEFAULT_CONFIG_PATH.exists():
                path, explicit = DEFAULT_CONFIG_PATH, False
            else:
                return {}

        if explicit and not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        try:
            return read_yaml_file(path)
        except YamlConfigError as exc:
            raise ConfigError(str(exc)) from exc

    @classmethod
    def _build(cls, flat: dict[str, Any]) -> Settings:
        """Overlay environment variables onto *flat* YAML values and build."""
        raw: dict[str, Any] = {
            k: v
            for k, v in flat.items()
            if k
            not in (
                "auth",
                "memory",
                "mill",
                "mail",
                "calendar",
                "conversation",
                "refdocs",
                "knowledge",
                "self_review",
                "version_check",
                "component_agent",
                "component_client",
                "diagnostics",
            )
        }
        auth_raw: dict[str, Any] = dict(flat.get("auth") or {})

        def env_override(field: str, env_name: str) -> None:
            value = os.getenv(env_name)
            if value is not None:
                raw[field] = value

        env_override("llmio_api_key", "LLMIO_API_KEY")
        subagent_val = os.getenv("LLMIO_SUBAGENT_MODEL")
        if subagent_val is not None:
            raw["subagent_model"] = subagent_val or None
        check_loop_val = os.getenv("LLMIO_CHECK_LOOP_MODEL")
        if check_loop_val is not None:
            raw["check_loop_model"] = check_loop_val or None
        env_override("agent_instruction", "AGENT_INSTRUCTION")
        env_override("server_host", "SERVER_HOST")
        env_override("log_level", "LOG_LEVEL")

        level_str = os.getenv("LLMIO_MODEL_LEVEL")
        if level_str is not None:
            try:
                raw["llmio_model_level"] = int(level_str)
            except ValueError:
                raise ValueError(
                    f"LLMIO_MODEL_LEVEL must be an integer, got {level_str!r}"
                ) from None

        port_str = os.getenv("SERVER_PORT")
        if port_str is not None:
            try:
                raw["server_port"] = int(port_str)
            except ValueError:
                raise ValueError(
                    f"SERVER_PORT must be an integer, got {port_str!r}"
                ) from None

        timeout_str = os.getenv("IDLE_TIMEOUT_MINUTES")
        if timeout_str is not None:
            try:
                raw["idle_timeout_minutes"] = int(timeout_str)
            except ValueError:
                raise ValueError(
                    f"IDLE_TIMEOUT_MINUTES must be an integer, got {timeout_str!r}"
                ) from None

        bg_tasks_str = os.getenv("MAX_BACKGROUND_TASKS")
        if bg_tasks_str is not None:
            try:
                raw["max_background_tasks"] = int(bg_tasks_str)
            except ValueError:
                raise ValueError(
                    f"MAX_BACKGROUND_TASKS must be an integer, got {bg_tasks_str!r}"
                ) from None

        max_loops_str = os.getenv("MAX_CHECK_LOOPS")
        if max_loops_str is not None:
            try:
                raw["max_check_loops"] = int(max_loops_str)
            except ValueError:
                raise ValueError(
                    f"MAX_CHECK_LOOPS must be an integer, got {max_loops_str!r}"
                ) from None

        min_interval_str = os.getenv("MIN_CHECK_LOOP_INTERVAL_SECONDS")
        if min_interval_str is not None:
            try:
                raw["min_check_loop_interval_seconds"] = float(min_interval_str)
            except ValueError:
                raise ValueError(
                    f"MIN_CHECK_LOOP_INTERVAL_SECONDS must be a float, "
                    f"got {min_interval_str!r}"
                ) from None

        cors_raw = os.getenv("CORS_ALLOW_ORIGINS")
        if cors_raw is not None:
            raw["cors_allow_origins"] = [
                origin.strip() for origin in cors_raw.split(",") if origin.strip()
            ]

        env_override("correlation_id_header", "CORRELATION_ID_HEADER")

        max_img_str = os.getenv("MAX_IMAGES_PER_MESSAGE")
        if max_img_str is not None:
            try:
                raw["max_images_per_message"] = int(max_img_str)
            except ValueError:
                raise ValueError(
                    f"MAX_IMAGES_PER_MESSAGE must be an integer, got {max_img_str!r}"
                ) from None

        max_bytes_str = os.getenv("MAX_IMAGE_BYTES")
        if max_bytes_str is not None:
            try:
                raw["max_image_bytes"] = int(max_bytes_str)
            except ValueError:
                raise ValueError(
                    f"MAX_IMAGE_BYTES must be an integer, got {max_bytes_str!r}"
                ) from None

        allowed_types = os.getenv("ALLOWED_IMAGE_MEDIA_TYPES")
        if allowed_types is not None:
            raw["allowed_image_media_types"] = [
                t.strip() for t in allowed_types.split(",") if t.strip()
            ]

        auth_enabled = os.getenv("AUTH_ENABLED")
        if auth_enabled is not None:
            auth_raw["enabled"] = _parse_bool(auth_enabled)
        auth_username = os.getenv("AUTH_USERNAME")
        if auth_username is not None:
            auth_raw["username"] = auth_username
        auth_password = os.getenv("AUTH_PASSWORD")
        if auth_password is not None:
            auth_raw["password"] = auth_password

        if auth_raw:
            raw["auth"] = auth_raw

        memory_raw = _build_memory_raw(flat.get("memory"))
        if memory_raw:
            raw["memory"] = memory_raw

        mill_raw = _build_mill_raw(flat.get("mill"))
        if mill_raw:
            raw["mill"] = mill_raw

        mail_raw = _build_mail_raw(flat.get("mail"))
        if mail_raw:
            raw["mail"] = mail_raw

        calendar_raw = _build_calendar_raw(flat.get("calendar"))
        if calendar_raw:
            raw["calendar"] = calendar_raw

        conversation_raw = _build_conversation_raw(flat.get("conversation"))
        if conversation_raw:
            raw["conversation"] = conversation_raw

        refdocs_raw = _build_refdocs_raw(flat.get("refdocs"))
        if refdocs_raw:
            raw["refdocs"] = refdocs_raw

        board_reader_raw = _build_board_reader_raw(flat.get("board_reader"))
        if board_reader_raw:
            raw["board_reader"] = board_reader_raw

        knowledge_raw = _build_knowledge_raw(flat.get("knowledge"))
        if knowledge_raw:
            raw["knowledge"] = knowledge_raw

        diagnostics_raw = _build_diagnostics_raw(flat.get("diagnostics"))
        if diagnostics_raw:
            raw["diagnostics"] = diagnostics_raw

        self_review_raw = _build_self_review_raw(flat.get("self_review"))
        if self_review_raw:
            raw["self_review"] = self_review_raw

        component_agent_raw = _build_component_agent_raw(flat.get("component_agent"))
        if component_agent_raw:
            raw["component_agent"] = component_agent_raw

        version_check_raw = _build_version_check_raw(flat.get("version_check"))
        if version_check_raw:
            raw["version_check"] = version_check_raw

        component_client_raw = _build_component_client_raw(flat.get("component_client"))
        if component_client_raw:
            raw["component_client"] = component_client_raw

        pending_questions_raw = _build_pending_questions_raw(
            flat.get("pending_questions")
        )
        if pending_questions_raw:
            raw["pending_questions"] = pending_questions_raw

        direct_repo_raw = _build_direct_repo_raw(flat.get("direct_repo"))
        if direct_repo_raw:
            raw["direct_repo"] = direct_repo_raw

        return cls(**raw)


def _load_dotenv() -> None:
    """Load a ``.env`` file into the environment if python-dotenv is present."""
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover — python-dotenv is a required dep
        logger.debug("python-dotenv not installed; skipping .env loading")
