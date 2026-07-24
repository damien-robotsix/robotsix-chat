"""Config endpoints — read and update the server's config file.

``GET /config`` returns the current config (secrets masked) with version
and JSON Schema metadata.

``PUT /config`` deep-merges the submitted form over the existing persisted
config, validates, increments the version, and persists.  A field absent
from the submitted payload is preserved, not blanked.

``GET /config/versions`` returns the version history (without full data).

``POST /config/rollback`` reverts to a previous version and creates a new
version entry (append-only history, never destructive).
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from robotsix_config import resolve_config_path
from starlette.requests import Request
from starlette.responses import JSONResponse

from robotsix_chat.config import Settings

from ._shared import _parse_json_body
from .errors import _error_body

logger = logging.getLogger(__name__)

# Sentinel for masked secret values — the UI sends this back when the
# user has not changed a secret; we preserve the on-disk value.
_MASKED_SECRET_SENTINEL = "**********"


# ---------------------------------------------------------------------------
# Deep merge
# ---------------------------------------------------------------------------


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
    :data:`_SECRET_KEY_SUFFIXES`).
    """

    def _walk(m: dict[str, Any], e: dict[str, Any], u: dict[str, Any]) -> None:
        for key in list(m.keys()):
            uv = u.get(key)
            if _is_secret_key(key) and (uv == _MASKED_SECRET_SENTINEL or uv == ""):
                if key in e:
                    m[key] = deepcopy(e[key])
                continue
            if (
                isinstance(m[key], dict)
                and isinstance(e.get(key), dict)
                and isinstance(uv, dict)
            ):
                _walk(m[key], e[key], uv)

    _walk(merged, existing, update)
    return merged


def _mask_secrets(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *data* with secret field values replaced by the sentinel.

    Secret fields are identified by key-name suffix (see
    :data:`_SECRET_KEY_SUFFIXES`).
    """

    def _walk(d: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in d.items():
            if _is_secret_key(key) and isinstance(value, str) and value:
                result[key] = _MASKED_SECRET_SENTINEL
            elif isinstance(value, dict):
                result[key] = _walk(value)
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
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        # codeql[py/clear-text-storage-sensitive-data]
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
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


# ---------------------------------------------------------------------------
# JSON Schema (cached at module level)
# ---------------------------------------------------------------------------

# Module-level cache for the JSON Schema — generated once at import time
# and re-used by every GET /config call.  Use a sentinel to detect when
# Settings.model_json_schema() has not been called yet (lazy import in
# tests may not trigger it).
_settings_json_schema: dict[str, Any] | None = None


def _get_schema() -> dict[str, Any]:
    """Return the JSON Schema for :class:`Settings`, cached at module level."""
    global _settings_json_schema
    if _settings_json_schema is None:
        _settings_json_schema = Settings.model_json_schema()
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


async def config_get_endpoint(request: Request) -> JSONResponse:
    """Return the current on-disk config with secrets masked, plus version and schema.

    ``GET /config`` — no auth (gateway handles it).
    """
    config_path = _resolve_config_path_from_app(request)
    data = _read_config_json(config_path)

    # Ensure version history is bootstrapped.
    version = _bootstrap_version_history(config_path, data)

    # Build the response: version + schema + (masked) config keys at top level.
    masked = _mask_secrets(data)
    response: dict[str, Any] = {
        "version": version,
        "schema": _get_schema(),
    }
    response.update(masked)
    return JSONResponse(response)


async def config_save_endpoint(request: Request) -> JSONResponse:
    """Deep-merge the submitted form over the existing config, validate, and persist.

    ``PUT /config`` — accepts a JSON object with the fields to update.
    Fields absent from the payload are preserved from the on-disk config.

    Returns 200 with the new version on success, 422 (RFC 9457) when the
    merged config fails :class:`~robotsix_chat.config.Settings` validation.
    """
    config_path = _resolve_config_path_from_app(request)
    body = await _parse_json_body(request)

    # 1. Read the current on-disk config (raw JSON, not model-dumped).
    existing = _read_config_json(config_path)

    # 2. Deep-merge the submitted form over the existing config.
    merged = _deep_merge(existing, body)

    # 3. Restore on-disk secrets that were submitted as masked or blank.
    merged = _preserve_masked_secrets(merged, existing, body)

    # 4. Validate the merged config through Settings.
    try:
        Settings.model_validate(merged)
    except ValidationError as exc:
        logger.warning(
            "Config save rejected: validation failed — %s",
            exc,
        )
        return _problem_response(
            422,
            "Config Validation Failed",
            str(exc),
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
    return JSONResponse({"version": new_ver, "status": "ok"})


async def config_versions_endpoint(request: Request) -> JSONResponse:
    """Return the version history (without full config data).

    ``GET /config/versions`` — returns a list of ``{version, timestamp,
    changed_keys}`` entries, newest first.
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
    return JSONResponse(result)


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
    return JSONResponse({"version": new_ver, "status": "ok"})


# ---------------------------------------------------------------------------
# Helper: resolve config path from app state or env
# ---------------------------------------------------------------------------


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
