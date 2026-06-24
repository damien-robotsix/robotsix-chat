"""Config contract + validation for the robotsix-chat component agent.

Defines the :func:`get_config_snapshot` (``config-get`` shape),
:func:`validate_config_update` / :func:`apply_config_update` (``config-set``
shape), a per-key allowlist, secret redaction, and audit logging — everything
child #4 ("Embed the agent") needs to serve ``config-get`` and ``config-set``
request kinds over the broker.

**Dotted-path convention.** Keys follow the YAML layout in
``robotsix_chat/config.py`` (``_YAML_PATH_TO_FIELD``): ``server.log_level``,
``mill.enabled``, ``conversation.max_history_turns``, etc.  This is the same
convention used in ``config/chat.local.yaml`` and environment-variable
overrides.

**Settable vs. read-only.** The module-level :data:`SETTABLE_KEYS` allowlist
includes genuinely live-mutable scalars and feature ``enabled`` flags.
Startup-only fields like ``server.host``, ``server.port``,
``llmio.model_level``, and ``agent.instruction`` are intentionally excluded
because mutating them at runtime would not alter the running process.

**Secret redaction.** Any field whose dotted-path key matches a secret pattern
(``*api_key``, ``*token``, ``password``) is replaced with the sentinel
``"***"`` in snapshots and audit records.  No secret is ever returned in clear
text.

**Error framing.** :class:`ConfigContractError` carries ``code``, ``message``,
and ``details`` — the same fields as ``robotsix_agent_comm.protocol.Error``'s
body — so child #4's responder can map it to a protocol error trivially.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ValidationError

from robotsix_chat.config import _YAML_PATH_TO_FIELD, Settings

logger = logging.getLogger(__name__)

__all__ = [
    "SETTABLE_KEYS",
    "ConfigContractError",
    "get_config_snapshot",
    "describe_config",
    "validate_config_update",
    "apply_config_update",
]

# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Dotted-key components that indicate a secret-bearing field.
_SECRET_PATTERNS = ("api_key", "token", "password")
_REDACTED_SENTINEL = "***"


def _is_secret_dotted_key(dotted_key: str) -> bool:
    """Return ``True`` if any component of *dotted_key* is secret-bearing."""
    for part in dotted_key.split("."):
        part_lower = part.lower()
        for pat in _SECRET_PATTERNS:
            if pat in part_lower:
                return True
    return False


# ---------------------------------------------------------------------------
# Reverse dotted-path mapping (Settings field name → YAML dotted path)
# ---------------------------------------------------------------------------

_FIELD_TO_DOTTED: dict[str, str] = {v: k for k, v in _YAML_PATH_TO_FIELD.items()}

# Fields added after the original YAML mapping was defined — give them
# sensible dotted paths consistent with the existing convention.
_FIELD_TO_DOTTED.setdefault("max_images_per_message", "server.max_images_per_message")
_FIELD_TO_DOTTED.setdefault("max_image_bytes", "server.max_image_bytes")
_FIELD_TO_DOTTED.setdefault(
    "allowed_image_media_types", "server.allowed_image_media_types"
)


# ---------------------------------------------------------------------------
# Config contract error
# ---------------------------------------------------------------------------


class ConfigContractError(Exception):
    """Structured error mirroring ``protocol.Error``'s body fields.

    Attributes:
        code: Machine-readable error code (e.g. ``"UNKNOWN_KEY"``).
        message: Human-readable description.
        details: Optional dict with extra context (offending keys, reason).

    """

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialise with a machine code, human message, and optional details."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


# ---------------------------------------------------------------------------
# Settable-key allowlist
# ---------------------------------------------------------------------------

# Each entry maps a dotted-path key to metadata used by the validate and
# apply functions.  ``path`` is the sequence of ``getattr`` steps from a
# ``Settings`` instance (or from its ``model_dump()`` dict) to the field.
#
# Docstring note: startup-only fields like ``server.host``, ``server.port``,
# ``llmio.model_level``, and ``agent.instruction`` are intentionally excluded
# because mutating them at runtime would not affect the running process.

SETTABLE_KEYS: dict[str, dict[str, Any]] = {
    # -- Server-level scalars -------------------------------------------
    "server.log_level": {
        "type": "str",
        "python_type": str,
        "path": ["log_level"],
    },
    "server.idle_timeout_minutes": {
        "type": "int (>= 0)",
        "python_type": int,
        "path": ["idle_timeout_minutes"],
    },
    "server.max_background_tasks": {
        "type": "int (>= 1)",
        "python_type": int,
        "path": ["max_background_tasks"],
    },
    "server.max_check_loops": {
        "type": "int (>= 1)",
        "python_type": int,
        "path": ["max_check_loops"],
    },
    "server.min_check_loop_interval_seconds": {
        "type": "float (>= 1.0)",
        "python_type": (int, float),
        "path": ["min_check_loop_interval_seconds"],
    },
    "server.cors_allow_origins": {
        "type": "list[str]",
        "python_type": list,
        "path": ["cors_allow_origins"],
    },
    "server.max_images_per_message": {
        "type": "int",
        "python_type": int,
        "path": ["max_images_per_message"],
    },
    "server.max_image_bytes": {
        "type": "int",
        "python_type": int,
        "path": ["max_image_bytes"],
    },
    "server.allowed_image_media_types": {
        "type": "list[str]",
        "python_type": list,
        "path": ["allowed_image_media_types"],
    },
    # -- Feature enabled flags ------------------------------------------
    "mill.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["mill", "enabled"],
    },
    "mail.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["mail", "enabled"],
    },
    "calendar.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["calendar", "enabled"],
    },
    "memory.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["memory", "enabled"],
    },
    "refdocs.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["refdocs", "enabled"],
    },
    "knowledge.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["knowledge", "enabled"],
    },
    "self_review.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["self_review", "enabled"],
    },
    "component_agent.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["component_agent", "enabled"],
    },
    "board_reader.enabled": {
        "type": "bool",
        "python_type": bool,
        "path": ["board_reader", "enabled"],
    },
    # -- Conversation settings ------------------------------------------
    "conversation.max_history_turns": {
        "type": "int",
        "python_type": int,
        "path": ["conversation", "max_history_turns"],
    },
    "conversation.max_conversations": {
        "type": "int",
        "python_type": int,
        "path": ["conversation", "max_conversations"],
    },
    "conversation.persist_path": {
        "type": "str",
        "python_type": str,
        "path": ["conversation", "persist_path"],
    },
}


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------


def _walk_model_fields(
    model: BaseModel,
    prefix: str,
) -> dict[str, Any]:
    """Recursively walk *model*'s fields, building dotted-path → value pairs.

    *prefix* is the dotted-path prefix for *model*'s fields (e.g. ``"mill"``).
    """
    result: dict[str, Any] = {}
    for field_name in type(model).model_fields:
        value = getattr(model, field_name)
        dotted = f"{prefix}.{field_name}" if prefix else field_name
        if isinstance(value, BaseModel):
            result.update(_walk_model_fields(value, dotted))
        elif _is_secret_dotted_key(dotted):
            result[dotted] = _REDACTED_SENTINEL
        else:
            result[dotted] = value
    return result


# ---------------------------------------------------------------------------
# Public API — snapshot & describe
# ---------------------------------------------------------------------------


def get_config_snapshot(settings: Settings) -> dict[str, Any]:
    """Return the genuine current configuration as a flat dotted-path mapping.

    Every leaf field of *settings* (including nested sub-models) is included.
    Secret-bearing fields are redacted to ``"***"``.  The result is
    JSON-serializable and suitable as the ``config-get`` response body.
    """
    result: dict[str, Any] = {}
    for field_name in type(settings).model_fields:
        value = getattr(settings, field_name)
        dotted = _FIELD_TO_DOTTED.get(field_name, field_name)
        if isinstance(value, BaseModel):
            result.update(_walk_model_fields(value, dotted))
        elif _is_secret_dotted_key(dotted):
            result[dotted] = _REDACTED_SENTINEL
        else:
            result[dotted] = value
    return result


def describe_config() -> dict[str, Any]:
    """Return machine-usable metadata about the config contract.

    Returns a dict with a ``settable`` key mapping each settable dotted-path
    key to its declared type string.  Callers / discovery layers can use this
    to learn which keys may be updated and what types they expect.
    """
    return {
        "settable": {key: {"type": meta["type"]} for key, meta in SETTABLE_KEYS.items()}
    }


# ---------------------------------------------------------------------------
# Internal: candidate construction
# ---------------------------------------------------------------------------


def _merge_updates(
    base: dict[str, Any],
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Deep-copy *base* and apply dotted-path *updates* into the copy.

    *base* is expected to be a ``Settings.model_dump()`` dict (nested dicts
    for sub-models).  *updates* keys are dotted-path strings; their ``path``
    metadata in :data:`SETTABLE_KEYS` tells us how to navigate *base*.
    """
    result = deepcopy(base)
    for dotted_key, value in updates.items():
        path = SETTABLE_KEYS[dotted_key]["path"]
        target: dict[str, Any] = result
        for step in path[:-1]:
            target = target[step]
        target[path[-1]] = value
    return result


def _build_candidate(
    settings: Settings,
    updates: Mapping[str, Any],
) -> Settings:
    """Build a candidate ``Settings`` from *settings* + *updates*.

    Constructing the candidate exercises every pydantic validator and
    :meth:`Settings.model_post_init`, so cross-field invariants are enforced.
    """
    base = settings.model_dump()
    merged = _merge_updates(base, updates)
    return Settings(**merged)


# ---------------------------------------------------------------------------
# Public API — validate & apply
# ---------------------------------------------------------------------------


def validate_config_update(
    settings: Settings,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate *updates* without mutating *settings*.

    Returns the validated update dict on success (same shape as *updates*).
    Raises :class:`ConfigContractError` for:

    * ``UNKNOWN_KEY`` — one or more keys are not in :data:`SETTABLE_KEYS`.
    * ``TYPE_MISMATCH`` — a value has an incompatible Python type.
    * ``CROSS_FIELD_INVALID`` — the resulting configuration would violate a
      cross-field constraint (e.g. enabling ``mill`` without a broker token).

    *updates* is a mapping of dotted-path key → new value.
    """
    if not updates:
        return {}

    # 1. Reject unknown keys
    unknown = sorted(k for k in updates if k not in SETTABLE_KEYS)
    if unknown:
        raise ConfigContractError(
            code="UNKNOWN_KEY",
            message=f"Unknown config key(s): {', '.join(unknown)}",
            details={"unknown_keys": unknown},
        )

    # 2. Per-key type check (bool is a subclass of int — guard explicitly)
    type_errors: list[dict[str, Any]] = []
    for key, value in updates.items():
        meta = SETTABLE_KEYS[key]
        py_type = meta["python_type"]
        if py_type is int and isinstance(value, bool):
            type_errors.append(
                {
                    "key": key,
                    "expected": "int",
                    "got": "bool",
                    "hint": "Use true/false only for boolean fields",
                }
            )
        elif not isinstance(value, py_type):
            type_errors.append(
                {
                    "key": key,
                    "expected": meta["type"],
                    "got": type(value).__name__,
                }
            )

    if type_errors:
        raise ConfigContractError(
            code="TYPE_MISMATCH",
            message=f"Type mismatch for {len(type_errors)} key(s)",
            details={"type_errors": type_errors},
        )

    # 3. Cross-field validation via candidate Settings construction
    try:
        _build_candidate(settings, updates)
    except (ValueError, ValidationError) as exc:
        raise ConfigContractError(
            code="CROSS_FIELD_INVALID",
            message=str(exc),
            details={"error": str(exc)},
        ) from exc

    return dict(updates)


def apply_config_update(
    settings: Settings,
    updates: Mapping[str, Any],
) -> dict[str, tuple[Any, Any]]:
    """Validate *updates* and apply them to the live *settings* instance.

    Validation runs first; if it fails the live instance is left **completely
    unchanged**.  On success each changed field is set via ``setattr`` on the
    inner model objects, so child #4's held reference immediately reflects the
    new values.

    Returns an audit record: ``{dotted_key: (old_value, new_value)}`` with
    secret-bearing values redacted to ``"***"``.

    An INFO-level log entry is emitted containing the same redacted before/after
    for every changed key.
    """
    if not updates:
        return {}

    # 1. Validate first — never mutate on invalid input
    validate_config_update(settings, updates)

    # 2. Snapshot old values (already redacted by get_config_snapshot)
    old_snapshot = get_config_snapshot(settings)

    # 3. Apply each update to the live instance
    for dotted_key, value in updates.items():
        path = SETTABLE_KEYS[dotted_key]["path"]
        target: Any = settings
        for step in path[:-1]:
            target = getattr(target, step)
        setattr(target, path[-1], value)

    # 4. Snapshot new values
    new_snapshot = get_config_snapshot(settings)

    # 5. Build audit record
    audit: dict[str, tuple[Any, Any]] = {}
    for dotted_key in updates:
        old_val = old_snapshot.get(dotted_key, _REDACTED_SENTINEL)
        new_val = new_snapshot.get(dotted_key, _REDACTED_SENTINEL)
        audit[dotted_key] = (old_val, new_val)

    # 6. Emit auditable log
    logger.info(
        "Config updated: %s",
        {k: f"{old} → {new}" for k, (old, new) in audit.items()},
    )

    return audit
