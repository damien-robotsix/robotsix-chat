"""Validation, legacy-migration and ``load()`` surface for :class:`Settings`.

Split out of :mod:`robotsix_chat.config.settings` (module-size split) so the
model body there stays a pure declarative field list.  ``Settings`` inherits
the :class:`_SettingsLoaderMixin` hooks from here and re-exports the public
names (``ConfigValidationError``, ``SYSTEM_PROMPT_VERSION``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, model_validator
from robotsix_config import InvalidConfigError, load_config, resolve_config_path

from robotsix_chat.config.constants import (
    VALID_MODEL_LEVELS,
    drop_blank_numeric_sentinels,
)
from robotsix_chat.config.langfuse_models import overlay_env_projects
from robotsix_chat.config.models import (
    FeedbackSettings,
    RefDocsSettings,
    SubsessionsSettings,
    VersionCheckSettings,
)
from robotsix_chat.config.system_prompt_history import KNOWN_SYSTEM_PROMPT_SHA256S

logger = logging.getLogger(__name__)


class ConfigValidationError(ValueError):
    """Raised when one or more config preconditions fail.

    Carries a ``failures`` list so callers can report per-precondition
    details (which check failed, what value was seen) rather than a
    single opaque string.
    """

    def __init__(self, failures: list[str]) -> None:
        """Store *failures* and set a combined message."""
        self.failures: list[str] = failures
        super().__init__("; ".join(failures))


# Version stamp for the agent_instruction default literal.
# Bump on every change to Settings.agent_instruction and update
# docs/system_prompt_changelog.md with a new entry + SHA256.
SYSTEM_PROMPT_VERSION = 163


# Unambiguous old-scheme model levels and their new-scheme equivalents.  The
# v0.21.0 rework collapsed the old 1..5 capability ladder to 1..3 using
# ``{1->1, 2->1, 3->2, 4->2, 5->3}``.  Only the values 4 and 5 are unambiguously
# old-scheme (they never existed in the new 1..3 range), so only they are
# remapped; new-scheme pins 1..3 are indistinguishable from a deliberate choice
# and any other value (e.g. a garbage 6) is left for the range checks to reject.
# Mirrors the ``_LEGACY_MODEL_LEVEL_MAP`` convention in
# :mod:`robotsix_chat.config.constants`.
_LEGACY_LEVEL_REMAP: dict[int, int] = {4: 2, 5: 3}


def _remap_legacy_model_level(value: Any) -> Any:
    """Remap an unambiguous old-scheme model level to the new 1..3 scheme.

    Only the old-scheme values ``4`` and ``5`` are remapped (``4 -> 2``,
    ``5 -> 3``); every other value — including new-scheme ``1..3`` pins,
    genuinely invalid levels like ``6``, and any non-int value — passes
    through untouched so the downstream range checks still reject real
    garbage.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return value
    return _LEGACY_LEVEL_REMAP.get(value, value)


LEGACY_LLMIO_API_KEY_ERROR = (
    "Config key 'llmio_api_key' is no longer supported: rename it to "
    "'openrouter_api_key' in the config file (the value is the OpenRouter "
    "fallback-slot API key)."
)


def _reject_legacy_llmio_api_key(data: dict[str, Any]) -> None:
    """Fail config load when the pre-standard ``llmio_api_key`` key is present.

    The component standard's no-fallback rule forbids reading pre-standard
    credential field names; the error names the canonical key so the
    operator can migrate the config file.
    """
    if "llmio_api_key" in data:
        raise ValueError(LEGACY_LLMIO_API_KEY_ERROR)


def _reject_legacy_keys_in_file(path: Path) -> None:
    """Fail fast when the raw config file still carries ``llmio_api_key``.

    Runs before :func:`robotsix_config.load_config`, whose self-healing pass
    would otherwise strip the unknown key — and persist the stripped file,
    silently discarding the operator's secret — instead of failing load.
    Unreadable / malformed files are left for ``load_config`` to report.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return
    if isinstance(raw, dict) and "llmio_api_key" in raw:
        raise InvalidConfigError(f"{path}: {LEGACY_LLMIO_API_KEY_ERROR}")


class _SettingsProtocol(Protocol):
    """The :class:`Settings` field surface used by the mixin's instance hooks.

    Declared here so :mod:`robotsix_chat.config.settings_loader` never imports
    ``settings`` back (avoiding an import cycle); ``Settings`` satisfies it
    structurally.
    """

    chat_default_model_level: int
    summary_model_level: int
    idle_timeout_minutes: int
    vision_model: str
    subsessions: SubsessionsSettings
    refdocs: RefDocsSettings
    version_check: VersionCheckSettings
    feedback: FeedbackSettings

    @staticmethod
    def _require_min(value: float | int, min_val: float | int, name: str) -> str | None:
        """Return an error string if *value* < *min_val*, or ``None``."""


class _SettingsLoaderMixin(BaseModel):
    """Validation, legacy-migration and ``load()`` hooks for :class:`Settings`.

    Provides ``model_post_init``, the ``@model_validator(mode="before")``
    legacy-migration / normalisation hooks, and the ``load()`` classmethod
    that :class:`Settings` inherits unchanged (pydantic collects the
    decorators from this base via the MRO).
    """

    @property
    def vision_model_configured(self: _SettingsProtocol) -> bool:
        """Whether a vision model is configured for image captioning.

        An empty or unset ``vision_model`` means 'vision model
        unconfigured' — the curated no-image-support failure path then
        applies instead of attempting to caption.
        """
        return bool(self.vision_model)

    @staticmethod
    def _require_min(value: float | int, min_val: float | int, name: str) -> str | None:
        """Return an error string if *value* < *min_val*, or ``None``."""
        if value < min_val:
            return f"{name} must be >= {min_val}, got {value!r}"
        return None

    def model_post_init(self: _SettingsProtocol, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations.

        All preconditions are checked so that every failure is reported
        at once — callers get a full list of what failed rather than
        stopping at the first error.
        """
        failures: list[str] = []

        if self.chat_default_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"chat_default_model_level must be one of "
                f"{sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.chat_default_model_level!r}"
            )
        # No level requires a key up front any more: every level is served
        # by the keyless Claude SDK default slot, and ``llmio.api_key`` only
        # matters when provider failover routes a call to the keyed
        # OpenRouter slot. A missing key therefore degrades failover
        # instead of failing config load — cli.py logs a warning at startup.
        if self.summary_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"summary_model_level must be one of {sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.summary_model_level!r}"
            )
        # Unlike chat_default_model_level, a missing key here is not fatal at config
        # load — create_agent_from_settings falls back to a keyless level
        # (see cli.py) so a keyed level never breaks a deployment that has
        # not configured an OpenRouter key.
        err = self._require_min(self.idle_timeout_minutes, 0, "idle_timeout_minutes")
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.max_concurrent, 1, "subsessions.max_concurrent"
        )
        if err:
            failures.append(err)
        err = self._require_min(self.subsessions.max_depth, 1, "subsessions.max_depth")
        if err:
            failures.append(err)
        if self.subsessions.default_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"subsessions.default_model_level must be one of "
                f"{sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.subsessions.default_model_level!r}"
            )
        if self.subsessions.delegated_read_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"subsessions.delegated_read_model_level must be one of "
                f"{sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.subsessions.delegated_read_model_level!r}"
            )
        if self.subsessions.monitor_max_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"subsessions.monitor_max_model_level must be one of "
                f"{sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.subsessions.monitor_max_model_level!r}"
            )
        err = self._require_min(
            self.subsessions.min_interval_seconds,
            1.0,
            "subsessions.min_interval_seconds",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.auto_stop_no_change_runs,
            1,
            "subsessions.auto_stop_no_change_runs",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.mill_recovery_initial_backoff_seconds,
            1.0,
            "subsessions.mill_recovery_initial_backoff_seconds",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.mill_recovery_max_backoff_seconds,
            1.0,
            "subsessions.mill_recovery_max_backoff_seconds",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.mill_recovery_max_retries,
            0,
            "subsessions.mill_recovery_max_retries",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.max_idle_runs,
            0,
            "subsessions.max_idle_runs",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.max_no_change_pauses,
            0,
            "subsessions.max_no_change_pauses",
        )
        if err:
            failures.append(err)
        # component_client has no required fields beyond `enabled` —
        # an empty components list just means no agents are reachable,
        # and the list_component_agents tool returns a helpful message.
        if self.refdocs.enabled and not self.refdocs.repos:
            failures.append(
                "refdocs.repos must be non-empty when refdocs is enabled — "
                "provide it via the `refdocs.repos` config field"
            )
        if self.version_check.enabled and not self.version_check.repo:
            failures.append(
                "version_check.repo is required when version_check.enabled is true — "
                "provide it via the `version_check.repo` config field"
            )
        if self.feedback.enabled and not self.feedback.board_url:
            failures.append(
                "feedback.board_url must be non-empty when feedback.enabled is "
                "true — provide it via the `feedback.board_url` config field"
            )

        if failures:
            raise ConfigValidationError(failures)

    # ------------------------------------------------------------------
    # Legacy config key migration
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_keys(cls, data: Any) -> Any:
        """Strip legacy config keys that no longer exist in the schema.

        Strips the removed ``autonomy`` block, ``low_risk_actions``,
        and ``subsessions.pre_authorized_ticket_patterns`` — the standing
        autonomy policy is now baked into the prompt layer.
        """
        if not isinstance(data, dict):
            return data

        # Strip the entire retired autonomous block (replaced by 'periodic';
        # stored templates are known to re-inject removed keys, which would
        # otherwise brick every save via extra="forbid").
        if "autonomous" in data:
            logger.info(
                "Dropping retired config block 'autonomous' (replaced by "
                "'periodic' — see the periodic-sessions rework)"
            )
            data = dict(data)
            del data["autonomous"]

        # Strip the entire removed notification block (the notify_user tool
        # and its store-and-forward subsystem were decommissioned — the
        # operator is now notified only via new assistant messages and
        # user_chat subsessions). Deployed configs still carry it, which
        # extra="forbid" would otherwise reject and crash-loop the boot.
        if "notification" in data:
            logger.info(
                "Dropping removed config block 'notification' (the notify_user "
                "tool was decommissioned)"
            )
            data = dict(data)
            del data["notification"]

        # Strip the entire removed autonomy block.
        if "autonomy" in data:
            logger.info(
                "Dropping removed config key 'autonomy' (standing policy "
                "is now baked into the prompt layer)"
            )
            data = dict(data)
            del data["autonomy"]

        # Strip the removed low_risk_actions key.
        if "low_risk_actions" in data:
            logger.info(
                "Dropping removed config key 'low_risk_actions' (standing "
                "policy is now baked into the prompt layer)"
            )
            data = dict(data)
            del data["low_risk_actions"]

        # Strip the removed llmio_task_budget_tokens — the task_budget /
        # max_tokens self-pacing countdown on keyless tiers was decommissioned
        # (2026-09-05). Deployed configs still carry it, which extra="forbid"
        # would otherwise reject and crash-loop the boot.
        if "llmio_task_budget_tokens" in data:
            logger.info(
                "Dropping removed config key 'llmio_task_budget_tokens' "
                "(the task_budget self-pacing mechanism was decommissioned)"
            )
            data = dict(data)
            del data["llmio_task_budget_tokens"]

        # Strip the removed compaction_min_turns / compaction_keep_recent_turns
        # keys — idle-timeout compaction was removed and the periodic summary
        # scheduler (evergoing) is the single context-reduction mechanism.
        # Deployed configs still carry these, which extra="forbid" would
        # otherwise reject and crash-loop the boot.
        for _compaction_key in ("compaction_min_turns", "compaction_keep_recent_turns"):
            if _compaction_key in data:
                logger.info(
                    "Dropping removed config key '%s' (idle-timeout compaction "
                    "was removed; the periodic summary scheduler is the single "
                    "context-reduction mechanism)",
                    _compaction_key,
                )
                data = dict(data)
                del data[_compaction_key]

        # Strip the removed llmio_cooldown_seconds — the llmio cooldown knob
        # was removed in v0.21.0. Deployed configs still carry it, which
        # extra="forbid" would otherwise reject and crash-loop the boot.
        if "llmio_cooldown_seconds" in data:
            logger.info(
                "Dropping removed config key 'llmio_cooldown_seconds' "
                "(removed in v0.21.0)"
            )
            data = dict(data)
            del data["llmio_cooldown_seconds"]

        # Strip any leftover ``cognee`` block. The in-process cognee memory
        # backend was replaced by the robotsix-memory component in v0.21.0;
        # its live config lived under ``memory`` (stripped in
        # ``_migrate_legacy_deploy_and_mail``), but older configs can still
        # carry a top-level ``cognee`` block, which extra="forbid" would
        # otherwise reject and crash-loop the boot.
        if "cognee" in data:
            logger.info(
                "Dropping removed config block 'cognee' (cognee replaced by "
                "the robotsix-memory component in v0.21.0)"
            )
            data = dict(data)
            del data["cognee"]

        # Strip the removed chat_model_level override — the main chat agent
        # now always uses the unified ``chat_default_model_level``.
        if "chat_model_level" in data:
            logger.info(
                "Dropping removed config key 'chat_model_level' (unified into "
                "'chat_default_model_level')"
            )
            data = dict(data)
            del data["chat_model_level"]

        # Rename legacy ``llmio_model_level`` → ``chat_default_model_level``.
        # Configs serialized before the rename still carry the old key; map
        # its value over so the setting is preserved (extra="forbid" would
        # otherwise reject the stale key and brick config load).
        if "llmio_model_level" in data:
            logger.info(
                "Renaming legacy config key 'llmio_model_level' → "
                "'chat_default_model_level'"
            )
            data = dict(data)
            legacy_level = data.pop("llmio_model_level")
            data.setdefault("chat_default_model_level", legacy_level)

        # The pre-standard ``llmio_api_key`` credential key is rejected, not
        # silently renamed: the component standard forbids credential
        # fallbacks, and a silent rename keeps unmigrated configs invisible.
        _reject_legacy_llmio_api_key(data)

        # Strip pre_authorized_ticket_patterns from subsessions.
        subsessions = data.get("subsessions")
        if (
            isinstance(subsessions, dict)
            and "pre_authorized_ticket_patterns" in subsessions
        ):
            logger.info(
                "Dropping removed config key "
                "'subsessions.pre_authorized_ticket_patterns' "
                "(standing policy is now baked into the prompt layer)"
            )
            subsessions = dict(subsessions)
            del subsessions["pre_authorized_ticket_patterns"]
            data = dict(data)
            data["subsessions"] = subsessions

        return data

    @model_validator(mode="before")
    @classmethod
    def _remap_legacy_model_levels(cls, data: Any) -> Any:
        """Remap persisted model-level fields from the old 1..5 scheme.

        The v0.21.0 rework collapsed the old 1..5 capability ladder to 1..3
        (``{1->1, 2->1, 3->2, 4->2, 5->3}``). A config serialized before the
        collapse can still carry a level ``4`` or ``5`` in any of its
        integer level fields (the chat default, the summariser, the
        subsession spawn defaults, feedback, per-periodic-preset levels);
        the post-init range checks would reject those. Remap every such
        field through :func:`_remap_legacy_model_level`, which only touches
        out-of-range old-scheme values and leaves new-scheme ``1..3`` pins
        untouched.
        """
        if not isinstance(data, dict):
            return data

        def _remap_in(container: dict[str, Any], key: str) -> None:
            if key not in container:
                return
            new_value = _remap_legacy_model_level(container[key])
            if new_value != container[key]:
                logger.info(
                    "Remapping legacy model level '%s': %r -> %r "
                    "(old 1..5 ladder collapsed to 1..3 in v0.21.0)",
                    key,
                    container[key],
                    new_value,
                )
                container[key] = new_value

        # Top-level capability levels.
        for key in ("chat_default_model_level", "summary_model_level"):
            _remap_in(data, key)

        # Subsession spawn-default levels.
        subsessions = data.get("subsessions")
        if isinstance(subsessions, dict):
            for key in (
                "default_model_level",
                "delegated_read_model_level",
                "monitor_max_model_level",
            ):
                _remap_in(subsessions, key)

        # Feedback analyser level.
        feedback = data.get("feedback")
        if isinstance(feedback, dict):
            _remap_in(feedback, "model_level")

        # Per-preset periodic session levels.
        periodic = data.get("periodic")
        if isinstance(periodic, dict):
            sessions = periodic.get("sessions")
            if isinstance(sessions, list):
                for session in sessions:
                    if isinstance(session, dict):
                        _remap_in(session, "model_level")

        return data

    @model_validator(mode="before")
    @classmethod
    def _reconcile_stale_agent_instruction(cls, data: Any) -> Any:
        """Auto-upgrade a stored ``agent_instruction`` that pins a FORMER default.

        A deployed config volume can freeze ``agent_instruction`` to whatever the
        code default was when the pin was first written. Because a stored key
        shadows the code default, every later ``SYSTEM_PROMPT_VERSION`` bump is
        then inert in production until someone manually removes the key — this is
        the 2026-09-05 incident, where a v-frozen pin (sha256 ``82f1e66a…``, ~6 KB,
        describing the removed cognee backend) shadowed the current ~91 KB v161
        default for six weeks.

        At boot: if the stored value's SHA256 matches any recorded governed
        default (the ``**SHA256:**`` records in ``docs/system_prompt_changelog.md``,
        mirrored at runtime in :data:`KNOWN_SYSTEM_PROMPT_SHA256S`), the pin is a
        stale former default — drop it so the current code default fills in, and
        log the upgrade. A value matching **no** recorded default is a genuine
        operator customization: keep it, but warn about the version drift so the
        operator knows it will not track ``SYSTEM_PROMPT_VERSION``.
        """
        if not isinstance(data, dict):
            return data
        stored = data.get("agent_instruction")
        if not isinstance(stored, str):
            return data

        current_default = cls.model_fields["agent_instruction"].default
        if stored == current_default:
            # Already the current default (or a bare echo of it) — nothing to do.
            return data

        stored_sha = hashlib.sha256(stored.encode()).hexdigest()
        if stored_sha in KNOWN_SYSTEM_PROMPT_SHA256S:
            data = dict(data)
            del data["agent_instruction"]
            logger.info(
                "Auto-upgrading stale pinned 'agent_instruction' (sha256 %s…) to "
                "the current governed default v%d: the stored value is a former "
                "default frozen by a config volume; dropping the pin so the code "
                "default applies",
                stored_sha[:8],
                SYSTEM_PROMPT_VERSION,
            )
            return data

        logger.warning(
            "Keeping customized 'agent_instruction' (sha256 %s…): it matches no "
            "recorded governed default, so it is treated as a genuine operator "
            "customization and will NOT track SYSTEM_PROMPT_VERSION (currently "
            "v%d). Remove the key from the config to return to the managed "
            "default.",
            stored_sha[:8],
            SYSTEM_PROMPT_VERSION,
        )
        return data

    @classmethod
    def migrate_legacy_config(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Pre-strip hook for ``robotsix_config.load_config``.

        ``load_config`` strips keys the model no longer declares *before*
        calling ``model_validate``, so a migration written only as a
        ``@model_validator(mode="before")`` never sees the legacy value — the
        key is already gone. That is not hypothetical: it silently disabled
        :meth:`_migrate_legacy_memory_openrouter_key` above, whose whole
        purpose is to stop a deployed config crash-looping the container after
        an image upgrade.

        robotsix-config calls this hook on the raw file contents before
        stripping, so the migration runs while ``memory.llm.api_key`` still
        exists. The before-validator is kept as well: it covers the paths that
        do not go through ``load_config`` (``Settings.model_validate`` on a raw
        dict, which the config routes and tests use). Both delegate to the same
        function, which pops the legacy key, so running twice is a no-op.
        """
        # Strip removed autonomy/authorization keys.
        if "notification" in data:
            logger.info("migrate_legacy_config: dropping removed key 'notification'")
            del data["notification"]
        if "autonomy" in data:
            logger.info("migrate_legacy_config: dropping removed key 'autonomy'")
            del data["autonomy"]
        if "low_risk_actions" in data:
            logger.info(
                "migrate_legacy_config: dropping removed key 'low_risk_actions'"
            )
            del data["low_risk_actions"]
        if "chat_model_level" in data:
            logger.info(
                "migrate_legacy_config: dropping removed key 'chat_model_level'"
            )
            del data["chat_model_level"]
        if "llmio_model_level" in data:
            logger.info(
                "migrate_legacy_config: renaming legacy key 'llmio_model_level' "
                "→ 'chat_default_model_level'"
            )
            legacy_level = data.pop("llmio_model_level")
            data.setdefault("chat_default_model_level", legacy_level)
        # ``llmio_api_key`` is deliberately NOT handled here: the loader
        # swallows hook errors and would then strip (and persist away) the
        # key. ``Settings.load`` rejects it on the raw file instead.
        subsessions = data.get("subsessions")
        if (
            isinstance(subsessions, dict)
            and "pre_authorized_ticket_patterns" in subsessions
        ):
            logger.info(
                "migrate_legacy_config: dropping removed key "
                "'subsessions.pre_authorized_ticket_patterns'"
            )
            del subsessions["pre_authorized_ticket_patterns"]

        migrated = _migrate_legacy_deploy_and_mail(data)
        data = migrated if isinstance(migrated, dict) else data

        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_deploy_plane(cls, data: Any) -> Any:
        """Consolidate the deploy plane and drop the retired ``mail`` block.

        Covers the paths that do not go through
        ``robotsix_config.load_config`` — ``Settings.model_validate`` on a raw
        dict, which the config routes and the tests use.
        """
        return _migrate_legacy_deploy_and_mail(data)

    # ------------------------------------------------------------------
    # Legacy config normalisation
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_empty_strings(cls, data: Any) -> Any:
        """Coerce legacy ``""`` and JS-toString sentinels to proper containers.

        Older deployed configs used ``""`` for optional array/object
        fields that were never configured, and a browser-side serialisation
        bug in the Configure UI sometimes passes ``String(value)`` instead
        of ``JSON.stringify(value)``, yielding sentinels like
        ``"[object Object]"`` for objects.

        Normalize all of these here so validation passes on untouched or
        corrupted keys rather than failing with a type-mismatch error.
        """
        if not isinstance(data, dict):
            return data

        # Strings that indicate a JS/browser serialisation bug — an object
        # or array was passed through ``String()`` (or implicit
        # ``toString()``) instead of ``JSON.stringify``.
        _bad: frozenset[str] = frozenset({"[object Object]", "undefined", "null"})

        # Top-level list fields — tolerate "" and JS sentinels → []
        for key in ("cors_allow_origins", "allowed_image_media_types"):
            val = data.get(key)
            if val == "" or (isinstance(val, str) and val in _bad):
                data[key] = []

        # Top-level object fields — tolerate "" and JS sentinels → {}
        _object_keys = (
            "llmio_tier_overrides",
            "langfuse",
            "openrouter",
            "langfuse_inspect",
            "memory",
            "central_deploy",
            "conversation",
            "diagnostics",
            "refdocs",
            "render_url",
            "knowledge",
            "self_review",
            "sftp",
            "version_check",
            "component_client",
            "subsessions",
            "direct_repo",
            "github_security",
            "github_actions",
            "repo_study",
            "lifecycle",
            "http_probe",
            "public_fetch",
            "feedback",
            "periodic",
            "continuation",
            "docker_digest",
        )
        for key in _object_keys:
            val = data.get(key)
            if val == "" or (isinstance(val, str) and val in _bad):
                data[key] = {}

        # Nested list fields inside object sub-models
        if isinstance(data.get("refdocs"), dict):
            rv = data["refdocs"].get("repos")
            if rv == "" or (isinstance(rv, str) and rv in _bad):
                data["refdocs"]["repos"] = []
        if isinstance(data.get("component_client"), dict):
            cv = data["component_client"].get("components")
            if cv == "" or (isinstance(cv, str) and cv in _bad):
                data["component_client"]["components"] = []

        # Numeric fields across the whole config tree — tolerate legacy ""
        # sentinels so a cleared numeric input in the settings UI falls back
        # to its default (or null for optional numerics) instead of failing
        # validation and surfacing a raw "" placeholder in GET /config. The
        # recursive walk covers every nested submodel from this single call.
        data = drop_blank_numeric_sentinels(cls, data, recursive=True)

        return data

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> Any:
        """Load from the JSON file located by ``ROBOTSIX_CONFIG_FILE``.

        Other components' Langfuse credentials dispatched by central-deploy
        (``LANGFUSE_<PROJECT>_PUBLIC_KEY`` / ``_SECRET_KEY`` env vars) are
        overlaid onto ``langfuse.projects`` in memory — see
        :func:`~robotsix_chat.config.langfuse_models.overlay_env_projects`.
        """
        _reject_legacy_keys_in_file(resolve_config_path())
        settings: Any = load_config(cls)
        langfuse = overlay_env_projects(settings.langfuse, os.environ)
        if langfuse is settings.langfuse:
            return settings
        return settings.model_copy(update={"langfuse": langfuse})


def _migrate_legacy_deploy_and_mail(data: Any) -> Any:
    """Consolidate the deploy plane and drop the retired ``mail`` block.

    Older deployed configs carry duplicated deploy-plane wiring — a base URL
    at ``lifecycle.base_url`` (distinct from ``central_deploy.url``) and a
    deploy credential copied across ``feedback.deploy_api_key``,
    ``github_security.deploy_api_key`` and ``github_actions.deploy_api_key``.
    They also carry a stale ``mail`` block superseded by the component roster.

    With those fields removed from the schema, ``extra="forbid"`` would reject
    the whole file and crash-loop the container on the first start after an
    image upgrade. This copies any legacy value into the canonical
    ``central_deploy.{url,deploy_api_key}`` (an explicitly-configured canonical
    value always wins) and drops the ``mail`` block. The per-sub-model
    ``mode="before"`` validators then strip the now-unknown legacy keys.
    """
    if not isinstance(data, dict):
        return data

    # Drop the retired ``mail`` block (superseded by the component roster).
    if "mail" in data:
        data = dict(data)
        del data["mail"]
        logger.info(
            "Dropping removed config block 'mail' (superseded by the component roster)"
        )

    # Drop the retired ``memory`` block (the in-process cognee backend was
    # replaced by the robotsix-memory component, 2026-09-04). Deployed
    # configs still pin it; extra="forbid" would crash-loop the boot.
    if "memory" in data:
        data = dict(data)
        del data["memory"]
        logger.info(
            "Dropping removed config block 'memory' (cognee replaced by the "
            "robotsix-memory component)"
        )

    central = data.get("central_deploy")
    central = dict(central) if isinstance(central, dict) else {}
    changed = False

    # Canonical URL — fall back to the legacy ``lifecycle.base_url``.
    if not central.get("url"):
        lifecycle = data.get("lifecycle")
        if isinstance(lifecycle, dict) and lifecycle.get("base_url"):
            central["url"] = lifecycle["base_url"]
            changed = True
            logger.info("Migrating legacy 'lifecycle.base_url' → 'central_deploy.url'")

    # Canonical credential — fall back to the first legacy per-block key.
    if not central.get("deploy_api_key"):
        for block in ("feedback", "github_security", "github_actions"):
            sub = data.get(block)
            if isinstance(sub, dict) and sub.get("deploy_api_key"):
                central["deploy_api_key"] = sub["deploy_api_key"]
                changed = True
                logger.info(
                    "Migrating legacy '%s.deploy_api_key' → "
                    "'central_deploy.deploy_api_key'",
                    block,
                )
                break

    if changed:
        data = dict(data)
        data["central_deploy"] = central

    return data
