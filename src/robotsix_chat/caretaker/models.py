"""Parsed representation of a central-deploy caretaker finding.

central-deploy's caretaker loop exposes a list of findings at
``GET /caretaker/status``.  The exact wire schema is owned by
central-deploy (see central-deploy #924); this module parses it
**tolerantly** — accepting the documented field names plus common
aliases — so a minor schema drift degrades to "field absent" rather
than crashing the model-free poll loop.

A finding is identified fleet-wide by its ``(kind, component)`` pair;
the watcher de-duplicates operator notifications on that pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Severity string (case-folded) that marks a finding as operator-worthy.
ERROR_SEVERITY = "error"

# Human-readable default titles per surfaced finding kind, used when the
# finding itself carries no explicit title.
_DEFAULT_TITLES: dict[str, str] = {
    "claude_auth": "Claude credential unusable - fleet-wide",
    "crash_loop": "Component crash loop - fleet-wide",
    "health": "Component health check failing - fleet-wide",
}

# Default remediation text per kind, used when the finding carries no
# ``detail`` / remediation of its own.
_DEFAULT_REMEDIATION: dict[str, str] = {
    "claude_auth": (
        "Re-login from the Claude Auth panel in the central-deploy dashboard."
    ),
}


def _first_str(data: dict[str, Any], *keys: str) -> str:
    """Return the first present, non-empty string among *keys* in *data*."""
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


@dataclass(frozen=True)
class CaretakerFinding:
    """One caretaker finding, normalised from the ``/caretaker/status`` payload.

    Attributes:
        kind: Finding category, case-folded (e.g. ``claude_auth``,
            ``crash_loop``, ``health``).
        component: Affected component id (e.g. ``robotsix-chat``); empty
            string when the finding is fleet-wide / unscoped.
        severity: Severity string, case-folded (``error`` is surfaced).
        detail: Operator-facing remediation / explanation text.
        title: Short human-readable title for the notification.

    """

    kind: str
    component: str
    severity: str
    detail: str
    title: str

    @property
    def dedup_key(self) -> tuple[str, str]:
        """Fleet-wide identity: the ``(kind, component)`` pair."""
        return (self.kind, self.component)

    @property
    def is_error(self) -> bool:
        """Whether this finding is at error severity (operator-worthy)."""
        return self.severity == ERROR_SEVERITY

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> CaretakerFinding | None:
        """Build a finding from one raw payload entry, or ``None`` if unusable.

        Tolerant to field-name drift: ``kind``/``type``,
        ``component``/``component_id``/``service``,
        ``severity``/``level``, and
        ``detail``/``remediation``/``message``/``summary``/``description``.
        Returns ``None`` when no ``kind`` can be determined.
        """
        kind = _first_str(raw, "kind", "type").lower()
        if not kind:
            return None
        component = _first_str(raw, "component", "component_id", "service")
        severity = _first_str(raw, "severity", "level").lower()
        detail = _first_str(
            raw,
            "detail",
            "remediation",
            "message",
            "summary",
            "description",
        )
        if not detail:
            detail = _DEFAULT_REMEDIATION.get(kind, "")
        title = _first_str(raw, "title")
        if not title:
            title = _DEFAULT_TITLES.get(kind, f"Caretaker finding: {kind}")
        return cls(
            kind=kind,
            component=component,
            severity=severity,
            detail=detail,
            title=title,
        )

    def render_body(self) -> str:
        """Notification body text: component line + remediation detail."""
        scope = self.component or "fleet-wide"
        if self.detail:
            return f"[{scope}] {self.detail}"
        return f"[{scope}] {self.kind} finding reported by central-deploy caretaker."


def parse_findings(payload: Any) -> list[CaretakerFinding]:
    """Parse a ``/caretaker/status`` response body into findings.

    Accepts either a bare JSON list of finding objects or a dict with a
    ``findings`` (or ``items``) list.  Non-dict entries and entries
    without a ``kind`` are skipped.  Never raises on shape drift — an
    unrecognised payload yields an empty list.
    """
    if isinstance(payload, dict):
        entries = payload.get("findings")
        if entries is None:
            entries = payload.get("items")
    else:
        entries = payload
    if not isinstance(entries, list):
        return []
    findings: list[CaretakerFinding] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        finding = CaretakerFinding.from_payload(entry)
        if finding is not None:
            findings.append(finding)
    return findings
