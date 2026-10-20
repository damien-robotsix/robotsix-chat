"""Tests for tolerant caretaker-finding parsing."""

from __future__ import annotations

from robotsix_chat.caretaker.models import (
    CaretakerFinding,
    parse_findings,
)


def test_parse_bare_list() -> None:
    findings = parse_findings(
        [
            {
                "kind": "claude_auth",
                "component": "robotsix-chat",
                "severity": "error",
                "detail": "refresh token expired",
            }
        ]
    )
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "claude_auth"
    assert f.component == "robotsix-chat"
    assert f.is_error
    assert f.dedup_key == ("claude_auth", "robotsix-chat")
    assert "refresh token expired" in f.render_body()


def test_parse_findings_wrapper_dict() -> None:
    findings = parse_findings({"findings": [{"kind": "health", "severity": "error"}]})
    assert [f.kind for f in findings] == ["health"]


def test_parse_items_wrapper_dict() -> None:
    findings = parse_findings({"items": [{"type": "crash_loop", "level": "error"}]})
    assert len(findings) == 1
    assert findings[0].kind == "crash_loop"
    assert findings[0].severity == "error"


def test_field_aliases() -> None:
    (f,) = parse_findings(
        [
            {
                "type": "crash_loop",
                "component_id": "robotsix-invest",
                "level": "ERROR",
                "message": "restart count 42 in 5m",
            }
        ]
    )
    assert f.kind == "crash_loop"
    assert f.component == "robotsix-invest"
    assert f.severity == "error"  # case-folded
    assert "restart count 42" in f.detail


def test_claude_auth_default_title_and_remediation() -> None:
    (f,) = parse_findings([{"kind": "claude_auth", "severity": "error"}])
    assert f.title == "Claude credential unusable - fleet-wide"
    # No detail supplied → the kind's default remediation is used.
    assert "Re-login" in f.detail


def test_malformed_payload_degrades_to_empty() -> None:
    assert parse_findings(None) == []
    assert parse_findings("not-json-shaped") == []
    assert parse_findings({"nope": 1}) == []
    # Entries that are not dicts or lack a kind are skipped.
    assert parse_findings([1, "x", {}, {"severity": "error"}]) == []


def test_render_body_unscoped_component() -> None:
    f = CaretakerFinding(
        kind="claude_auth",
        component="",
        severity="error",
        detail="re-login",
        title="t",
    )
    assert f.render_body() == "[fleet-wide] re-login"
