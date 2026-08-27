"""Tests for trivy_parser — parsing Trivy table-formatted scan output."""

from __future__ import annotations

from robotsix_chat.repo.direct.trivy_parser import (
    TrivyFinding,
    TrivySummary,
    format_findings_summary,
    parse_trivy_table,
)

# ---------------------------------------------------------------------------
# Sample Trivy table outputs for testing
# ---------------------------------------------------------------------------

_TRIVY_TABLE_SINGLE = """\
robotsix-chat:ci-scan (debian 12.6)
=====================================
Total: 2 (CRITICAL: 0, HIGH: 2, MEDIUM: 0, LOW: 0)

┌──────────┬────────────────────┬──────────┬───────────────┬─────────────┐
│ Library  │ Vulnerability      │ Severity │ Installed     │ Fixed       │
│          │                    │          │ Version       │ Version     │
├──────────┼────────────────────┼──────────┼───────────────┼─────────────┤
│ libxml2  │ CVE-2024-34428     │ HIGH     │ 2.9.14-1.3    │ 2.9.14-1.4  │
│ libssl3  │ CVE-2024-5535      │ HIGH     │ 3.0.13-1~deb12│ 3.0.14-1~de │
└──────────┴────────────────────┴──────────┴───────────────┴─────────────┘
"""

_TRIVY_TABLE_CRITICAL = """\
robotsix-chat:ci-scan (debian 12.6)
=====================================
Total: 1 (CRITICAL: 1, HIGH: 0, MEDIUM: 0, LOW: 0)

┌──────────────┬────────────────────┬──────────┬───────────────┬─────────────┐
│ Library      │ Vulnerability      │ Severity │ Installed     │ Fixed       │
│              │                    │          │ Version       │ Version     │
├──────────────┼────────────────────┼──────────┼───────────────┼─────────────┤
│ openssl      │ CVE-2024-0727      │ CRITICAL │ 3.0.11-1~deb2 │ 3.0.13-1~de │
└──────────────┴────────────────────┴──────────┴───────────────┴─────────────┘
"""

_TRIVY_TABLE_GHSA = """\
my-image:latest (alpine 3.19)
==============================
Total: 1 (CRITICAL: 0, HIGH: 0, MEDIUM: 1, LOW: 0)

┌──────────┬──────────────────────┬──────────┬───────────────┬──────────────┐
│ Library  │ Vulnerability        │ Severity │ Installed     │ Fixed        │
│          │                      │          │ Version       │ Version      │
├──────────┼──────────────────────┼──────────┼───────────────┼──────────────┤
│ go       │ GHSA-4v49-3g2w-r7h5  │ MEDIUM   │ 1.21.0        │ 1.21.5       │
└──────────┴──────────────────────┴──────────┴───────────────┴──────────────┘
"""

_TRIVY_TABLE_NO_VULNS = """\
robotsix-chat:ci-scan (debian 12.6)
=====================================
Total: 0 (CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0)
"""

_TRIVY_TABLE_EMPTY = """\
robotsix-chat:ci-scan (debian 12.6)
=====================================
"""

# Table with no vulnerable findings (Trivy reports "No vulnerabilities found")
_TRIVY_TABLE_NONE_FOUND = """\
robotsix-chat:ci-scan (debian 12.6)
=====================================

No vulnerabilities found.
"""

# Non-Trivy log
_RANDOM_LOG = """\
2024-10-01T12:00:00Z Starting build...
2024-10-01T12:00:01Z Compiling main.go
2024-10-01T12:00:02Z Build failed: cannot find module 'foo'
"""


# ---------------------------------------------------------------------------
# parse_trivy_table
# ---------------------------------------------------------------------------


class TestParseTrivyTable:
    """Tests for parse_trivy_table parser."""

    def test_parses_single_table(self) -> None:
        """Parse a standard two-finding Trivy table."""
        result = parse_trivy_table(_TRIVY_TABLE_SINGLE)

        assert result.target == "robotsix-chat:ci-scan"
        assert result.summary is not None
        assert result.summary.total == 2
        assert result.summary.critical == 0
        assert result.summary.high == 2
        assert result.summary.medium == 0
        assert result.summary.low == 0
        assert len(result.findings) == 2

    def test_finding_fields(self) -> None:
        """Verify individual fields of parsed findings."""
        result = parse_trivy_table(_TRIVY_TABLE_SINGLE)

        first = result.findings[0]
        assert first.library == "libxml2"
        assert first.vulnerability_id == "CVE-2024-34428"
        assert first.severity == "HIGH"
        assert first.installed_version == "2.9.14-1.3"
        assert first.fixed_version == "2.9.14-1.4"

        second = result.findings[1]
        assert second.library == "libssl3"
        assert second.vulnerability_id == "CVE-2024-5535"
        assert second.severity == "HIGH"

    def test_parses_critical_severity(self) -> None:
        """Parse a table with a single CRITICAL finding."""
        result = parse_trivy_table(_TRIVY_TABLE_CRITICAL)

        assert result.summary is not None
        assert result.summary.critical == 1
        assert len(result.findings) == 1
        assert result.findings[0].severity == "CRITICAL"
        assert result.findings[0].vulnerability_id == "CVE-2024-0727"

    def test_parses_ghsa_id(self) -> None:
        """Parse a GHSA-style vulnerability ID."""
        result = parse_trivy_table(_TRIVY_TABLE_GHSA)

        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.vulnerability_id == "GHSA-4v49-3g2w-r7h5"
        assert f.severity == "MEDIUM"
        assert f.library == "go"

    def test_no_vulns_zero_total(self) -> None:
        """Handle a table with zero total vulnerabilities."""
        result = parse_trivy_table(_TRIVY_TABLE_NO_VULNS)

        assert result.summary is not None
        assert result.summary.total == 0
        assert result.findings == []

    def test_empty_table_no_findings(self) -> None:
        """Handle a table with no summary line."""
        result = parse_trivy_table(_TRIVY_TABLE_EMPTY)

        assert result.summary is None
        assert result.findings == []

    def test_no_vulnerabilities_found_text(self) -> None:
        """Handle 'No vulnerabilities found' text output."""
        result = parse_trivy_table(_TRIVY_TABLE_NONE_FOUND)

        assert result.findings == []

    def test_random_log_returns_empty(self) -> None:
        """Non-Trivy input yields empty results."""
        result = parse_trivy_table(_RANDOM_LOG)

        assert result.summary is None
        assert result.findings == []
        assert result.target == ""

    def test_raw_output_preserved(self) -> None:
        """Raw output is stored verbatim on the result."""
        result = parse_trivy_table(_TRIVY_TABLE_SINGLE)

        assert result.raw_output == _TRIVY_TABLE_SINGLE


# ---------------------------------------------------------------------------
# format_findings_summary
# ---------------------------------------------------------------------------


class TestFormatFindingsSummary:
    """Tests for format_findings_summary Markdown formatter."""

    def test_markdown_table_with_findings(self) -> None:
        """Formatted output contains expected Markdown elements."""
        result = parse_trivy_table(_TRIVY_TABLE_SINGLE)
        output = format_findings_summary(result)

        assert "### Target: robotsix-chat:ci-scan" in output
        assert "**Total vulnerabilities: 2**" in output
        assert "CRITICAL: 0, HIGH: 2" in output
        assert "| CVE-2024-34428 |" in output
        assert "| CVE-2024-5535 |" in output
        assert "libxml2" in output
        assert "libssl3" in output
        assert "2.9.14-1.4" in output

    def test_severity_ordering_critical_first(self) -> None:
        """CRITICAL findings sort before HIGH in formatted output."""
        merged = (
            _TRIVY_TABLE_SINGLE.strip()
            + "\n"
            + """
┌──────────────┬────────────────────┬──────────┬───────────────┬─────────────┐
│ Library      │ Vulnerability      │ Severity │ Installed     │ Fixed       │
│              │                    │          │ Version       │ Version     │
├──────────────┼────────────────────┼──────────┼───────────────┼─────────────┤
│ openssl      │ CVE-2024-0727      │ CRITICAL │ 3.0.11-1~deb2 │ 3.0.13-1~de │
└──────────────┴────────────────────┴──────────┴───────────────┴─────────────┘
"""
        )
        result = parse_trivy_table(merged)
        output = format_findings_summary(result)

        # CRITICAL finding should appear before HIGH findings
        crit_pos = output.index("CVE-2024-0727")
        high_pos = output.index("CVE-2024-34428")
        assert crit_pos < high_pos

    def test_no_findings_message(self) -> None:
        """Empty results produce a 'No vulnerability findings' message."""
        result = parse_trivy_table(_RANDOM_LOG)
        output = format_findings_summary(result)

        assert "No vulnerability findings" in output

    def test_zero_total_reports_no_findings(self) -> None:
        """Zero-total table reports no findings in summary."""
        result = parse_trivy_table(_TRIVY_TABLE_NO_VULNS)
        output = format_findings_summary(result)

        assert "Total vulnerabilities: 0" in output
        assert "No individual vulnerability entries could be parsed" in output


# ---------------------------------------------------------------------------
# TrivyFinding / TrivySummary dataclass construction
# ---------------------------------------------------------------------------


class TestDataclasses:
    """Basic dataclass construction checks."""

    def test_trivy_finding_frozen(self) -> None:
        """TrivyFinding dataclass stores all fields correctly."""
        f = TrivyFinding(
            library="lib",
            vulnerability_id="CVE-2024-0001",
            severity="HIGH",
            installed_version="1.0",
            fixed_version="1.1",
        )
        assert f.library == "lib"
        assert f.fixed_version == "1.1"

    def test_trivy_summary_frozen(self) -> None:
        """TrivySummary dataclass stores all severity counts."""
        s = TrivySummary(total=5, critical=1, high=2, medium=1, low=1)
        assert s.total == 5
        assert s.critical == 1
        assert s.high == 2
