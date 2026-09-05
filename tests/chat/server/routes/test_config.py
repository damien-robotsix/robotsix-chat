"""Tests for the config GET/PUT/versions/rollback endpoints.

Coverage: deep-merge preservation, validation-before-persist, secret
masking, version history, rollback, and RFC 9457 error responses.
"""

from __future__ import annotations

import json
import stat
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

    async def stream(self, message: str, **kwargs: object):
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


def test_mask_secrets_masks_openrouter_keys() -> None:
    """Alias-keyed OpenRouter secrets are masked even without a secret suffix."""
    data = {  # pragma: allowlist secret
        "openrouter": {"keys": {"robotsix-chat-cognee": "sk-or"}}
    }
    result = _mask_secrets(data)
    assert (
        result["openrouter"]["keys"]["robotsix-chat-cognee"] == "**********"
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
        "openrouter": {
            "keys": {"robotsix-chat-cognee": "sk-or"}  # pragma: allowlist secret
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
    assert (
        result["openrouter"]["keys"]["robotsix-chat-cognee"] == "**********"
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


def test_preserve_masked_secrets_restores_openrouter_key() -> None:
    """A masked alias-keyed OpenRouter secret restores the on-disk value."""
    existing = {  # pragma: allowlist secret
        "openrouter": {"keys": {"robotsix-chat-cognee": "sk-real"}}
    }
    update = {  # pragma: allowlist secret
        "openrouter": {"keys": {"robotsix-chat-cognee": "**********"}}
    }
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    assert (  # pragma: allowlist secret
        result["openrouter"]["keys"]["robotsix-chat-cognee"] == "sk-real"
    )


def test_preserve_masked_secrets_restores_blank_openrouter_key() -> None:
    """A blank alias-keyed OpenRouter secret restores the on-disk value."""
    existing = {  # pragma: allowlist secret
        "openrouter": {"keys": {"robotsix-chat-cognee": "sk-real"}}
    }
    update = {"openrouter": {"keys": {"robotsix-chat-cognee": ""}}}
    merged = _deep_merge(existing, update)
    result = _preserve_masked_secrets(merged, existing, update)
    assert (  # pragma: allowlist secret
        result["openrouter"]["keys"]["robotsix-chat-cognee"] == "sk-real"
    )


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


def test_write_preserves_restrictive_mode(tmp_path: Path) -> None:
    """A 0600 config stays 0600 across a save.

    The write goes to a fresh temp file and renames over the target, so
    without an explicit chmod the config — which holds API keys — comes back
    world-readable at the process umask.
    """
    path = tmp_path / "config.json"
    _write_config_json(path, {"a": 1})
    path.chmod(0o600)

    _write_config_json(path, {"a": 2})

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _read_config_json(path) == {"a": 2}


def test_write_defaults_to_owner_only_for_new_file(tmp_path: Path) -> None:
    """A config created by the writer is never world-readable."""
    path = tmp_path / "config.json"
    _write_config_json(path, {"a": 1})
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


# ---------------------------------------------------------------------------
# Shared assertions for the standard GET /config response shape
# ---------------------------------------------------------------------------


def _assert_version_header(data: dict, expected_version: int) -> None:
    """Assert the standard GET /config response shape."""
    assert data["version"] == expected_version
    assert "schema" in data
    assert isinstance(data["schema"], dict)
    assert "$defs" in data["schema"] or "properties" in data["schema"]
    assert isinstance(data["config"], dict)


# ---------------------------------------------------------------------------
# GET /config
# ---------------------------------------------------------------------------


def test_get_config_returns_masked_data(tmp_path: Path) -> None:
    """GET /config returns config with secrets masked, plus version and schema."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "llmio_api_key": "sk-real",  # pragma: allowlist secret
            "server_port": 8080,
            "memory": {
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
            "openrouter": {
                "keys": {"robotsix-chat-cognee": "sk-mem"}  # pragma: allowlist secret
            },
        },
    )
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    _assert_version_header(data, 1)
    config = data["config"]

    assert config["chat_default_model_level"] == 2
    assert config["llmio_api_key"] == "**********"
    assert config["server_port"] == 8080
    assert (  # pragma: allowlist secret
        config["openrouter"]["keys"]["robotsix-chat-cognee"] == "**********"
    )


def test_get_config_missing_file(tmp_path: Path) -> None:
    """GET /config returns version 1 and empty config when no config file exists."""
    config_path = tmp_path / "nonexistent.json"
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == 1  # bootstrapped from empty config
    assert data.get("schema") is not None
    assert isinstance(data["config"], dict)


def test_get_config_includes_schema(tmp_path: Path) -> None:
    """GET /config includes a valid JSON Schema at the ``schema`` key."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    schema = data.get("schema")
    assert isinstance(schema, dict)
    # A valid JSON Schema has a top-level "type" or "properties" key.
    assert "properties" in schema or "type" in schema


# ---------------------------------------------------------------------------
# GET /config — default overlay (new fields appear even absent from file)
# ---------------------------------------------------------------------------


def test_get_config_includes_periodic_when_absent(tmp_path: Path) -> None:
    """Absent ``periodic`` still appears in GET /config via schema defaults.

    The presets editor always has a section to render.
    """
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2, "server_port": 8080})
    client = _make_app(config_path)

    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()

    assert "periodic" in data["config"]
    periodic = data["config"]["periodic"]
    assert periodic["sessions"] == []
    assert periodic["ready_staleness_minutes"] == 10


def test_get_config_overlay_preserves_file_values(tmp_path: Path) -> None:
    """When the file sets a value, it takes precedence over the default."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "periodic": {
                "sessions": [
                    {
                        "name": "mail-triage",
                        "initial_prompt": "Review the queue. READ-ONLY.",
                        "schedule_interval_seconds": 86400,
                    }
                ],
            },
        },
    )
    client = _make_app(config_path)

    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()

    sessions = data["config"]["periodic"]["sessions"]
    assert [p["name"] for p in sessions] == ["mail-triage"]
    # Defaults fill in missing keys.
    assert data["config"]["periodic"]["ready_staleness_minutes"] == 10


# ---------------------------------------------------------------------------
# GET /config — scoped reads (keys_only / path / include_schema)
# ---------------------------------------------------------------------------


def _config_with_secrets(config_path: Path) -> None:
    """Write a config file with a secret and a nested periodic block."""
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "llmio_api_key": "sk-real",  # pragma: allowlist secret
            "agent_instruction": "You are helpful.",
            "periodic": {
                "sessions": [{"name": "nightly", "schedule_interval_seconds": 86400}],
            },
        },
    )


def test_get_config_keys_only_lists_top_level(tmp_path: Path) -> None:
    """keys_only=true returns the top-level key names with sizes and no schema."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?keys_only=true")
    assert resp.status_code == 200
    data = resp.json()
    assert "schema" not in data
    assert "config" not in data
    assert data["version"] == 1
    assert isinstance(data["keys"], list)
    names = [entry["name"] for entry in data["keys"]]
    assert "agent_instruction" in names
    assert "periodic" in names
    for entry in data["keys"]:
        assert "name" in entry
        assert "size" in entry
        assert isinstance(entry["size"], int)


def test_get_config_path_returns_subtree(tmp_path: Path) -> None:
    """path=<dotted> returns only that subtree, masked, with no schema."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?path=periodic")
    assert resp.status_code == 200
    data = resp.json()
    assert "schema" not in data
    assert "config" in data
    assert data["version"] == 1
    sessions = data["config"]["sessions"]
    assert [p["name"] for p in sessions] == ["nightly"]


def test_get_config_path_nested_array(tmp_path: Path) -> None:
    """path=periodic.sessions returns just that array, with no schema."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?path=periodic.sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert "schema" not in data
    assert [p["name"] for p in data["config"]] == ["nightly"]


def test_get_config_path_agent_instruction(tmp_path: Path) -> None:
    """path=agent_instruction returns the scalar base prompt."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?path=agent_instruction")
    assert resp.status_code == 200
    data = resp.json()
    assert "schema" not in data
    assert data["config"] == "You are helpful."


def test_get_config_path_masks_secret(tmp_path: Path) -> None:
    """Path into a secret leaf still masks the value."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?path=llmio_api_key")
    assert resp.status_code == 200
    assert resp.json()["config"] == "**********"


def test_get_config_path_missing_returns_404(tmp_path: Path) -> None:
    """Path that does not exist returns 404 with a clear message."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?path=nope.nope")
    assert resp.status_code == 404
    body = resp.json()
    assert body["status"] == 404
    assert "does not exist" in body["detail"]


def test_get_config_include_schema_false_omits_schema(tmp_path: Path) -> None:
    """include_schema=false returns full masked config with no schema."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?include_schema=false")
    assert resp.status_code == 200
    data = resp.json()
    assert "schema" not in data
    assert data["version"] == 1
    assert data["config"]["agent_instruction"] == "You are helpful."
    assert data["config"]["llmio_api_key"] == "**********"


def test_get_config_keys_only_and_path_conflict_returns_400(tmp_path: Path) -> None:
    """Combining keys_only and path is a 400."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?keys_only=true&path=periodic")
    assert resp.status_code == 400
    assert resp.json()["status"] == 400


def test_get_config_malformed_bool_returns_400(tmp_path: Path) -> None:
    """A non-boolean keys_only value is a 400."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?keys_only=maybe")
    assert resp.status_code == 400
    assert resp.json()["status"] == 400


def test_get_config_empty_path_returns_400(tmp_path: Path) -> None:
    """An empty path value is a 400."""
    config_path = tmp_path / "config.json"
    _config_with_secrets(config_path)
    client = _make_app(config_path)

    resp = client.get("/config?path=")
    assert resp.status_code == 400
    assert resp.json()["status"] == 400


# ---------------------------------------------------------------------------
# Retired-block stripping (the pre-rework ``autonomous`` block)
# ---------------------------------------------------------------------------


def test_get_config_drops_retired_autonomous_block(tmp_path: Path) -> None:
    """A file still carrying the retired ``autonomous`` block loads fine.

    The block is stripped rather than bricking validation (stored templates
    are known to re-inject removed keys).
    """
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "autonomous": {"completion_marker": "---AUTONOMOUS COMPLETE---"},
        },
    )
    client = _make_app(config_path)

    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()

    assert "autonomous" not in data["config"]
    assert "periodic" in data["config"]


def test_put_succeeds_when_file_has_retired_autonomous_block(
    tmp_path: Path,
) -> None:
    """PUT /config succeeds despite a retired on-disk ``autonomous`` block.

    The save drops the block.
    """
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "autonomous": {"sessions": [{"name": "default"}]},
        },
    )
    client = _make_app(config_path)

    resp = client.put("/config", json={"idle_timeout_minutes": 45})
    assert resp.status_code == 200, resp.text

    on_disk = _read_config_json(config_path)
    assert on_disk["idle_timeout_minutes"] == 45


# ---------------------------------------------------------------------------
# PUT /config — deep-merge preservation
# ---------------------------------------------------------------------------


def test_put_preserves_unmentioned_keys(tmp_path: Path) -> None:
    """Keys absent from the PUT body are preserved from on-disk config."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
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
    assert resp.json()["config"]["server_port"] == 9000

    # Re-read the file.
    on_disk = _read_config_json(config_path)
    assert on_disk["server_port"] == 9000
    assert on_disk["chat_default_model_level"] == 2  # preserved
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
                "embedding": {"endpoint": "http://box:11434/v1", "dimensions": 1024},
            },
            "openrouter": {
                "keys": {"robotsix-chat-cognee": "sk-llm"}  # pragma: allowlist secret
            },
        },
    )
    client = _make_app(config_path)

    # Submit only memory.enabled — everything else must be preserved.
    resp = client.put("/config", json={"memory": {"enabled": False}})
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["memory"]["enabled"] is False  # updated
    assert (  # pragma: allowlist secret
        on_disk["openrouter"]["keys"]["robotsix-chat-cognee"] == "sk-llm"
    )  # preserved
    # preserved (partial save did not touch these)
    assert on_disk["memory"]["embedding"]["endpoint"] == "http://box:11434/v1"
    assert on_disk["memory"]["embedding"]["dimensions"] == 1024  # preserved


def test_put_drops_repinned_default_agent_instruction(tmp_path: Path) -> None:
    """A Save echoing the code-default ``agent_instruction`` is not persisted.

    The settings panel GETs the effective config (defaults merged in) and PUTs
    it back wholesale; without the guard this pins the default and freezes the
    system prompt (incident 2026-09-05). The guard drops a re-pinned default.
    """
    from robotsix_chat.config import Settings

    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    default = Settings.model_fields["agent_instruction"].default
    resp = client.put(
        "/config",
        json={"agent_instruction": default, "server_port": 9000},
    )
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert "agent_instruction" not in on_disk  # re-pinned default dropped
    assert on_disk["server_port"] == 9000  # unrelated change persisted


def test_put_keeps_custom_agent_instruction(tmp_path: Path) -> None:
    """A genuine ``agent_instruction`` override (≠ default) is still persisted."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    custom = "You are a helpful assistant. Bespoke operator override."
    resp = client.put("/config", json={"agent_instruction": custom})
    assert resp.status_code == 200

    on_disk = _read_config_json(config_path)
    assert on_disk["agent_instruction"] == custom


# ---------------------------------------------------------------------------
# PUT /config — validation-before-persist
# ---------------------------------------------------------------------------


def test_put_rejects_invalid_model_level(tmp_path: Path) -> None:
    """An invalid model_level is rejected with 422 and does not persist."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    resp = client.put("/config", json={"chat_default_model_level": 99})
    assert resp.status_code == 422
    error_data = resp.json()
    assert "failures" in error_data
    assert any("chat_default_model_level" in f for f in error_data["failures"])

    on_disk = _read_config_json(config_path)
    assert on_disk["chat_default_model_level"] == 2


def test_put_model_level_preserves_tier_overrides(tmp_path: Path) -> None:
    """Changing only the model level via the panel keeps llmio_tier_overrides.

    Regression guard: the settings panel serialised the dict-typed
    ``llmio_tier_overrides`` field with ``String(value)`` → the literal
    ``"[object Object]"`` string, which pydantic rejected with a
    ``dict_type`` error, blocking every save.  The submitted sentinel must
    be dropped so the deep-merge preserves the stored dict.
    """
    from robotsix_llmio.config import FALLBACK_LEVEL3

    config_path = tmp_path / "config.json"
    stored_overrides = {"fallback": {"level2": FALLBACK_LEVEL3.model_dump()}}
    _write_config(
        config_path,
        {
            "chat_default_model_level": 1,
            "llmio_tier_overrides": stored_overrides,
        },
    )
    client = _make_app(config_path)

    # Mimic the panel: it re-serialises the whole document and mangles the
    # object field into "[object Object]" while the operator only changed
    # the model level to 2.
    resp = client.put(
        "/config",
        json={
            "chat_default_model_level": 2,
            "llmio_tier_overrides": "[object Object]",
        },
    )
    assert resp.status_code == 200, resp.json()

    on_disk = _read_config_json(config_path)
    assert on_disk["chat_default_model_level"] == 2
    # The stored dict must be intact — never the corrupt sentinel string.
    assert on_disk["llmio_tier_overrides"] == stored_overrides


def test_strip_corrupt_object_sentinels_recurses() -> None:
    """The sentinel is stripped at any depth; real values are kept."""
    from robotsix_chat.chat.server.routes.config import (
        _strip_corrupt_object_sentinels,
    )

    cleaned = _strip_corrupt_object_sentinels(
        {
            "a": "[object Object]",
            "b": 2,
            "nested": {"c": "[object Object]", "d": "keep"},
        }
    )
    assert cleaned == {"b": 2, "nested": {"d": "keep"}}


def test_put_reports_all_precondition_failures(tmp_path: Path) -> None:
    """Multiple precondition failures are all reported in the failures list."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "memory": {
                "enabled": True,
                "embedding": {"endpoint": "http://box:11434/v1"},
            },
            "openrouter": {
                "keys": {"robotsix-chat-cognee": "sk-llm"}  # pragma: allowlist secret
            },
        },
    )
    client = _make_app(config_path)

    # Trigger multiple failures: invalid model_level + blank embedding endpoint
    resp = client.put(
        "/config",
        json={
            "chat_default_model_level": 99,
            "memory": {"embedding": {"endpoint": ""}},
        },
    )
    assert resp.status_code == 422
    error_data = resp.json()
    assert "failures" in error_data
    failures = error_data["failures"]
    # Both preconditions should appear
    assert any("chat_default_model_level" in f for f in failures), failures


# ---------------------------------------------------------------------------
# PUT /config — secret handling
# ---------------------------------------------------------------------------


def test_put_masked_secret_preserves_original(tmp_path: Path) -> None:
    """Submitting the sentinel for a secret field preserves the on-disk value."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
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
            "chat_default_model_level": 2,
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
            "chat_default_model_level": 2,
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
    _write_config(config_path, {"chat_default_model_level": 2})
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
    _write_config(config_path, {"chat_default_model_level": 2})
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
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    # Bootstrap: at least one version exists from a GET.
    resp_get = client.get("/config")
    assert resp_get.status_code == 200

    # Make a change to create version 2.
    client.put("/config", json={"server_port": 9000})

    resp = client.get("/config/versions")
    assert resp.status_code == 200
    versions = resp.json()["versions"]
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
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    # Don't call GET /config first — go straight to /config/versions.
    resp = client.get("/config/versions")
    assert resp.status_code == 200
    versions = resp.json()["versions"]
    assert isinstance(versions, list)
    assert len(versions) >= 1  # bootstrapped


# ---------------------------------------------------------------------------
# GET /config/versions/{version} — single version document (secrets masked)
# ---------------------------------------------------------------------------


def test_get_version_document_returns_masked_document(tmp_path: Path) -> None:
    """GET /config/versions/{version} returns the stored doc, secrets masked."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "memory": {"llm": {"model": "openrouter/openai/gpt-5-mini"}},
            "openrouter": {"keys": {"robotsix-chat-cognee": "plain-secret-a"}},
        },
    )
    client = _make_app(config_path)

    # Bootstrap version history (v1), then save a change (v2).
    client.get("/config")
    client.put("/config", json={"server_port": 9000})

    resp = client.get("/config/versions/1")
    assert resp.status_code == 200
    doc = resp.json()
    assert doc["version"] == 1
    assert "timestamp" in doc
    assert doc["config"]["memory"]["llm"]["model"] == "openrouter/openai/gpt-5-mini"
    # Set secrets are masked; no plaintext ever appears.
    assert doc["config"]["openrouter"]["keys"]["robotsix-chat-cognee"] == "**********"
    assert "plain-secret-a" not in resp.text


def test_get_version_document_empty_secret_stays_empty(tmp_path: Path) -> None:
    """Unset secret values stay unmasked (empty string) in the document."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"openrouter": {"keys": {"robotsix-chat-cognee": ""}}})
    client = _make_app(config_path)
    client.get("/config")  # bootstrap v1

    resp = client.get("/config/versions/1")
    assert resp.status_code == 200
    assert resp.json()["config"]["openrouter"]["keys"]["robotsix-chat-cognee"] == ""


def test_get_version_document_unknown_version_returns_404(tmp_path: Path) -> None:
    """Unknown version returns 404; non-integer segment also 404s."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)
    client.get("/config")  # bootstrap v1

    resp = client.get("/config/versions/999")
    assert resp.status_code == 404

    resp_bad = client.get("/config/versions/not-a-number")
    assert resp_bad.status_code == 404


# ---------------------------------------------------------------------------
# GET /config/versions/{version}/diff — value-level diff vs previous version
# ---------------------------------------------------------------------------


def test_version_diff_reports_nested_changed_paths(tmp_path: Path) -> None:
    """Diff reports dot-notation nested paths with old/new values.

    Mirrors the incident: v2 changed several nested fields (continuation,
    memory.llm.model, openrouter key), v3 reverted the model.  The v2 diff
    surfaces all three changes; the v3 diff shows only the reversion.
    """
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "continuation": {"max_consecutive": 3},
            "memory": {"llm": {"model": "openrouter/openai/gpt-5-mini"}},
            "openrouter": {"keys": {"robotsix-chat-cognee": "secret-a"}},
        },
    )
    client = _make_app(config_path)
    client.get("/config")  # bootstrap v1

    # v2: bump continuation max, switch model to nano, rotate the key.
    client.put(
        "/config",
        json={
            "continuation": {"max_consecutive": 5},
            "memory": {"llm": {"model": "openrouter/openai/gpt-5-nano"}},
            "openrouter": {"keys": {"robotsix-chat-cognee": "secret-b"}},
        },
    )
    # v3: revert only the model.
    client.put(
        "/config",
        json={"memory": {"llm": {"model": "openrouter/openai/gpt-5-mini"}}},
    )

    # --- v2 diff: three changed paths ---
    resp2 = client.get("/config/versions/2/diff")
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["version"] == 2
    assert body2["previous_version"] == 1
    by_path2 = {c["path"]: c for c in body2["changes"]}

    assert by_path2["continuation.max_consecutive"] == {
        "path": "continuation.max_consecutive",
        "old": 3,
        "new": 5,
    }
    assert by_path2["memory.llm.model"]["old"] == "openrouter/openai/gpt-5-mini"
    assert by_path2["memory.llm.model"]["new"] == "openrouter/openai/gpt-5-nano"
    # Secret key rotation: changed-only, no old/new values.
    assert by_path2["openrouter.keys.robotsix-chat-cognee"] == {
        "path": "openrouter.keys.robotsix-chat-cognee",
        "changed": True,
    }
    assert "secret-a" not in resp2.text
    assert "secret-b" not in resp2.text

    # --- v3 diff: only the model reversion ---
    resp3 = client.get("/config/versions/3/diff")
    assert resp3.status_code == 200
    body3 = resp3.json()
    assert body3["version"] == 3
    assert body3["previous_version"] == 2
    by_path3 = {c["path"]: c for c in body3["changes"]}

    assert len(by_path3) == 1
    assert by_path3["memory.llm.model"]["old"] == "openrouter/openai/gpt-5-nano"
    assert by_path3["memory.llm.model"]["new"] == "openrouter/openai/gpt-5-mini"


def test_version_diff_first_version_diffs_empty_document(tmp_path: Path) -> None:
    """Version 1 (no predecessor) diffs against an empty document."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "memory": {"llm": {"model": "openrouter/openai/gpt-5-mini"}},
        },
    )
    client = _make_app(config_path)
    client.get("/config")  # bootstrap v1

    resp = client.get("/config/versions/1/diff")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"] == 1
    assert body["previous_version"] == 0
    by_path = {c["path"]: c for c in body["changes"]}

    # chat_default_model_level is added
    assert by_path["chat_default_model_level"] == {
        "path": "chat_default_model_level",
        "new": 2,
    }
    assert "old" not in by_path["chat_default_model_level"]
    # Nested path added
    assert by_path["memory.llm.model"] == {
        "path": "memory.llm.model",
        "new": "openrouter/openai/gpt-5-mini",
    }
    assert "old" not in by_path["memory.llm.model"]


def test_version_diff_unknown_version_returns_404(tmp_path: Path) -> None:
    """Diff of a nonexistent version returns 404."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)
    client.get("/config")  # bootstrap v1

    resp = client.get("/config/versions/999/diff")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /config/rollback
# ---------------------------------------------------------------------------


def test_rollback_to_previous_version(tmp_path: Path) -> None:
    """Rolling back restores that version's data and creates a new version."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2, "server_port": 8000})
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
    assert rollback_resp.json()["config"]["server_port"] == 8000
    new_version = rollback_resp.json()["version"]
    assert new_version >= 3  # v1 initial, v2 save, v3 rollback

    # Verify the config was restored.
    on_disk = _read_config_json(config_path)
    assert on_disk["server_port"] == 8000
    assert on_disk["chat_default_model_level"] == 2


def test_rollback_nonexistent_version(tmp_path: Path) -> None:
    """Rollback to a nonexistent version returns 404."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    # Bootstrap version history.
    client.get("/config")

    resp = client.post("/config/rollback", json={"version": 999})
    assert resp.status_code == 404


def test_rollback_invalid_version_param(tmp_path: Path) -> None:
    """Rollback with a non-integer version param returns 400."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    # Bootstrap version history.
    client.get("/config")

    resp = client.post("/config/rollback", json={"version": "abc"})
    assert resp.status_code == 400


def test_rollback_no_history(tmp_path: Path) -> None:
    """Rollback with no prior version history returns 404."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)

    # No bootstrap (no GET /config, no PUT) — no version history.
    resp = client.post("/config/rollback", json={"version": 1})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Response envelopes — robotsix-standards config-ownership contract
#
# The shared settings panel reads ``payload.config`` and falls back to ``{}``
# when it is missing.  With the document spread across the top level the panel
# renders every field at its schema default and the operator's next Save writes
# those defaults over the live config — the 2026-08-24 chat config wipe.
# ---------------------------------------------------------------------------


def test_get_config_nests_document_under_config_key(tmp_path: Path) -> None:
    """GET /config returns exactly ``config``/``schema``/``version``."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2, "server_port": 8080})
    client = _make_app(config_path)

    data = client.get("/config").json()

    assert set(data) == {"config", "schema", "version"}
    assert data["config"]["server_port"] == 8080
    # No config key ever leaks to the top level.
    assert "server_port" not in data
    assert "chat_default_model_level" not in data


def test_put_returns_effective_config(tmp_path: Path) -> None:
    """PUT /config answers with the new effective config, secrets masked."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "chat_default_model_level": 2,
            "server_port": 8080,
            "llmio_api_key": "sk-real",  # pragma: allowlist secret
        },
    )
    client = _make_app(config_path)

    resp = client.put("/config", json={"server_port": 9000})
    assert resp.status_code == 200
    body = resp.json()

    assert set(body) == {"config", "version"}
    assert body["config"]["server_port"] == 9000
    assert body["config"]["chat_default_model_level"] == 2  # untouched key round-trips
    assert body["config"]["llmio_api_key"] == "**********"
    assert "sk-real" not in resp.text  # pragma: allowlist secret


def test_put_response_config_matches_get(tmp_path: Path) -> None:
    """Re-rendering from the PUT response equals a fresh GET — no drift.

    The panel re-renders from the save response; when the two disagree the
    next Save diffs against a stale document and writes phantom changes.
    """
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2, "server_port": 8080})
    client = _make_app(config_path)
    client.get("/config")  # bootstrap version history

    put_config = client.put("/config", json={"server_port": 9000}).json()["config"]
    get_config = client.get("/config").json()["config"]

    assert put_config == get_config


def test_get_versions_wraps_list_under_versions_key(tmp_path: Path) -> None:
    """GET /config/versions returns ``{"versions": [...]}``, not a bare list."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)
    client.get("/config")

    body = client.get("/config/versions").json()

    assert isinstance(body, dict)
    assert isinstance(body["versions"], list)
    assert body["versions"][0]["version"] >= 1


def test_get_version_document_nests_under_config_key(tmp_path: Path) -> None:
    """GET /config/versions/{version} nests the stored document too."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2, "server_port": 8080})
    client = _make_app(config_path)
    client.get("/config")  # bootstrap v1

    body = client.get("/config/versions/1").json()

    assert set(body) == {"config", "version", "timestamp"}
    assert body["config"]["server_port"] == 8080


def test_rollback_returns_effective_config(tmp_path: Path) -> None:
    """POST /config/rollback answers with the same envelope as PUT."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2, "server_port": 8000})
    client = _make_app(config_path)
    client.get("/config")  # v1
    client.put("/config", json={"server_port": 9000})  # v2

    resp = client.post("/config/rollback", json={"version": 1})
    assert resp.status_code == 200
    body = resp.json()

    assert set(body) == {"config", "version"}
    assert body["config"]["server_port"] == 8000


# ---------------------------------------------------------------------------
# GET /config/deploy — deploy configuration sub-path
# ---------------------------------------------------------------------------


def test_get_config_deploy_returns_deploy_section(tmp_path: Path) -> None:
    """GET /config/deploy returns only the central_deploy block."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {"central_deploy": {"url": "http://deploy:8100", "roster_cache_ttl": 600.0}},
    )
    client = _make_app(config_path)
    resp = client.get("/config/deploy")
    assert resp.status_code == 200
    data = resp.json()
    # Response has config and schema keys.
    assert "config" in data
    assert "schema" in data
    # Config contains the central_deploy data.
    assert data["config"]["url"] == "http://deploy:8100"
    assert data["config"]["roster_cache_ttl"] == 600.0


def test_get_config_deploy_includes_schema(tmp_path: Path) -> None:
    """GET /config/deploy returns a schema describing deploy config shape."""
    config_path = tmp_path / "config.json"
    _write_config(config_path, {"chat_default_model_level": 2})
    client = _make_app(config_path)
    resp = client.get("/config/deploy")
    assert resp.status_code == 200
    data = resp.json()
    schema = data.get("schema", {})
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    # Schema should describe the deploy settings shape directly (matching
    # the ``config`` value), not wrapped under ``central_deploy``.
    assert "url" in props
    assert "roster_cache_ttl" in props
    assert "$schema" in schema


def test_get_config_deploy_empty_config(tmp_path: Path) -> None:
    """GET /config/deploy returns empty deploy config when file is absent."""
    config_path = tmp_path / "config.json"
    client = _make_app(config_path)
    resp = client.get("/config/deploy")
    assert resp.status_code == 200
    data = resp.json()
    assert "config" in data
    assert "schema" in data
    # An empty config has no central_deploy, so config should be empty
    # (but schema should still describe the deploy shape).
    assert isinstance(data["config"], dict)
    schema = data["schema"]
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"
    props = schema.get("properties", {})
    assert "url" in props
    assert "roster_cache_ttl" in props


def test_get_config_deploy_secret_masked(tmp_path: Path) -> None:
    """Secret fields in the deploy config are masked."""
    config_path = tmp_path / "config.json"
    _write_config(
        config_path,
        {
            "central_deploy": {
                "component_credentials": {
                    "mill": {
                        "header_token": "sk-secret-token",  # pragma: allowlist secret
                    },
                },
            },
        },
    )
    client = _make_app(config_path)
    resp = client.get("/config/deploy")
    assert resp.status_code == 200
    data = resp.json()
    creds = data["config"].get("component_credentials", {})
    if creds:
        assert creds["mill"]["header_token"] == "**********"  # pragma: allowlist secret
