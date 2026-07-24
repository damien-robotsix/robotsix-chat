"""Tests for the config GET/PUT/versions/rollback endpoints.

Coverage: deep-merge preservation, validation-before-persist, secret
masking, version history, rollback, and RFC 9457 error responses.
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
    """Secret keys are replaced with ``**********``."""
    data = {"memory": {"llm": {"api_key": "sk-secret"}}}  # pragma: allowlist secret
    result = _mask_secrets(data)
    assert (
        result["memory"]["llm"]["api_key"] == "**********"
    )  # pragma: allowlist secret


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
        "llmio_api_key": "sk-abc",  # pragma: allowlist secret
        "memory": {
            "llm": {"api_key": "sk-def"},  # pragma: allowlist secret
            "embedding": {"api_key": "sk-ghi"},  # pragma: allowlist secret
        },
        "langfuse": {"secret_key": "sk-lf"},  # pragma: allowlist secret
        "direct_repo": {"github_app_private_key": "pk"},  # pragma: allowlist secret
    }
    result = _mask_secrets(data)
    assert result["llmio_api_key"] == "**********"
    assert (
        result["memory"]["llm"]["api_key"] == "**********"
    )  # pragma: allowlist secret
    assert (
        result["memory"]["embedding"]["api_key"] == "**********"
    )  # pragma: allowlist secret
    assert result["langfuse"]["secret_key"] == "**********"  # pragma: allowlist secret
    assert (
        result["direct_repo"]["github_app_private_key"] == "**********"
    )  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# _preserve_masked_secrets
# ---------------------------------------------------------------------------


def test_preserve_masked_secrets_restores_original() -> None:
    """When update has sentinel for a secret, the existing value is restored."""
    existing = {"memory": {"llm": {"api_key": "sk-real"}}}  # pragma: allowlist secret
    update = {"memory": {"llm": {"api_key": "**********"}}}  # pragma: allowlist secret
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    assert result["memory"]["llm"]["api_key"] == "sk-real"  # pragma: allowlist secret


def test_preserve_masked_secrets_restores_on_blank() -> None:
    """When update has blank string for a secret, the existing value is restored."""
    existing = {"memory": {"llm": {"api_key": "sk-real"}}}  # pragma: allowlist secret
    update = {"memory": {"llm": {"api_key": ""}}}  # pragma: allowlist secret
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    assert result["memory"]["llm"]["api_key"] == "sk-real"  # pragma: allowlist secret


def test_preserve_masked_secrets_lets_new_value_through() -> None:
    """When update supplies a real (non-masked) secret, it is kept."""
    existing = {"memory": {"llm": {"api_key": "sk-old"}}}  # pragma: allowlist secret
    update = {"memory": {"llm": {"api_key": "sk-new"}}}  # pragma: allowlist secret
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    assert result["memory"]["llm"]["api_key"] == "sk-new"  # pragma: allowlist secret


def test_preserve_masked_secrets_non_secret_not_affected() -> None:
    """Non-secret fields with sentinel value are NOT treated as masked."""
    existing = {"server_host": "0.0.0.0"}
    update = {"server_host": "**********"}
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    # "server_host" is not a secret key, so sentinel is kept as-is
    assert result["server_host"] == "**********"


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
# Shared assertions for the standard GET /config response shape
# ---------------------------------------------------------------------------


def _assert_version_header(data: dict, expected_version: int) -> None:
    """Assert the standard GET /config response shape."""
    assert data["version"] == expected_version
    assert "schema" in data
    assert isinstance(data["schema"], dict)
    assert "$defs" in data["schema"] or "properties" in data["schema"]


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


def test_get_config_returns_masked_data(tmp_path: Path) -> None:
    """GET /config returns config with secrets masked, plus version and schema."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "llmio_api_key": "sk-real",  # pragma: allowlist secret
            "server_port": 8080,
            "memory": {
                "llm": {"api_key": "sk-mem"},  # pragma: allowlist secret
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
        },
    )
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    _assert_version_header(data, 1)

    assert data["llmio_model_level"] == 3
    assert data["llmio_api_key"] == "**********"
    assert data["server_port"] == 8080
    assert data["memory"]["llm"]["api_key"] == "**********"  # pragma: allowlist secret
    assert data["memory"]["embedding"]["endpoint"] == "http://box:11434/v1"


def test_get_config_missing_file(tmp_path: Path) -> None:
    """GET /config returns version 1 and empty config when no config file exists."""
    config_path = tmp_path / "nonexistent.json"
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1  # bootstrapped from empty config
    assert data.get("schema") is not None


def test_get_config_includes_schema(tmp_path: Path) -> None:
    """GET /config includes a valid JSON Schema at the ``schema`` key."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    schema = data.get("schema")
    assert isinstance(schema, dict)
    # A valid JSON Schema has a top-level "type" or "properties" key.
    assert "properties" in schema or "type" in schema


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
    assert resp.json()["version"] >= 1
    assert resp.json()["status"] == "ok"

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
                "llm": {"api_key": "sk-llm"},  # pragma: allowlist secret
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
    assert (
        on_disk["memory"]["llm"]["api_key"] == "sk-llm"  # pragma: allowlist secret
    )  # preserved
    # preserved (partial save did not touch these)
    assert on_disk["memory"]["embedding"]["endpoint"] == "http://box:11434/v1"
    assert on_disk["memory"]["embedding"]["dimensions"] == 1024  # preserved


# ---------------------------------------------------------------------------
# PUT /config — validation-before-persist
# ---------------------------------------------------------------------------


def test_put_rejects_invalid_config(tmp_path: Path) -> None:
    """A save that would yield an invalid config is rejected with 422 (RFC 9457)."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "memory": {
                "enabled": True,
                "llm": {"api_key": "sk-llm"},  # pragma: allowlist secret
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
    assert resp.headers["Content-Type"] == "application/json"
    error_data = resp.json()
    assert error_data["error"] == "config validation failed"
    assert "memory.embedding.endpoint" in error_data.get("detail", "")
    assert "failures" in error_data
    assert any("memory.embedding.endpoint" in f for f in error_data["failures"])

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
    error_data = resp.json()
    assert "failures" in error_data
    assert any("llmio.model_level" in f for f in error_data["failures"])

    on_disk = _read_config_json(config_path)
    assert on_disk["llmio_model_level"] == 3


def test_put_reports_all_precondition_failures(tmp_path: Path) -> None:
    """Multiple precondition failures are all reported in the failures list."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "memory": {
                "enabled": True,
                "llm": {"api_key": "sk-llm"},  # pragma: allowlist secret
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
        },
    )
    client = _make_app(config_path)

    # Trigger multiple failures: invalid model_level + blank embedding endpoint
    resp = client.put(
        "/config",
        json={
            "llmio_model_level": 99,
            "memory": {"embedding": {"endpoint": ""}},
        },
    )
    assert resp.status_code == 422
    error_data = resp.json()
    assert "failures" in error_data
    failures = error_data["failures"]
    # Both preconditions should appear
    assert any("llmio.model_level" in f for f in failures), failures
    assert any("memory.embedding.endpoint" in f for f in failures), failures


# ---------------------------------------------------------------------------
# PUT /config — secret handling
# ---------------------------------------------------------------------------


def test_put_masked_secret_preserves_original(tmp_path: Path) -> None:
    """Submitting the sentinel for a secret field preserves the on-disk value."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "llmio_api_key": "sk-real-key",  # pragma: allowlist secret
        },
    )
    client = _make_app(config_path)

    resp = client.put("/config", json={"llmio_api_key": "**********"})
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["llmio_api_key"] == "sk-real-key"  # pragma: allowlist secret


def test_put_blank_secret_preserves_original(tmp_path: Path) -> None:
    """Submitting an empty string for a secret field preserves the on-disk value."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "llmio_api_key": "sk-real-key",  # pragma: allowlist secret
        },
    )
    client = _make_app(config_path)

    resp = client.put("/config", json={"llmio_api_key": ""})
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["llmio_api_key"] == "sk-real-key"  # pragma: allowlist secret


def test_put_new_secret_overwrites_existing(tmp_path: Path) -> None:
    """Submitting a real (non-masked) secret overwrites the on-disk value."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "llmio_model_level": 3,
            "llmio_api_key": "sk-old",  # pragma: allowlist secret
        },
    )
    client = _make_app(config_path)

    resp = client.put(
        "/config",
        json={"llmio_api_key": "sk-new"},  # pragma: allowlist secret
    )
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["llmio_api_key"] == "sk-new"  # pragma: allowlist secret


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


# ---------------------------------------------------------------------------
# PUT /config — version increment
# ---------------------------------------------------------------------------


def test_put_increments_version(tmp_path: Path) -> None:
    """Each successful PUT increments the version number."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    resp1 = client.put("/config", json={"server_port": 9000})
    assert resp1.status_code == 200
    v1 = resp1.json()["version"]
    assert v1 >= 1

    resp2 = client.put("/config", json={"idle_timeout_minutes": 30})
    assert resp2.status_code == 200
    v2 = resp2.json()["version"]
    assert v2 == v1 + 1


# ---------------------------------------------------------------------------
# GET /config/versions
# ---------------------------------------------------------------------------


def test_get_versions_returns_history(tmp_path: Path) -> None:
    """GET /config/versions returns version history entries."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    # Bootstrap: at least one version exists from a GET.
    resp_get = client.get("/config")
    assert resp_get.status_code == 200

    # Make a change to create version 2.
    client.put("/config", json={"server_port": 9000})

    resp = client.get("/config/versions")
    assert resp.status_code == 200
    versions = resp.json()
    assert isinstance(versions, list)
    assert len(versions) >= 2  # initial + save

    # Each entry has the standard keys and no data payload.
    for entry in versions:
        assert "version" in entry
        assert "timestamp" in entry
        assert "changed_keys" in entry
        assert "data" not in entry  # full config data excluded

    # Newest first.
    assert versions[0]["version"] > versions[-1]["version"]


def test_get_versions_no_history(tmp_path: Path) -> None:
    """GET /config/versions bootstraps if no prior history exists."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    # Don't call GET /config first — go straight to /config/versions.
    resp = client.get("/config/versions")
    assert resp.status_code == 200
    versions = resp.json()
    assert isinstance(versions, list)
    assert len(versions) >= 1  # bootstrapped


# ---------------------------------------------------------------------------
# POST /config/rollback
# ---------------------------------------------------------------------------


def test_rollback_to_previous_version(tmp_path: Path) -> None:
    """Rolling back restores that version's data and creates a new version."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3, "server_port": 8000})
    client = _make_app(config_path)

    # Bootstrap version history.
    client.get("/config")

    # Change server_port to 9000 (version 2).
    resp = client.put("/config", json={"server_port": 9000})
    assert resp.status_code == 200

    # Confirm it's 9000 on disk.
    assert _read_config_json(config_path)["server_port"] == 9000

    # Roll back to version 1 (server_port was 8000).
    rollback_resp = client.post("/config/rollback", json={"version": 1})
    assert rollback_resp.status_code == 200
    assert rollback_resp.json()["status"] == "ok"
    new_version = rollback_resp.json()["version"]
    assert new_version >= 3  # v1 initial, v2 save, v3 rollback

    # Verify the config was restored.
    on_disk = _read_config_json(config_path)
    assert on_disk["server_port"] == 8000
    assert on_disk["llmio_model_level"] == 3


def test_rollback_nonexistent_version(tmp_path: Path) -> None:
    """Rollback to a nonexistent version returns 404."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    # Bootstrap version history.
    client.get("/config")

    resp = client.post("/config/rollback", json={"version": 999})
    assert resp.status_code == 404


def test_rollback_invalid_version_param(tmp_path: Path) -> None:
    """Rollback with a non-integer version param returns 400."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    # Bootstrap version history.
    client.get("/config")

    resp = client.post("/config/rollback", json={"version": "abc"})
    assert resp.status_code == 400


def test_rollback_no_history(tmp_path: Path) -> None:
    """Rollback with no prior version history returns 404."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"llmio_model_level": 3})
    client = _make_app(config_path)

    # No bootstrap (no GET /config, no PUT) — no version history.
    resp = client.post("/config/rollback", json={"version": 1})
    assert resp.status_code == 404
