"""Tests for the config contract + validation module."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from robotsix_chat.component_agent.config_contract import (
    _REDACTED_SENTINEL,
    SETTABLE_KEYS,
    ConfigContractError,
    apply_config_update,
    describe_config,
    get_config_snapshot,
    validate_config_update,
)
from robotsix_chat.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _settings_dict(settings: Settings) -> dict[str, Any]:
    """Return ``settings.model_dump()`` for field-by-field comparison."""
    return settings.model_dump()


# ---------------------------------------------------------------------------
# SETTABLE_KEYS is a subset of real Settings fields
# ---------------------------------------------------------------------------


def test_settable_keys_are_real_fields():
    """Every key in SETTABLE_KEYS must correspond to a real Settings field."""
    # Build a set of all valid dotted paths from a default Settings instance.
    snapshot = get_config_snapshot(Settings())
    valid_keys = set(snapshot.keys())

    for key in SETTABLE_KEYS:
        assert key in valid_keys, (
            f"SETTABLE_KEYS key {key!r} does not exist in Settings; "
            f"the allowlist may have drifted from the model"
        )


def test_settable_keys_paths_resolve():
    """Every SETTABLE_KEYS path must resolve via getattr on a real instance."""
    s = Settings()
    for key, meta in SETTABLE_KEYS.items():
        target: Any = s
        for step in meta["path"]:
            target = getattr(target, step)
        # If we got here the path resolved — just ensure it's not another model
        # (scalar fields should be leaf values, not nested BaseModels).
        # We allow list/dict though.
        assert not hasattr(target, "model_fields"), (
            f"SETTABLE_KEYS path for {key!r} resolved to a nested model, "
            f"not a leaf field"
        )


# ---------------------------------------------------------------------------
# get_config_snapshot
# ---------------------------------------------------------------------------


def test_snapshot_includes_all_fields():
    """get_config_snapshot returns entries for every leaf field."""
    s = Settings()
    snap = get_config_snapshot(s)

    # Spot-check a few well-known keys from different sections.
    assert snap["server.log_level"] == "INFO"
    assert snap["server.idle_timeout_minutes"] == 30
    assert snap["mill.enabled"] is False
    assert snap["memory.enabled"] is False
    assert snap["conversation.max_history_turns"] == 50
    assert snap["llmio.model_level"] == 3  # read-only, but still present

    # Nested sub-model fields
    assert "memory.llm.api_key" in snap
    assert "memory.embedding.endpoint" in snap


def test_snapshot_redacts_secrets():
    """No secret-bearing value leaks into the snapshot."""
    s = Settings(
        llmio_api_key="sk-secret-123",
        mill={"broker_token": "tok-mill"},
        refdocs={"github_token": "ghp_secret"},
        board_reader={"api_token": "br-token"},
    )
    snap = get_config_snapshot(s)

    # All secret-bearing keys must be redacted.
    secret_keys = [
        "llmio.api_key",
        "mill.broker_token",
        "mail.api_token",
        "calendar.broker_token",
        "refdocs.github_token",
        "board_reader.api_token",
        "memory.llm.api_key",
        "memory.embedding.api_key",
    ]
    for key in secret_keys:
        assert snap.get(key) == _REDACTED_SENTINEL, (
            f"Secret key {key!r} was not redacted — got {snap.get(key)!r}"
        )

    # Non-secret keys must show real values.
    assert snap["server.log_level"] == "INFO"
    assert snap["mill.enabled"] is False


def test_snapshot_redacts_mail_calendar_tokens():
    """Mail and calendar API tokens are also redacted."""
    s = Settings(
        mail={"api_token": "mail-tok"},
        calendar={"broker_token": "cal-tok"},
    )
    snap = get_config_snapshot(s)
    assert snap["mail.api_token"] == _REDACTED_SENTINEL
    assert snap["calendar.broker_token"] == _REDACTED_SENTINEL


def test_snapshot_is_deterministic():
    """Two snapshots of the same settings are equal."""
    s = Settings()
    snap1 = get_config_snapshot(s)
    snap2 = get_config_snapshot(s)
    assert snap1 == snap2


# ---------------------------------------------------------------------------
# describe_config
# ---------------------------------------------------------------------------


def test_describe_config_returns_settable_metadata():
    """describe_config exposes settable keys and their types."""
    desc = describe_config()
    assert "settable" in desc
    settable = desc["settable"]
    assert isinstance(settable, dict)
    for key, meta in SETTABLE_KEYS.items():
        assert key in settable
        assert settable[key]["type"] == meta["type"]


# ---------------------------------------------------------------------------
# validate_config_update — accept paths
# ---------------------------------------------------------------------------


def test_validate_empty_updates_passes():
    """Empty updates always pass validation."""
    s = Settings()
    result = validate_config_update(s, {})
    assert result == {}


def test_validate_valid_single_scalar():
    """A single valid scalar update passes."""
    s = Settings()
    result = validate_config_update(s, {"server.log_level": "DEBUG"})
    assert result == {"server.log_level": "DEBUG"}


def test_validate_valid_multiple_keys():
    """Multiple valid updates pass together."""
    s = Settings()
    result = validate_config_update(
        s,
        {
            "server.log_level": "WARNING",
            "conversation.max_history_turns": 20,
            "server.idle_timeout_minutes": 45,
        },
    )
    assert len(result) == 3


def test_validate_valid_bool_flags():
    """Feature enabled flags can be toggled."""
    # mill.enabled=true requires a broker_token (cross-field constraint).
    s = Settings(mill={"broker_token": "test-token"})
    result = validate_config_update(s, {"mill.enabled": True, "memory.enabled": False})
    assert result == {"mill.enabled": True, "memory.enabled": False}


def test_validate_float_accepts_int():
    """subsessions.min_interval_seconds accepts int (coerced to float)."""
    s = Settings()
    result = validate_config_update(s, {"subsessions.min_interval_seconds": 120})
    assert result == {"subsessions.min_interval_seconds": 120}


def test_validate_list_field():
    """List-typed field accepts a list."""
    s = Settings()
    result = validate_config_update(
        s, {"server.cors_allow_origins": ["https://example.com"]}
    )
    assert result == {"server.cors_allow_origins": ["https://example.com"]}


# ---------------------------------------------------------------------------
# validate_config_update — reject paths
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_key():
    """Unknown keys are rejected with UNKNOWN_KEY code."""
    s = Settings()
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"no.such.key": 42})
    err = exc_info.value
    assert err.code == "UNKNOWN_KEY"
    assert "no.such.key" in err.message
    assert err.details["unknown_keys"] == ["no.such.key"]


def test_validate_rejects_multiple_unknown_keys():
    """All unknown keys are reported together."""
    s = Settings()
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"bad.one": 1, "bad.two": 2})
    err = exc_info.value
    assert err.code == "UNKNOWN_KEY"
    assert err.details["unknown_keys"] == ["bad.one", "bad.two"]


def test_validate_rejects_bool_for_int():
    """Passing a bool where an int is expected is rejected."""
    s = Settings()
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"server.idle_timeout_minutes": True})
    err = exc_info.value
    assert err.code == "TYPE_MISMATCH"
    assert any(
        e["key"] == "server.idle_timeout_minutes" for e in err.details["type_errors"]
    )


def test_validate_rejects_wrong_type_for_str():
    """Passing an int where a str is expected is rejected."""
    s = Settings()
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"server.log_level": 123})
    err = exc_info.value
    assert err.code == "TYPE_MISMATCH"
    assert any(e["key"] == "server.log_level" for e in err.details["type_errors"])


def test_validate_rejects_str_for_bool():
    """Passing a string where a bool is expected is rejected."""
    s = Settings()
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"mill.enabled": "yes"})
    err = exc_info.value
    assert err.code == "TYPE_MISMATCH"


def test_validate_rejects_cross_field_mill_enabled_without_token():
    """Enabling mill without broker_token fails cross-field validation."""
    s = Settings(mill={"broker_token": ""})
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"mill.enabled": True})
    err = exc_info.value
    assert err.code == "CROSS_FIELD_INVALID"
    assert "broker_token" in err.message.lower() or "mill" in err.message.lower()


def test_validate_rejects_cross_field_memory_enabled_without_llm_api_key():
    """Enabling memory without memory.llm.api_key fails cross-field validation."""
    s = Settings(
        memory={"llm": {"api_key": ""}, "embedding": {"endpoint": "http://x:11434/v1"}}
    )
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"memory.enabled": True})
    err = exc_info.value
    assert err.code == "CROSS_FIELD_INVALID"
    assert "api_key" in err.message.lower() or "memory" in err.message.lower()


def test_validate_rejects_refdocs_enabled_without_repos():
    """Enabling refdocs without repos fails cross-field validation."""
    s = Settings(refdocs={"repos": []})
    with pytest.raises(ConfigContractError) as exc_info:
        validate_config_update(s, {"refdocs.enabled": True})
    err = exc_info.value
    assert err.code == "CROSS_FIELD_INVALID"
    assert "repos" in err.message.lower()


# ---------------------------------------------------------------------------
# apply_config_update — accept paths
# ---------------------------------------------------------------------------


def test_apply_valid_update_mutates_settings():
    """A valid update changes the live Settings instance."""
    s = Settings()
    assert s.log_level == "INFO"
    audit = apply_config_update(s, {"server.log_level": "DEBUG"})
    assert s.log_level == "DEBUG"
    assert "server.log_level" in audit
    assert audit["server.log_level"] == ("INFO", "DEBUG")


def test_apply_multiple_updates():
    """Multiple keys can be updated in one call."""
    s = Settings()
    audit = apply_config_update(
        s,
        {
            "server.log_level": "WARNING",
            "conversation.max_history_turns": 25,
            "server.idle_timeout_minutes": 60,
        },
    )
    assert s.log_level == "WARNING"
    assert s.conversation.max_history_turns == 25
    assert s.idle_timeout_minutes == 60
    assert len(audit) == 3


def test_apply_toggle_enabled_flag():
    """Toggling a feature enabled flag works."""
    s = Settings()
    assert s.mill.enabled is False
    # mill.enabled=true requires broker_token; provide one
    s.mill.broker_token = "test-token"
    audit = apply_config_update(s, {"mill.enabled": True})
    assert s.mill.enabled is True
    assert audit["mill.enabled"] == (False, True)


def test_apply_conversation_persist_path():
    """Setting a conversation persist_path works."""
    s = Settings()
    audit = apply_config_update(s, {"conversation.persist_path": "/tmp/convs.json"})  # noqa: S108
    assert s.conversation.persist_path == "/tmp/convs.json"  # noqa: S108
    assert audit["conversation.persist_path"] == (
        ".data/conversations.json",
        "/tmp/convs.json",  # noqa: S108
    )


def test_apply_audit_uses_snapshot_redaction():
    """Audit record sources values from get_config_snapshot (secret-safe).

    Even though no currently-settable key is secret-bearing, the audit path
    reads old/new values via get_config_snapshot(), which always redacts
    secrets.  If a secret-bearing key were ever added to SETTABLE_KEYS, its
    audit entries would automatically be redacted.

    We verify this indirectly: the same snapshot used by audit already redacts
    every secret in the full snapshot (test_snapshot_redacts_secrets).  Here
    we assert that the audit record for a non-secret settable key returns
    genuine, non-redacted values — confirming the pipeline works.
    """
    s = Settings()
    audit = apply_config_update(s, {"server.log_level": "ERROR"})
    # Non-secret key should show real old and new values.
    assert audit["server.log_level"] == ("INFO", "ERROR")


def test_apply_empty_updates_noop():
    """Empty updates return an empty audit and do nothing."""
    s = Settings()
    original = _settings_dict(s)
    audit = apply_config_update(s, {})
    assert audit == {}
    assert _settings_dict(s) == original


# ---------------------------------------------------------------------------
# apply_config_update — reject paths (no mutation)
# ---------------------------------------------------------------------------


def test_apply_rejected_does_not_mutate():
    """A rejected update leaves the Settings instance byte-for-byte unchanged."""
    s = Settings()
    original_dump = _settings_dict(s)

    with pytest.raises(ConfigContractError):
        apply_config_update(s, {"no.such.key": 1})

    assert _settings_dict(s) == original_dump


def test_apply_rejected_cross_field_does_not_mutate():
    """A cross-field rejection leaves Settings completely unchanged."""
    s = Settings(mill={"broker_token": ""})
    original_dump = _settings_dict(s)

    with pytest.raises(ConfigContractError):
        apply_config_update(s, {"mill.enabled": True})

    assert _settings_dict(s) == original_dump


def test_apply_rejected_type_mismatch_no_mutation():
    """Type mismatch rejection does not mutate Settings."""
    s = Settings()
    original_dump = _settings_dict(s)

    with pytest.raises(ConfigContractError):
        apply_config_update(s, {"server.log_level": 123})

    assert _settings_dict(s) == original_dump


def test_apply_partial_update_before_failure_does_not_mutate():
    """Even if some keys are valid, a failing update mutates nothing."""
    s = Settings()
    original_dump = _settings_dict(s)

    with pytest.raises(ConfigContractError):
        # server.log_level is valid, bad.key is unknown
        apply_config_update(s, {"server.log_level": "DEBUG", "bad.key": 1})

    # The valid key must NOT have been applied.
    assert _settings_dict(s) == original_dump
    assert s.log_level == "INFO"  # unchanged


# ---------------------------------------------------------------------------
# apply_config_update — audit logging
# ---------------------------------------------------------------------------


def test_apply_logs_audit_record(caplog: pytest.LogCaptureFixture):
    """Successful apply emits an INFO log with redacted old→new values."""
    s = Settings()
    with caplog.at_level(
        logging.INFO, logger="robotsix_chat.component_agent.config_contract"
    ):
        apply_config_update(s, {"server.log_level": "DEBUG"})

    assert any("Config updated" in record.message for record in caplog.records)
    assert any("INFO → DEBUG" in record.message for record in caplog.records)


def test_apply_audit_record_structure():
    """Audit record maps changed keys to (old, new) tuples."""
    s = Settings()
    audit = apply_config_update(
        s,
        {"server.log_level": "ERROR", "server.idle_timeout_minutes": 15},
    )
    assert isinstance(audit, dict)
    assert audit["server.log_level"] == ("INFO", "ERROR")
    assert audit["server.idle_timeout_minutes"] == (30, 15)


# ---------------------------------------------------------------------------
# Error shape matches protocol.Error framing
# ---------------------------------------------------------------------------


def test_config_contract_error_has_code_message_details():
    """ConfigContractError carries code, message, and details."""
    err = ConfigContractError(
        code="TEST_CODE",
        message="Something went wrong",
        details={"key": "value"},
    )
    assert err.code == "TEST_CODE"
    assert err.message == "Something went wrong"
    assert err.details == {"key": "value"}
    assert str(err) == "Something went wrong"


def test_config_contract_error_defaults_details_to_empty_dict():
    """``details`` defaults to {} when not provided."""
    err = ConfigContractError(code="E", message="M")
    assert err.details == {}


# ---------------------------------------------------------------------------
# Settable keys are declared as genuinely live-mutable
# ---------------------------------------------------------------------------


def test_settable_keys_excludes_startup_only_fields():
    """Startup-only fields must NOT appear in SETTABLE_KEYS."""
    excluded = [
        "server.host",
        "server.port",
        "llmio.model_level",
        "llmio.api_key",
        "llmio.subagent_model",
        "llmio.check_loop_model",
        "agent.instruction",
        "server.correlation_id_header",
        "mill.broker_host",
        "mill.broker_port",
        "mill.broker_scheme",
        "mill.broker_token",
        "mill.agent_id",
        "mill.board_manager_id",
        "mill.repo_id",
        "mill.timeout",
        "mail.api_base_url",
        "mail.api_token",
        "mail.timeout",
        "calendar.broker_host",
        "calendar.broker_port",
        "calendar.broker_scheme",
        "calendar.broker_token",
        "calendar.agent_id",
        "calendar.calendar_agent_id",
        "calendar.timeout",
        "memory.data_dir",
        "memory.recall_search_type",
        "memory.llm.provider",
        "memory.llm.model",
        "memory.llm.endpoint",
        "memory.llm.api_key",
        "memory.embedding.provider",
        "memory.embedding.model",
        "memory.embedding.endpoint",
        "memory.embedding.dimensions",
        "memory.embedding.api_key",
        "memory.embedding.huggingface_tokenizer",
        "refdocs.repos",
        "refdocs.ref",
        "refdocs.github_token",
        "refdocs.base_url",
        "refdocs.timeout",
        "board_reader.api_base_url",
        "board_reader.api_token",
        "board_reader.cache_ttl",
        "knowledge.path",
        "self_review.recent_activity_limit",
        "conversation.idle_reset_seconds",
    ]
    for key in excluded:
        assert key not in SETTABLE_KEYS, (
            f"Startup-only field {key!r} should not be in SETTABLE_KEYS"
        )


def test_settable_keys_includes_expected_live_mutable_fields():
    """The conservative initial allowlist includes the documented fields."""
    expected = [
        "server.log_level",
        "server.idle_timeout_minutes",
        "conversation.max_history_turns",
        "mill.enabled",
        "mail.enabled",
        "calendar.enabled",
        "memory.enabled",
        "refdocs.enabled",
        "knowledge.enabled",
        "self_review.enabled",
        "board_reader.enabled",
    ]
    for key in expected:
        assert key in SETTABLE_KEYS, (
            f"Expected live-mutable key {key!r} missing from SETTABLE_KEYS"
        )
