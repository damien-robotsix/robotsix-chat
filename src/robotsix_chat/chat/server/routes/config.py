"""Config endpoints — read and update the server's config file.

``GET /config`` returns the current config (secrets masked).
``PUT /config`` deep-merges the submitted form over the existing persisted
config, validates the result through :class:`~robotsix_chat.config.Settings`,
and only persists on success.  A field absent from the submitted payload is
preserved, not blanked.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
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
_MASKED_SECRET_SENTINEL = "***"


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


# Suffixes that mark a config key as a secret field.  Any key whose
# name *ends with* one of these suffixes is treated as a secret.
_SECRET_KEY_SUFFIXES: tuple[str, ...] = (
    "_api_key",
    "_api_token",
    "_secret_key",
    "_private_key",
    "_github_token",
    "api_key",
    "api_token",
    "secret_key",
    "private_key",
    "github_token",
    "public_key",
    "deploy_api_key",
)


def _is_secret_key(key: str) -> bool:
    """Return ``True`` when *key* names a secret field."""
    return key.endswith(_SECRET_KEY_SUFFIXES)


def _preserve_masked_secrets(
    merged: dict[str, Any], existing: dict[str, Any], update: dict[str, Any]
) -> dict[str, Any]:
    """Replace masked secret sentinels in *merged* with their on-disk values.

    When the UI submits a masked value (``"***"``) for a secret field we
    treat it as "unchanged" and restore the existing on-disk value.

    Secret fields are identified by key-name suffix (see
    :data:`_SECRET_KEY_SUFFIXES`).
    """

    def _walk(m: dict[str, Any], e: dict[str, Any], u: dict[str, Any]) -> None:
        for key in list(m.keys()):
            uv = u.get(key)
            if uv == _MASKED_SECRET_SENTINEL and _is_secret_key(key):
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
    """Return a copy of *data* with secret field values replaced by ``"***"``.

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
# Endpoints
# ---------------------------------------------------------------------------


async def config_get_endpoint(request: Request) -> JSONResponse:
    """Return the current on-disk config with secrets masked.

    ``GET /config`` — no auth (gateway handles it).
    """
    config_path = _resolve_config_path_from_app(request)
    data = _read_config_json(config_path)
    return JSONResponse(_mask_secrets(data))


async def config_save_endpoint(request: Request) -> JSONResponse:
    """Deep-merge the submitted form over the existing config, validate, and persist.

    ``PUT /config`` — accepts a JSON object with the fields to update.
    Fields absent from the payload are preserved from the on-disk config.

    Returns 200 on success, 422 when the merged config fails
    :class:`~robotsix_chat.config.Settings` validation.
    """
    config_path = _resolve_config_path_from_app(request)
    body = await _parse_json_body(request)

    # 1. Read the current on-disk config (raw JSON, not model-dumped).
    existing = _read_config_json(config_path)

    # 2. Deep-merge the submitted form over the existing config.
    merged = _deep_merge(existing, body)

    # 3. Restore on-disk secrets that were submitted as masked.
    merged = _preserve_masked_secrets(merged, existing, body)

    # 4. Validate the merged config through Settings.
    try:
        Settings.model_validate(merged)
    except ValidationError as exc:
        logger.warning(
            "Config save rejected: validation failed — %s",
            exc,
        )
        return JSONResponse(
            {
                "error": "config validation failed",
                "detail": str(exc),
            },
            status_code=422,
        )

    # 5. Persist the merged (valid) config.
    try:
        _write_config_json(config_path, merged)
    except OSError as exc:
        logger.exception("Failed to write config to %s", config_path)
        return JSONResponse(
            _error_body(f"failed to write config: {exc}"),
            status_code=500,
        )

    logger.info("Config saved to %s (%d top-level keys)", config_path, len(merged))
    return JSONResponse({"status": "ok"})


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
