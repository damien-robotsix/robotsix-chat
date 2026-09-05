"""Config endpoints — read and update the server's config file.

The response envelopes follow robotsix-standards' config-ownership
contract: the config document always sits under a ``config`` key, never at
the top level.  The shared settings panel reads ``payload.config`` and
falls back to ``{}`` when it is missing, which renders every field at its
schema default — and a Save then writes those defaults over the live
config.  Keep the envelope.

``GET /config`` returns ``{"config": ..., "schema": ..., "version": ...}``
with secrets masked.

``GET /config/deploy`` returns ``{"config": <deploy settings>, "schema": ...}``
— the deploy configuration section with its sub-schema, for use by the
deploy plane to audit deploy-specific settings independently.

``PUT /config`` deep-merges the submitted form over the existing persisted
config, validates, increments the version, and persists.  A field absent
from the submitted payload is preserved, not blanked.  It answers with the
new effective ``{"config": ..., "version": ...}``.

``GET /config/versions`` returns ``{"versions": [...]}`` — the version
history without the full data payloads.

``GET /config/versions/{version}`` returns one version's stored document
under ``config``, with secrets masked exactly like ``GET /config``.

``GET /config/versions/{version}/diff`` returns a value-level diff of that
version against the previous one — dot-notation key paths with ``old`` /
``new`` values; secret leaves report only ``changed: true``, never values.

``POST /config/rollback`` reverts to a previous version and creates a new
version entry (append-only history, never destructive), answering with the
same ``{"config": ..., "version": ...}`` envelope as ``PUT /config``.
"""

from __future__ import annotations

import json
import logging
import stat
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from robotsix_config import config_schema, resolve_config_path
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.config import Settings
from robotsix_chat.config.schema_docs import apply_property_descriptions
from robotsix_chat.config.settings import ConfigValidationError

from ._shared import _parse_json_body
from .errors import _error_body

logger = logging.getLogger(__name__)

# Sentinel for masked secret values — the UI sends this back when the
# user has not changed a secret; we preserve the on-disk value.
_MASKED_SECRET_SENTINEL = "**********"


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------


def _strip_corrupt_object_sentinels(update: dict[str, Any]) -> dict[str, Any]:
    """Drop keys whose submitted value is the JS ``String(obj)`` sentinel.

    The browser settings panel occasionally serialises an object field with
    ``String(value)`` instead of ``JSON.stringify(value)``, yielding the
    literal ``"[object Object]"`` string (observed for
    ``llmio_tier_overrides``).  That value is never a legitimate config leaf;
    dropping the key here lets the deep-merge preserve the stored value
    instead of overwriting it — and later failing validation with a
    ``dict_type`` error.  Recurses into nested dicts so the fix covers
    object fields inside sub-models too.
    """
    result: dict[str, Any] = {}
    for key, value in update.items():
        if isinstance(value, dict):
            result[key] = _strip_corrupt_object_sentinels(value)
        elif value == "[object Object]":
            continue
        else:
            result[key] = value
    return result


def _deep_merge(existing: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *update* into *existing*.

    Dicts are merged recursively; all other types are overwritten by the
    update value.  The *existing* dict is never mutated — a fresh copy is
    returned.
    """
    result = deepcopy(existing)
    for key, value in update.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Secret-key detection
# ---------------------------------------------------------------------------

# Suffixes that mark a config key as a secret field.  Any key whose
# name *ends with* one of these suffixes is treated as a secret.
_SECRET_KEY_SUFFIXES: tuple[str, ...] = (
    "_api_key",
    "_api_token",
    "_secret_key",
    "_private_key",
    "api_key",
    "api_token",
    "secret_key",
    "private_key",
    "public_key",
    "deploy_api_key",
)


def _is_secret_key(key: str) -> bool:
    """Return ``True`` when *key* names a secret field."""
    return key.endswith(_SECRET_KEY_SUFFIXES)


def _is_secret_leaf(key: str, parent: tuple[str, ...]) -> bool:
    """Return ``True`` when the value at *key* under *parent* is a secret.

    In addition to the key-suffix heuristic, every value inside the
    canonical ``openrouter.keys`` map is a secret — the map is keyed by
    alias (Langfuse project name), so its leaf keys don't match any secret
    suffix.
    """
    if _is_secret_key(key):
        return True
    return len(parent) >= 2 and parent[-2:] == ("openrouter", "keys")


# ---------------------------------------------------------------------------
# Secret masking / preservation
# ---------------------------------------------------------------------------


def _preserve_masked_secrets(
    merged: dict[str, Any], existing: dict[str, Any], update: dict[str, Any]
) -> dict[str, Any]:
    """Replace masked secret sentinels and blank values with on-disk values.

    When the UI submits a masked value (``"**********"``) or an empty
    string for a secret field we treat it as "unchanged" and restore the
    existing on-disk value.

    Secret fields are identified by key-name suffix (see
    :data:`_SECRET_KEY_SUFFIXES`) plus the alias-keyed ``openrouter.keys``
    map (see :func:`_is_secret_leaf`).
    """

    def _walk(
        m: dict[str, Any],
        e: dict[str, Any],
        u: dict[str, Any],
        parent: tuple[str, ...] = (),
    ) -> None:
        for key in list(m.keys()):
            uv = u.get(key)
            if _is_secret_leaf(key, parent) and (
                uv == _MASKED_SECRET_SENTINEL or uv == ""
            ):
                if key in e:
                    m[key] = deepcopy(e[key])
                continue
            if (
                isinstance(m[key], dict)
                and isinstance(e.get(key), dict)
                and isinstance(uv, dict)
            ):
                _walk(m[key], e[key], uv, (*parent, key))

    _walk(merged, existing, update)
    return merged


def _mask_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with secret field values replaced by the sentinel.

    Secret fields are identified by key-name suffix (see
    :data:`_SECRET_KEY_SUFFIXES`) plus the alias-keyed ``openrouter.keys``
    map (see :func:`_is_secret_leaf`).
    """

    def _walk(d: dict[str, Any], parent: tuple[str, ...] = ()) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in d.items():
            if isinstance(value, dict):
                result[key] = _walk(value, (*parent, key))
            elif _is_secret_leaf(key, parent) and isinstance(value, str) and value:
                result[key] = _MASKED_SECRET_SENTINEL
            else:
                result[key] = deepcopy(value)
        return result

    return _walk(data)


# ---------------------------------------------------------------------------
# File I/O helpers
# ---------------------------------------------------------------------------


def _read_config_json(path: Path) -> dict[str, Any]:
    """Read and parse the config JSON file at *path*.

    Returns an empty dict when the file does not exist.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    if not raw.strip():
        return {}
    result: Any = json.loads(raw)
    if not isinstance(result, dict):
        return {}
    return result


# lgtm[py/clear-text-storage-sensitive-data]
def _write_config_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write *data* as JSON to *path*.

    Uses a temp-file + rename strategy so a crash mid-write never
    leaves a truncated config.

    The replacement carries the original file's permission bits. The temp
    file is created fresh, so without this the config — which holds API keys
    and other secrets — is left world-readable at the process umask after
    every save, silently widening a 0600 file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        # codeql[py/clear-text-storage-sensitive-data]
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        tmp.chmod(stat.S_IMODE(path.stat().st_mode))
    except OSError:
        # No existing file to copy from (first write), or the mode could not
        # be read — fall back to owner-only, never wider.
        tmp.chmod(0o600)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Version history (append-only JSONL alongside the config file)
# ---------------------------------------------------------------------------


def _versions_path(config_path: Path) -> Path:
    """Return the path to the append-only version-history file."""
    return config_path.with_suffix(config_path.suffix + ".versions")


def _read_versions(versions_file: Path) -> list[dict[str, Any]]:
    """Read all version entries from the JSONL file.

    Returns an empty list when the file does not exist or is empty.
    """
    try:
        raw = versions_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    entries: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry: Any = json.loads(line)
            if isinstance(entry, dict):
                entries.append(entry)
        except json.JSONDecodeError:
            logger.warning("Skipping corrupt version line in %s", versions_file)
    return entries


def _append_version(
    versions_file: Path,
    version: int,
    data: dict[str, Any],
    changed_keys: list[str],
) -> None:
    """Append a new version entry to the JSONL file."""
    entry = {
        "version": version,
        "timestamp": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "changed_keys": changed_keys,
        "data": data,
    }
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    with versions_file.open("a", encoding="utf-8") as f:
        f.write(line)


def _current_version(versions_file: Path) -> int:
    """Return the current version number (0 if no history exists)."""
    entries = _read_versions(versions_file)
    if not entries:
        return 0
    return int(entries[-1]["version"])


def _bootstrap_version_history(config_path: Path, config_data: dict[str, Any]) -> int:
    """Create the first version entry from the current config data.

    Returns the new version number (1).  No-op if a version history
    already exists.
    """
    vp = _versions_path(config_path)
    current = _current_version(vp)
    if current > 0:
        return current
    # Build the list of top-level keys that have non-default values.
    _append_version(vp, 1, deepcopy(config_data), ["initial"])
    return 1


def _compute_changed_keys(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Compute the list of top-level keys that differ between two dicts.

    Only reports top-level key names; nested changes report the parent key.
    """
    all_keys = set(before.keys()) | set(after.keys())
    changed: list[str] = []
    for key in sorted(all_keys):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def _find_version_entry(
    entries: list[dict[str, Any]], version: int
) -> tuple[int, dict[str, Any] | None]:
    """Return ``(index, entry)`` for *version* in *entries*.

    Returns ``(-1, None)`` when no entry carries that version number.
    """
    for index, entry in enumerate(entries):
        if entry.get("version") == version:
            return index, entry
    return -1, None


def _diff_dicts(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Compute a value-level diff between two config documents.

    Returns a list of changed key paths in dot-notation (nested keys, not
    just top-level names).  Non-secret leaf changes carry ``old`` and/or
    ``new`` — the side absent from one document omits its key.  Secret
    leaves carry only ``changed: true`` so values are never emitted; any
    dict or list value attached to a non-secret change is passed through
    :func:`_mask_secrets` first so no secret plaintext can leak.
    """
    _absent = object()

    def _mask_value(value: Any) -> Any:
        """Mask secrets nested inside a diff value (dicts and lists)."""
        if isinstance(value, dict):
            return _mask_secrets(value)
        if isinstance(value, list):
            return [_mask_value(item) for item in value]
        return deepcopy(value)

    def _change(
        path: str, key: str, parent: tuple[str, ...], old: Any, new: Any
    ) -> dict[str, Any]:
        if _is_secret_leaf(key, parent):
            return {"path": path, "changed": True}
        change: dict[str, Any] = {"path": path}
        if old is not _absent:
            change["old"] = _mask_value(old)
        if new is not _absent:
            change["new"] = _mask_value(new)
        return change

    changes: list[dict[str, Any]] = []

    def _walk(
        before_value: Any, after_value: Any, path: str, parent: tuple[str, ...]
    ) -> None:
        if isinstance(before_value, dict) and isinstance(after_value, dict):
            keys = set(before_value.keys()) | set(after_value.keys())
            for key in sorted(keys):
                sub_path = f"{path}.{key}" if path else key
                old = before_value.get(key, _absent)
                new = after_value.get(key, _absent)
                if old == new:
                    continue
                if isinstance(old, dict) and isinstance(new, dict):
                    _walk(old, new, sub_path, (*parent, key))
                elif isinstance(old, dict) and new is _absent:
                    _walk(old, {}, sub_path, (*parent, key))
                elif isinstance(new, dict) and old is _absent:
                    _walk({}, new, sub_path, (*parent, key))
                else:
                    changes.append(_change(sub_path, key, parent, old, new))
        elif before_value != after_value:
            changes.append(_change(path, "", parent, before_value, after_value))

    _walk(before, after, "", ())
    return changes


# ---------------------------------------------------------------------------
# JSON Schema (cached at module level)
# ---------------------------------------------------------------------------

# Module-level cache for the JSON Schema — generated once at import time
# and re-used by every GET /config call.  Use a sentinel to detect when
# the schema has not been generated yet (lazy import in tests may not
# trigger it).
_settings_json_schema: dict[str, Any] | None = None


def _get_schema() -> dict[str, Any]:
    """Return the JSON Schema for :class:`Settings`, cached at module level.

    Goes through ``robotsix_config.config_schema`` rather than calling
    ``Settings.model_json_schema()`` directly, so the shape this route serves
    to the settings UI is the same one the shared CLI writes into
    ``config/config.schema.json``.  They agree today, but a divergence would
    be invisible from here — the committed file is what CI checks, and the UI
    renders whatever this returns.

    The raw schema only carries a property ``description`` for fields with an
    explicit ``Field(description=...)``; :func:`apply_property_descriptions`
    fills the rest from the models' ``Attributes:`` docstrings so the settings
    UI can show a hover description for every setting.
    """
    global _settings_json_schema
    if _settings_json_schema is None:
        _settings_json_schema = apply_property_descriptions(config_schema(Settings))
    return _settings_json_schema


# ---------------------------------------------------------------------------
# RFC 9457 problem+json helper
# ---------------------------------------------------------------------------


def _problem_response(status: int, title: str, detail: str) -> JSONResponse:
    """Return an RFC 9457 problem+json response."""
    return JSONResponse(
        {
            "type": "about:blank",
            "title": title,
            "status": status,
            "detail": detail,
        },
        status_code=status,
        media_type="application/problem+json",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _effective_config(data: dict[str, Any]) -> dict[str, Any]:
    """Return *data* overlaid on the model defaults, validated and masked.

    The on-disk data is overlaid onto the full :class:`Settings` model
    defaults — every schema key (including newly-added fields like
    ``periodic.sessions``) appears even when absent from the persisted
    config file.  Validating through :class:`Settings` also strips legacy
    keys (e.g. ``approval_marker`` / ``proposal_marker``); on failure the
    unvalidated merge is returned so the UI can still render what we have
    while the operator addresses the validation errors.
    """
    defaults = Settings().model_dump(mode="json")
    merged = _deep_merge(defaults, data)

    try:
        response_data = Settings.model_validate(merged).model_dump(mode="json")
    except ValidationError:
        logger.warning(
            "Config validation failed while building the effective config; "
            "returning unvalidated merge (legacy keys may be present)"
        )
        response_data = merged

    return _mask_secrets(response_data)


def _config_serialized_size(value: Any) -> int:
    """Return the approximate serialized size (in chars) of *value*."""
    return len(json.dumps(value, ensure_ascii=False))


def _resolve_dotted_path(data: dict[str, Any], dotted_path: str) -> Any:
    """Resolve a dotted *dotted_path* into *data*, or raise :class:`KeyError`.

    Supports dict keys and integer indexes into arrays (e.g.
    ``periodic.sessions.0``).  Any missing segment raises :class:`KeyError`.
    """
    current: Any = data
    for part in dotted_path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(part)
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except ValueError, IndexError:
                raise KeyError(part) from None
        else:
            raise KeyError(part)
    return current


def _parse_bool_param(params: Any, name: str) -> bool | None:
    """Parse a boolean query param, returning ``None`` when absent.

    Raises :class:`ValueError` when present but not a ``true``/``false``.
    """
    raw = params.get(name)
    if raw is None:
        return None
    lowered = raw.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError(f"query parameter '{name}' must be 'true' or 'false'")


async def config_get_endpoint(request: Request) -> JSONResponse:
    """Return the current on-disk config with secrets masked, plus version and schema.

    ``GET /config`` — no auth (gateway handles it).  The response is the
    standard ``{"config": ..., "schema": ..., "version": ...}`` envelope;
    the config document is never spread across the top level (see the
    module docstring for what breaks when it is).

    Three optional query params enable scoped, lightweight reads that
    avoid pulling the (large) embedded schema:

    - ``keys_only=true`` — return ``{"keys": [{"name", "size"}, ...],
      "version"}`` with no schema, letting an agent enumerate the top-level
      config shape cheaply.
    - ``path=<dotted.path>`` — return just that subtree's masked value
      under ``config`` (e.g. ``path=periodic``), with no schema.
    - ``include_schema=false`` — return the full masked config but omit the
      ``schema`` field.

    With no params, the default ``{config, schema, version}`` envelope is
    returned unchanged.
    """
    config_path = _resolve_config_path_from_app(request)
    data = _read_config_json(config_path)

    # Ensure version history is bootstrapped.
    version = _bootstrap_version_history(config_path, data)

    params = request.query_params

    try:
        keys_only = _parse_bool_param(params, "keys_only")
        include_schema = _parse_bool_param(params, "include_schema")
    except ValueError as exc:
        return _problem_response(400, "Bad Request", str(exc))

    # keys_only=true — enumerate the top-level shape with no schema.
    if keys_only is True:
        if "path" in params:
            return _problem_response(
                400,
                "Bad Request",
                "query parameters 'keys_only' and 'path' are mutually exclusive",
            )
        effective = _effective_config(data)
        keys = [
            {"name": name, "size": _config_serialized_size(value)}
            for name, value in effective.items()
        ]
        return JSONResponse({"keys": keys, "version": version})

    # path=<dotted.path> — return just that subtree, masked, no schema.
    if "path" in params:
        dotted = (params.get("path") or "").strip()
        if not dotted:
            return _problem_response(
                400, "Bad Request", "query parameter 'path' must be non-empty"
            )
        effective = _effective_config(data)
        try:
            value = _resolve_dotted_path(effective, dotted)
        except KeyError:
            return _problem_response(
                404,
                "Not Found",
                f"config key path '{dotted}' does not exist",
            )
        return JSONResponse({"config": value, "version": version})

    # include_schema=false — full masked config without the schema.
    if include_schema is False:
        return JSONResponse({"config": _effective_config(data), "version": version})

    # Default — the standard full envelope.
    return JSONResponse(
        {
            "config": _effective_config(data),
            "schema": _get_schema(),
            "version": version,
        }
    )


async def config_deploy_get_endpoint(request: Request) -> JSONResponse:
    """Return the deploy configuration section with its sub-schema.

    ``GET /config/deploy`` — exposes only the ``central_deploy`` settings
    block so the deploy plane can audit and manage its own configuration
    independently of the full component settings.

    Response envelope: ``{"config": <deploy settings>, "schema": ...}``
    consistent with ``GET /config``.
    """
    config_path = _resolve_config_path_from_app(request)
    data = _read_config_json(config_path)
    full_config = _effective_config(data)

    deploy_config = full_config.get("central_deploy", {})

    # Extract the CentralDeploySettings sub-schema from the full schema.
    # The schema describes the deploy config shape directly (matching the
    # ``config`` value returned by this endpoint), not wrapped under a
    # ``central_deploy`` property.
    full_schema = _get_schema()
    deploy_schema: dict[str, Any] = {}
    if "properties" in full_schema:
        central_deploy_prop = full_schema["properties"].get("central_deploy", {})
        if "$ref" in central_deploy_prop:
            ref_path = central_deploy_prop["$ref"]
            ref_name = ref_path.split("/")[-1]
            if "$defs" in full_schema and ref_name in full_schema["$defs"]:
                defn = full_schema["$defs"][ref_name]
                deploy_schema = {
                    "$schema": full_schema.get(
                        "$schema", "https://json-schema.org/draft/2020-12/schema"
                    ),
                    **defn,
                    "$defs": full_schema["$defs"],
                }

    return JSONResponse(
        {
            "config": deploy_config,
            "schema": deploy_schema,
        }
    )


def _strip_repinned_agent_instruction(merged: dict[str, Any]) -> dict[str, Any]:
    """Drop ``agent_instruction`` from *merged* when it re-pins the code default.

    The settings panel GETs the *effective* config (code defaults merged in),
    so ``agent_instruction`` arrives pre-filled with the full current default; a
    Save would then persist that value, pinning it in the config file. Once
    pinned, the stored key shadows the code default and freezes the system
    prompt at that version — the exact clobber that made every later
    ``SYSTEM_PROMPT_VERSION`` bump inert in production (incident 2026-09-05).

    Stripping the key when it equals the current code default keeps the config
    file free of a redundant pin while leaving a genuine operator override (any
    value that differs from the default) untouched.
    """
    if not isinstance(merged, dict) or "agent_instruction" not in merged:
        return merged
    current_default = Settings.model_fields["agent_instruction"].default
    if merged.get("agent_instruction") == current_default:
        merged = dict(merged)
        del merged["agent_instruction"]
        logger.info(
            "Dropping re-pinned 'agent_instruction' from config save: the "
            "submitted value equals the current code default, so it is not "
            "persisted (prevents freezing the system prompt at this version)"
        )
    return merged


async def config_save_endpoint(request: Request) -> JSONResponse:
    """Deep-merge the submitted form over the existing config, validate, and persist.

    ``PUT /config`` — accepts a JSON object with the fields to update.
    Fields absent from the payload are preserved from the on-disk config.

    Returns 200 with ``{"config": ..., "version": ...}`` — the new
    effective config with secrets masked, so a client can re-render from
    the response — or 422 (RFC 9457) when the merged config fails
    :class:`~robotsix_chat.config.Settings` validation.
    """
    config_path = _resolve_config_path_from_app(request)
    body = await _parse_json_body(request)

    # 1. Read the current on-disk config (raw JSON, not model-dumped).
    existing = _read_config_json(config_path)

    # Drop JS ``String(obj)`` sentinels ("[object Object]") the settings
    # panel sometimes submits for object fields (e.g. llmio_tier_overrides)
    # so the deep-merge preserves the stored value instead of corrupting it.
    if isinstance(body, dict):
        body = _strip_corrupt_object_sentinels(body)

    # 2. Deep-merge the submitted form over the existing config.
    merged = _deep_merge(existing, body)

    # 3. Restore on-disk secrets that were submitted as masked or blank.
    merged = _preserve_masked_secrets(merged, existing, body)

    # 3b. Drop a re-pinned agent_instruction that merely echoes the code
    # default so a panel Save cannot freeze the system prompt at this version
    # (incident 2026-09-05). A genuine override (value != default) is kept.
    merged = _strip_repinned_agent_instruction(merged)

    # 4. Validate the merged config through Settings.
    try:
        Settings.model_validate(merged)
    except ValidationError as exc:
        # Extract per-precondition failures from the underlying
        # ConfigValidationError.  Pydantic v2 stores the original
        # exception in ``ctx["error"]`` of each error entry (not in
        # ``__cause__``), so we pull it from the first error's context.
        failures: list[str] = [str(exc)]
        for err in exc.errors():
            ctx_error = err.get("ctx", {}).get("error")
            if isinstance(ctx_error, ConfigValidationError):
                failures = ctx_error.failures
                break

        logger.warning(
            "Config save rejected: validation failed — %d precondition(s): %s",
            len(failures),
            failures,
        )
        return JSONResponse(
            {
                "error": "config validation failed",
                "detail": str(exc),
                "failures": failures,
            },
            status_code=422,
        )

    # 5. Compute changed keys for version history.
    changed_keys = _compute_changed_keys(existing, merged)

    # 6. Persist the merged (valid) config.
    try:
        _write_config_json(config_path, merged)
    except OSError as exc:
        logger.exception("Failed to write config to %s", config_path)
        return JSONResponse(
            _error_body(f"failed to write config: {exc}"),
            status_code=500,
        )

    # 7. Increment version and record history.
    vp = _versions_path(config_path)
    current_ver = _current_version(vp)
    new_ver = current_ver + 1
    _append_version(vp, new_ver, deepcopy(merged), changed_keys)

    logger.info(
        "Config saved to %s (version %d, %d top-level keys)",
        config_path,
        new_ver,
        len(merged),
    )
    return JSONResponse({"config": _effective_config(merged), "version": new_ver})


async def config_versions_endpoint(request: Request) -> JSONResponse:
    """Return the version history (without full config data).

    ``GET /config/versions`` — returns ``{"versions": [...]}``, each entry
    a ``{version, timestamp, changed_keys}`` record, newest first.
    """
    config_path = _resolve_config_path_from_app(request)
    vp = _versions_path(config_path)
    # Bootstrap if needed so the first GET /config/versions always
    # returns at least one entry.
    existing = _read_config_json(config_path)
    _bootstrap_version_history(config_path, existing)

    entries = _read_versions(vp)
    # Return entries newest-first, without the full data payload.
    result: list[dict[str, Any]] = []
    for entry in reversed(entries):
        result.append(
            {
                "version": entry["version"],
                "timestamp": entry["timestamp"],
                "changed_keys": entry["changed_keys"],
            }
        )
    return JSONResponse({"versions": result})


async def config_version_get_endpoint(request: Request) -> JSONResponse:
    """Return one version's stored config document with secrets masked.

    ``GET /config/versions/{version}`` — returns the exact document a
    rollback to that version would restore (the ``data`` payload recorded
    in the append-only history) under ``config``, with secret values
    masked exactly like ``GET /config``.
    """
    config_path = _resolve_config_path_from_app(request)
    version = request.path_params["version"]
    vp = _versions_path(config_path)
    entries = _read_versions(vp)

    _index, entry = _find_version_entry(entries, version)
    if entry is None:
        available = sorted(e["version"] for e in entries)
        return _problem_response(
            404,
            "Version not found",
            f"version {version} not found; available: {available}",
        )

    data = entry.get("data")
    if not isinstance(data, dict):
        data = {}

    return JSONResponse(
        {
            "config": _mask_secrets(data),
            "version": entry["version"],
            "timestamp": entry.get("timestamp"),
        }
    )


async def config_version_diff_endpoint(request: Request) -> JSONResponse:
    """Return a value-level diff of one version against the previous one.

    ``GET /config/versions/{version}/diff`` — changed key paths in
    dot-notation with ``old`` / ``new`` values (secrets masked); secret
    leaves report only ``changed: true``, never values.  Version 1 (or any
    version with no recorded predecessor) diffs against an empty document.
    """
    config_path = _resolve_config_path_from_app(request)
    version = request.path_params["version"]
    vp = _versions_path(config_path)
    entries = _read_versions(vp)

    index, entry = _find_version_entry(entries, version)
    if entry is None:
        available = sorted(e["version"] for e in entries)
        return _problem_response(
            404,
            "Version not found",
            f"version {version} not found; available: {available}",
        )

    previous_entry = entries[index - 1] if index > 0 else None
    before: dict[str, Any] = {}
    if previous_entry is not None:
        prev_data = previous_entry.get("data")
        if isinstance(prev_data, dict):
            before = prev_data
    after = entry.get("data")
    if not isinstance(after, dict):
        after = {}

    changes = _diff_dicts(before, after)
    return JSONResponse(
        {
            "version": entry["version"],
            "previous_version": previous_entry["version"] if previous_entry else 0,
            "changes": changes,
        }
    )


async def config_rollback_endpoint(request: Request) -> JSONResponse:
    """Revert config to a previous version.

    ``POST /config/rollback`` — accepts ``{"version": N}``, reverts the
    on-disk config to that version's data, and creates a **new** version
    entry (history is append-only, never destructive).
    """
    config_path = _resolve_config_path_from_app(request)
    body = await _parse_json_body(request)

    target_version = body.get("version")
    if not isinstance(target_version, int) or target_version < 1:
        return _problem_response(
            400,
            "Invalid rollback target",
            "version must be a positive integer",
        )

    vp = _versions_path(config_path)
    entries = _read_versions(vp)
    if not entries:
        return _problem_response(
            404,
            "No version history",
            "no version history exists to roll back from",
        )

    # Find the target version entry.
    target_entry: dict[str, Any] | None = None
    for entry in entries:
        if entry["version"] == target_version:
            target_entry = entry
            break

    if target_entry is None:
        available = [e["version"] for e in entries]
        return _problem_response(
            404,
            "Version not found",
            f"version {target_version} not found; available: {sorted(available)}",
        )

    target_data: dict[str, Any] = target_entry["data"]

    # Validate the target data still passes Settings validation (the schema
    # may have changed since that version was recorded).
    try:
        Settings.model_validate(target_data)
    except ValidationError as exc:
        logger.warning(
            "Rollback rejected: version %d fails current validation", target_version
        )
        return _problem_response(
            422,
            "Rollback validation failed",
            f"version {target_version} fails current config validation: {exc}",
        )

    # Compute changed keys vs current on-disk config.
    existing = _read_config_json(config_path)
    changed_keys = _compute_changed_keys(existing, target_data)

    # Write the target data as the current config.
    try:
        _write_config_json(config_path, target_data)
    except OSError as exc:
        logger.exception("Failed to write rollback config to %s", config_path)
        return JSONResponse(
            _error_body(f"failed to write config: {exc}"),
            status_code=500,
        )

    # Append a new version entry for the rollback.
    current_ver = _current_version(vp)
    new_ver = current_ver + 1
    rollback_keys = [f"rollback to v{target_version}"]
    if changed_keys:
        rollback_keys.extend(changed_keys)
    _append_version(vp, new_ver, deepcopy(target_data), rollback_keys)

    logger.info(
        "Config rolled back to version %d (now at version %d)",
        target_version,
        new_ver,
    )
    return JSONResponse({"config": _effective_config(target_data), "version": new_ver})


def _resolve_config_path_from_app(request: Request) -> Path:
    """Resolve the config file path from app state or the default env-var path.

    When the app was created with an explicit ``config_path`` in its state
    (e.g. in tests), use that; otherwise fall back to
    :func:`robotsix_config.resolve_config_path`.
    """
    config_path = getattr(request.app.state, "config_path", None)
    if config_path is not None:
        return Path(config_path)
    return resolve_config_path()
