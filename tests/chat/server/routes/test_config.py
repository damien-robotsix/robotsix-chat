"""Tests for the config GET/PUT endpoints.

Coverage: deep-merge preservation, validation-before-persist, secret masking.
"""

from __future__ import annotations

import json
from pathlib import Path

from starlette.testclient import TestClient

from robotsix_chat.chat.server.app import create_app
from robotsix_chat.chat.server.routes.config import (
    _deep_merge,
    _mask_secrets,
    _preserve_masked_secrets,
    _read_config_json,
    _write_config_json,
)

# ---------------------------------------------------------------------------
# Dummy agent for TestClient
# ---------------------------------------------------------------------------


class _DummyAgent:
    """Minimal agent stub — only ``stream`` is called by the chat endpoint."""

    async def stream(self, message: str):
        yield "ok"
        return

    # stream() is the only method called; cancel, tool_calls, etc. are
    # optional.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(config_path: Path) -> TestClient:
    """Build a Starlette TestClient with a *config_path* wired."""
    app = create_app(
        _DummyAgent(),
        config_path=str(config_path),
        serve_ui=False,
    )
    return TestClient(app, raise_server_exceptions=False)


def _write_config(path: Path, data: dict) -> None:
    """Write *data* as JSON to *path*."""
    path.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


def test_deep_merge_preserves_existing_key() -> None:
    """A key absent from the update dict is preserved from existing."""
    existing = {"a": 1, "b": {"c": 2, "d": 3}}
    update = {"b": {"c": 99}}
    result = _deep_merge(existing, update)
    assert result["a"] == 1
    assert result["b"]["c"] == 99  # updated
    assert result["b"]["d"] == 3  # preserved


def test_deep_merge_adds_new_key() -> None:
    """A key present only in update is added."""
    existing = {"a": 1}
    update = {"b": 2}
    result = _deep_merge(existing, update)
    assert result == {"a": 1, "b": 2}


def test_deep_merge_overwrites_scalar() -> None:
    """A scalar value in update replaces the existing value."""
    existing = {"a": "old"}
    update = {"a": "new"}
    result = _deep_merge(existing, update)
    assert result == {"a": "new"}


def test_deep_merge_overwrites_dict_with_scalar() -> None:
    """When update supplies a scalar for an existing dict key, it replaces."""
    existing = {"a": {"nested": True}}
    update = {"a": "scalar"}
    result = _deep_merge(existing, update)
    assert result == {"a": "scalar"}


def test_deep_merge_deeply_nested() -> None:
    """Deep merge works through multiple nesting levels."""
    existing = {"a": {"b": {"c": 1, "d": 2}}}
    update = {"a": {"b": {"c": 99}}}
    result = _deep_merge(existing, update)
    assert result["a"]["b"]["c"] == 99
    assert result["a"]["b"]["d"] == 2


def test_deep_merge_does_not_mutate_existing() -> None:
    """The existing dict is not mutated by the merge."""
    existing = {"a": {"b": 1}}
    update = {"a": {"c": 2}}
    _deep_merge(existing, update)
    assert existing == {"a": {"b": 1}}


# ---------------------------------------------------------------------------
# _mask_secrets
# ---------------------------------------------------------------------------


def test_mask_secrets_masks_api_key() -> None:
    """Secret keys are replaced with ``***``."""
    data = {"memory": {"llm": {"api_key": "sk-secret"}}}
    result = _mask_secrets(data)
    assert result["memory"]["llm"]["api_key"] == "***"


def test_mask_secrets_preserves_non_secret() -> None:
    """Non-secret fields are passed through unchanged."""
    data = {"server_port": 8080, "memory": {"enabled": True}}
    result = _mask_secrets(data)
    assert result["server_port"] == 8080
    assert result["memory"]["enabled"] is True


def test_mask_secrets_empty_string_not_masked() -> None:
    """Empty string secret values are not masked (no secret to hide)."""
    data = {"llmio_api_key": ""}
    result = _mask_secrets(data)
    assert result["llmio_api_key"] == ""


def test_mask_secrets_masks_multiple_keys() -> None:
    """Multiple secret keys at different nesting levels are all masked."""
    data = {
        "llmio_api_key": "sk-abc",
        "memory": {
            "llm": {"api_key": "sk-def"},
            "embedding": {"api_key": "sk-ghi"},
        },
        "langfuse": {"secret_key": "sk-lf"},
        "direct_repo": {"github_app_private_key": "pk"},
    }
    result = _mask_secrets(data)
    assert result["llmio_api_key"] == "***"
    assert result["memory"]["llm"]["api_key"] == "***"
    assert result["memory"]["embedding"]["api_key"] == "***"
    assert result["langfuse"]["secret_key"] == "***"
    assert result["direct_repo"]["github_app_private_key"] == "***"


# ---------------------------------------------------------------------------
# _preserve_masked_secrets
# ---------------------------------------------------------------------------


def test_preserve_masked_secrets_restores_original() -> None:
    """When update has ``***`` for a secret, the existing value is restored."""
    existing = {"memory": {"llm": {"api_key": "sk-real"}}}
    update = {"memory": {"llm": {"api_key": "***"}}}
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    assert result["memory"]["llm"]["api_key"] == "sk-real"


def test_preserve_masked_secrets_lets_new_value_through() -> None:
    """When update supplies a real (non-masked) secret, it is kept."""
    existing = {"memory": {"llm": {"api_key": "sk-old"}}}
    update = {"memory": {"llm": {"api_key": "sk-new"}}}
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    assert result["memory"]["llm"]["api_key"] == "sk-new"


def test_preserve_masked_secrets_non_secret_not_affected() -> None:
    """Non-secret fields with ``***`` value are NOT treated as masked."""
    existing = {"server_host": "0.0.0.0"}
    update = {"server_host": "***"}
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    # "server_host" is not a secret key, so "***" is kept as-is
    assert result["server_host"] == "***"


# ---------------------------------------------------------------------------
# _read_config_json / _write_config_json
# ---------------------------------------------------------------------------


def test_read_config_json_existing(tmp_path: Path) -> None:
    """Reads valid JSON from an existing file."""
    path = tmp_path / "config.json"
    _write_config(path, {"a": 1})
    result = _read_config_json(path)
    assert result == {"a": 1}


def test_read_config_json_missing(tmp_path: Path) -> None:
    """Returns empty dict when the file does not exist."""
    path = tmp_path / "nonexistent.json"
    result = _read_config_json(path)
    assert result == {}


def test_read_config_json_empty(tmp_path: Path) -> None:
    """Returns empty dict when the file is empty."""
    path = tmp_path / "config.json"
    path.write_text("")
    result = _read_config_json(path)
    assert result == {}


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    """Data written with _write_config_json can be read back."""
    path = tmp_path / "config.json"
    data = {"a": 1, "b": {"c": [1, 2, 3]}}
    _write_config_json(path, data)
    result = _read_config_json(path)
    assert result == data


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


def test_get_config_returns_masked_data(tmp_path: Path) -> None:
    """GET /config returns the on-disk config with secrets masked."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "llmio_api_key": "sk-real",
            "server_port": 8080,
            "memory": {
                "llm": {"api_key": "sk-mem"},
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
        },
    )
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["llmio_model_level"] == 3
    assert data["llmio_api_key"] == "***"
    assert data["server_port"] == 8080
    assert data["memory"]["llm"]["api_key"] == "***"
    assert data["memory"]["embedding"]["endpoint"] == "http://box:11434/v1"


def test_get_config_missing_file(tmp_path: Path) -> None:
    """GET /config returns empty object when no config file exists."""
    config_path = tmp_path / "nonexistent.json"
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    assert resp.json() == {}


# ---------------------------------------------------------------------------
# PUT /config — deep-merge preservation
# ---------------------------------------------------------------------------


def test_put_preserves_unmentioned_keys(tmp_path: Path) -> None:
    """Keys absent from the PUT body are preserved from on-disk config."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "server_port": 8080,
            "memory": {
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
        },
    )
    client = _make_app(config_path)

    # Submit only server_port — memory.embedding.endpoint must be preserved.
    resp = client.put("/config", json={"server_port": 9000})
    assert resp.status_code == 200

    # Re-read the file.
    on_disk = _read_config_json(config_path)
    assert on_disk["server_port"] == 9000
    assert on_disk["llmio_model_level"] == 3  # preserved
    # preserved (not blanked by partial save)
    assert on_disk["memory"]["embedding"]["endpoint"] == "http://box:11434/v1"


def test_put_preserves_nested_object_keys(tmp_path: Path) -> None:
    """Submitting a partial nested object preserves sibling keys."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "memory": {
                "enabled": True,
                "llm": {"api_key": "sk-llm"},
                "embedding": {"endpoint": "http://box:11434/v1", "dimensions": 1024},
            },
        },
    )
    client = _make_app(config_path)

    # Submit only memory.enabled — everything else must be preserved.
    resp = client.put("/config", json={"memory": {"enabled": False}})
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["memory"]["enabled"] is False  # updated
    assert on_disk["memory"]["llm"]["api_key"] == "sk-llm"  # preserved
    # preserved (partial save did not touch these)
    assert on_disk["memory"]["embedding"]["endpoint"] == "http://box:11434/v1"
    assert on_disk["memory"]["embedding"]["dimensions"] == 1024  # preserved


# ---------------------------------------------------------------------------
# PUT /config — validation-before-persist
# ---------------------------------------------------------------------------


def test_put_rejects_invalid_config(tmp_path: Path) -> None:
    """A save that would yield an invalid config is rejected with 422."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "memory": {
                "enabled": True,
                "llm": {"api_key": "sk-llm"},
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
        },
    )
    client = _make_app(config_path)

    # Blank the embedding endpoint — this would make memory invalid.
    resp = client.put(
        "/config",
        json={"memory": {"embedding": {"endpoint": ""}}},
    )
    assert resp.status_code == 422
    error_data = resp.json()
    assert "error" in error_data
    assert "memory.embedding.endpoint" in error_data.get("detail", "")

    # Assert the on-disk config was NOT modified.
    on_disk = _read_config_json(config_path)
    assert on_disk["memory"]["embedding"]["endpoint"] == "http://box:11434/v1"


def test_put_rejects_invalid_model_level(tmp_path: Path) -> None:
    """An invalid model_level is rejected with 422 and does not persist."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    resp = client.put("/config", json={"llmio_model_level": 99})
    assert resp.status_code == 422

    on_disk = _read_config_json(config_path)
    assert on_disk["llmio_model_level"] == 3


# ---------------------------------------------------------------------------
# PUT /config — secret handling
# ---------------------------------------------------------------------------


def test_put_masked_secret_preserves_original(tmp_path: Path) -> None:
    """Submitting ``***`` for a secret field preserves the on-disk value."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "llmio_api_key": "sk-real-key",
        },
    )
    client = _make_app(config_path)

    resp = client.put("/config", json={"llmio_api_key": "***"})
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["llmio_api_key"] == "sk-real-key"


def test_put_new_secret_overwrites_existing(tmp_path: Path) -> None:
    """Submitting a real (non-masked) secret overwrites the on-disk value."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "llmio_api_key": "sk-old",
        },
    )
    client = _make_app(config_path)

    resp = client.put("/config", json={"llmio_api_key": "sk-new"})
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["llmio_api_key"] == "sk-new"


# ---------------------------------------------------------------------------
# PUT /config — malformed requests
# ---------------------------------------------------------------------------


def test_put_invalid_json(tmp_path: Path) -> None:
    """Malformed JSON body returns 400."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {})
    client = _make_app(config_path)

    resp = client.put(
        "/config",
        content="not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_put_array_body(tmp_path: Path) -> None:
    """An array body returns 400 (expected object)."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {})
    client = _make_app(config_path)

    resp = client.put("/config", json=[1, 2, 3])
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# PUT /config — full Settings validation round-trip
# ---------------------------------------------------------------------------


def test_put_settings_validation_roundtrip(tmp_path: Path) -> None:
    """A valid full config round-trips through Settings validation."""
    from robotsix_chat.config import Settings

    config_path = tmp_path / "config.json"
    # Start with a minimal valid config.
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    # Add server_port and idle_timeout.
    resp = client.put(
        "/config",
        json={"server_port": 9000, "idle_timeout_minutes": 15},
    )
    assert resp.status_code == 200

    # Verify the saved config passes Settings validation.
    on_disk = _read_config_json(config_path)
    settings = Settings.model_validate(on_disk)
    assert settings.server_port == 9000
    assert settings.idle_timeout_minutes == 15
