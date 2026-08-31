"""Layered configuration for robotsix-chat.

Settings resolve through a single cascade: pydantic field defaults are
overlaid with values from a single JSON config file located by the
``ROBOTSIX_CONFIG_FILE`` environment variable (defaults to
``config/config.json``).  There is no environment-variable overlay —
``environment:`` is never a config channel for first-party code.

The LLM is selected through ``robotsix-llmio``'s consumer-facing
``provider-model`` tier identifier (``robotsix_llmio.config``): you pick a
capability level and llmio resolves the provider + model from its baked
defaults, never a concrete provider class.
"""

from __future__ import annotations

from types import UnionType
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel
from robotsix_llmio import default_tier_config
from robotsix_llmio.config import (
    LEVEL1_DEFAULT,
    LEVEL2_DEFAULT,
    LEVEL3_DEFAULT,
    LEVEL4_DEFAULT,
    LEVEL5_DEFAULT,
)

__all__ = [
    "FRONTIER_MODEL_LEVEL",
    "drop_blank_numeric_sentinels",
    "level_display_name",
    "level_needs_api_key",
]

# Numeric field annotations that must never carry a ``""`` sentinel.
_NUMERIC_TYPES = (int, float)


def _annotation_allows_number(annotation: Any) -> bool:
    """Whether *annotation* accepts an ``int`` or ``float`` value.

    Handles bare ``int``/``float`` as well as optional/union forms such as
    ``int | None`` and ``float | None`` (unwrapped via
    :func:`typing.get_args`). ``bool`` is intentionally excluded — although
    it subclasses ``int``, a checkbox is never persisted as ``""``.
    """
    if annotation in _NUMERIC_TYPES:
        return True
    return any(arg in _NUMERIC_TYPES for arg in get_args(annotation))


def _nested_model(annotation: Any) -> type[BaseModel] | None:
    """Extract a nested ``BaseModel`` subclass from *annotation*, if any.

    Handles a bare model annotation (``Sub``) and optional/union forms
    (``Sub | None``). Container annotations such as ``dict[str, Sub]`` or
    ``list[Sub]`` return ``None`` — their raw values are keyed/indexed
    collections, not a single model's field dict, so recursing into them
    with the element model would be incorrect.
    """
    origin = get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        return None
    if origin in (Union, UnionType):
        for arg in get_args(annotation):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                return arg
    return None


def drop_blank_numeric_sentinels(
    model_cls: type[BaseModel], data: Any, *, recursive: bool = False
) -> Any:
    """Strip legacy empty-string sentinels for numeric fields at load time.

    Older settings-UI form submissions serialized a *cleared* numeric input
    as the empty string ``""`` and persisted it to the config file. Such a
    value fails validation against an ``int``/``float`` field, which makes
    ``GET /config`` fall back to its unvalidated merge and surface the raw
    ``""`` in the settings UI as an ambiguous blank input. Dropping the key
    lets the field fall back to its default — ``None`` for optional numerics
    (serialized as JSON ``null``) or the documented numeric default
    otherwise — so deployed configs written with ``""`` sentinels load
    cleanly and never re-serialize a ``""`` placeholder.

    Intended for use from a ``@model_validator(mode="before")`` on config
    models that carry numeric/duration fields. Mutates and returns *data*
    when it is a dict; passes any other input through untouched.

    When *recursive* is true, the strip descends into nested ``BaseModel``
    submodels (raw dicts) so a single call from the top-level ``Settings``
    validator covers every numeric field in the whole config tree — no
    per-submodel validator required for ``GET /config`` (which validates the
    full ``Settings``). Per-model validators are still useful for validating
    a submodel standalone.
    """
    if not isinstance(data, dict):
        return data
    for name, field in model_cls.model_fields.items():
        annotation = field.annotation
        if _annotation_allows_number(annotation):
            for key in (name, field.alias):
                if key is not None and data.get(key) == "":
                    data.pop(key, None)
            continue
        if not recursive:
            continue
        nested_cls = _nested_model(annotation)
        if nested_cls is None:
            continue
        for key in (name, field.alias):
            if key is None:
                continue
            nested = data.get(key)
            if isinstance(nested, dict):
                drop_blank_numeric_sentinels(nested_cls, nested, recursive=True)
    return data


# robotsix-llmio now owns the level → provider-model mapping. The chat
# just picks a capability *level*; the combined provider-model identifier for
# that level comes from llmio's baked default TierLevelConfig (single source
# of truth) — see ``robotsix_llmio.config.tier``.
_LEVEL_DEFAULTS: dict[int, Any] = {
    1: LEVEL1_DEFAULT,
    2: LEVEL2_DEFAULT,
    3: LEVEL3_DEFAULT,
    4: LEVEL4_DEFAULT,
    5: LEVEL5_DEFAULT,
}

# Provider prefix for the keyless Claude SDK tier (auth via logged-in
# `claude` CLI — no API key needed).
_KEYLESS_PROVIDER = "claudeSDK"


def level_needs_api_key(level: int) -> bool:
    """Whether *level*'s default provider requires an ``api_key``.

    True for key-bearing providers (e.g. ``openrouter``), False for the
    keyless ``claudeSDK`` provider. Unknown levels are treated as needing a
    key (model_level is validated separately before this matters).
    """
    tlc = _LEVEL_DEFAULTS.get(level)
    return tlc is None or tlc.provider != _KEYLESS_PROVIDER


#: The strongest capability level a session can escalate to (currently 5).
#: Named rather than hard-coded at call sites so the frontier is a
#: one-line change here.
FRONTIER_MODEL_LEVEL = 5


def level_display_name(level: int) -> str:
    """Return the human-facing model name for *level*.

    Resolved from llmio's baked default tier config so the display always
    matches what actually served the turn. Unknown levels render as
    ``"level N"`` rather than raising — this is display text, never a
    control-flow input.
    """
    try:
        tlc = default_tier_config().for_level(level)
    except ValueError:
        return f"level {level}"
    return str(tlc.model_name)
