"""Parser for Trivy table-formatted vulnerability scan output.

Extracts structured CVE findings from Trivy's ``--format table`` output
as produced by ``aquasecurity/trivy-action`` in CI workflows.  The parser
handles the Unicode box-drawing table format that Trivy uses, including
the summary header with total/critical/high/medium/low counts.

Example Trivy table output::

    robotsix-chat:ci-scan (debian 12.6)
    =====================================
    Total: 2 (CRITICAL: 0, HIGH: 2, MEDIUM: 0, LOW: 0)

    ┌──────────┬────────────────┬──────────┬─────────────┬─────────────┐
    │ Library  │ Vulnerability  │ Severity │  Installed  │    Fixed    │
    │          │                │          │   Version   │   Version   │
    ├──────────┼────────────────┼──────────┼─────────────┼─────────────┤
    │ libxml2  │ CVE-2024-34428│  HIGH    │ 2.9.14-1.3  │ 2.9.14-1.4  │
    └──────────┴────────────────┴──────────┴─────────────┴─────────────┘
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TrivyFinding:
    """A single parsed vulnerability finding from Trivy table output."""

    library: str
    vulnerability_id: str
    severity: str
    installed_version: str
    fixed_version: str


@dataclass(frozen=True)
class TrivySummary:
    """Parsed summary line from Trivy table output header."""

    total: int
    critical: int
    high: int
    medium: int
    low: int


@dataclass(frozen=True)
class TrivyParseResult:
    """Complete parse of a Trivy table-formatted scan output."""

    findings: list[TrivyFinding]
    summary: TrivySummary | None
    target: str
    raw_output: str


# Matches the summary line: "Total: 2 (CRITICAL: 0, HIGH: 2, MEDIUM: 0, LOW: 0)"
_SUMMARY_RE = re.compile(
    r"Total:\s*(\d+)\s*"
    r"\(\s*CRITICAL:\s*(\d+)\s*,\s*HIGH:\s*(\d+)\s*,\s*MEDIUM:\s*(\d+)\s*,\s*LOW:\s*(\d+)\s*\)"
)

# Matches the target line (first non-empty line before === separator):
# "image (os os-version)"
_TARGET_RE = re.compile(r"^(.+?)\s+\(.*\)\s*$")

# Matches a CVE-like ID (CVE-YYYY-NNNNN) or GHSA-like ID
_VULN_ID_RE = re.compile(r"(CVE-\d{4}-\d+|GHSA-[\w-]+)")


def _is_table_row(line: str) -> bool:
    """Return True if *line* is a Trivy box-drawing table data row.

    Only ``│``-prefixed lines are data rows.  Border edges (``┌``, ``├``,
    ``└``) and the ``┘``/``┐``/``┤`` lines at the right edge are NOT data
    rows — they delimit table sections and are used to flush accumulated
    multi-line cells.
    """
    return line.startswith("│")


def _is_border_or_separator(line: str) -> bool:
    """Return True if *line* is a table border or row separator."""
    return bool(line) and line[0] in "┌├└─"


def _split_table_cells(line: str) -> list[str]:
    """Split a box-drawing table row into cell values.

    Trivy uses ``│`` as the cell separator.  The leading and trailing ``│``
    are present on every data row.
    """
    # Strip leading/trailing │ then split by inner │
    stripped = line.strip()
    stripped = stripped.removeprefix("│")
    stripped = stripped.removesuffix("│")
    return [cell.strip() for cell in stripped.split("│")]


def parse_trivy_table(text: str) -> TrivyParseResult:
    """Parse Trivy ``--format table`` output into structured findings.

    Handles:
    - The summary header line (total/critical/high/medium/low counts).
    - The target line (image name + OS info).
    - Tabular vulnerability rows with box-drawing characters.
    - Multi-line library/vulnerability names (Trivy sometimes wraps long
      CVE IDs or package names across two rows within the same table cell).

    Args:
        text: Raw stdout from a Trivy table-format scan.

    Returns:
        A :class:`TrivyParseResult` with parsed findings, summary, and target.

    """
    findings: list[TrivyFinding] = []
    summary: TrivySummary | None = None
    target: str = ""

    lines = text.splitlines()

    # --- target line: first non-empty, non-separator line ---
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if all(c in "=┌├└─" for c in stripped):
            continue
        # Check if it looks like "name (os info)"
        m = _TARGET_RE.match(stripped)
        if m:
            target = m.group(1)
        break

    # --- summary line ---
    for line in lines:
        m = _SUMMARY_RE.search(line)
        if m:
            summary = TrivySummary(
                total=int(m.group(1)),
                critical=int(m.group(2)),
                high=int(m.group(3)),
                medium=int(m.group(4)),
                low=int(m.group(5)),
            )
            break

    # --- table rows ---
    # Accumulate multi-line cell groups.  Trivy table output has:
    #   header row │ Library │ Vulnerability │ Severity │ Installed │ Fixed │
    #   separator  ├─────────────────────────────────────────────────────────┤
    #   data row   │ lib1    │ CVE-...       │ HIGH     │ 1.2.3     │ 1.2.4 │
    #   continuation (for multi-line entries):
    #              │         │               │          │           │(debian│
    #   separator  ├─────────────────────────────────────────────────────────┤
    # ...
    # The parser collects row cells and flushes when a row has a CVE in
    # column 2 (or when a separator/top/bottom border is seen).

    current_row_cells: list[str] = []

    for line in lines:
        stripped = line.strip()

        # Flush accumulated row when reaching a non-data-row line
        # (border, separator, blank line, or summary text).
        if not _is_table_row(stripped):
            if current_row_cells and len(current_row_cells) >= 5:
                _try_add_finding(current_row_cells, findings)
            current_row_cells = []
            continue

        cells = _split_table_cells(stripped)

        # Skip header rows (first row says "Library", "Vulnerability", etc.)
        if cells and cells[0].lower() in ("library", ""):
            current_row_cells = []
            continue

        # Empty cells — flush and skip.
        if not cells or all(c == "" for c in cells):
            current_row_cells = []
            continue

        # Does this row have a CVE/ID in column 2 (vulnerability column)?
        vuln_match = _VULN_ID_RE.search(cells[1]) if len(cells) >= 3 else None
        if vuln_match:
            # Flush any previous accumulated row
            if current_row_cells and len(current_row_cells) >= 5:
                _try_add_finding(current_row_cells, findings)
            current_row_cells = cells
        elif current_row_cells:
            # Continuation line — merge into the current row
            merged = list(current_row_cells)
            for i, cell in enumerate(cells):
                if i < len(merged) and cell:
                    # Append with space if there's existing content
                    if merged[i]:
                        merged[i] = f"{merged[i]} {cell}"
                    else:
                        merged[i] = cell
            current_row_cells = merged
        else:
            # Orphan continuation row — try to parse anyway
            if len(cells) >= 5:
                _try_add_finding(cells, findings)

    # Flush the last accumulated row
    if current_row_cells and len(current_row_cells) >= 5:
        _try_add_finding(current_row_cells, findings)

    return TrivyParseResult(
        findings=findings,
        summary=summary,
        target=target,
        raw_output=text,
    )


def _try_add_finding(cells: list[str], findings: list[TrivyFinding]) -> None:
    """Attempt to construct a :class:`TrivyFinding` from table cells.

    Requires at least 5 cells: [library, vuln_id, severity, installed, fixed].
    The vulnerability ID must match CVE/GHSA pattern.
    """
    if len(cells) < 5:
        return

    vuln_match = _VULN_ID_RE.search(cells[1])
    if not vuln_match:
        return

    # Extract severity — may include " (debian)" or similar suffix
    severity = cells[2].split()[0] if cells[2] else ""

    findings.append(
        TrivyFinding(
            library=cells[0],
            vulnerability_id=vuln_match.group(0),
            severity=severity,
            installed_version=cells[3],
            fixed_version=cells[4],
        )
    )


def format_findings_summary(result: TrivyParseResult) -> str:
    """Format a :class:`TrivyParseResult` as human-readable Markdown.

    Groups findings by severity and lists remediation information
    (fixed version) alongside each CVE.
    """
    if not result.findings and not result.summary:
        return "No vulnerability findings parsed from Trivy output."

    lines: list[str] = []

    if result.target:
        lines.append(f"### Target: {result.target}")

    if result.summary:
        s = result.summary
        lines.append(
            f"**Total vulnerabilities: {s.total}** "
            f"(CRITICAL: {s.critical}, HIGH: {s.high}, "
            f"MEDIUM: {s.medium}, LOW: {s.low})"
        )
        lines.append("")

    if not result.findings:
        lines.append("No individual vulnerability entries could be parsed.")
        return "\n".join(lines)

    # Group by severity (highest first)
    severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    sorted_findings = sorted(
        result.findings,
        key=lambda f: severity_order.get(f.severity.upper(), 99),
    )

    # Summary table
    lines.append("| CVE | Package | Severity | Installed | Fixed |")
    lines.append("|-----|---------|----------|-----------|-------|")
    for f in sorted_findings:
        fix = f.fixed_version or "_not yet_"
        lines.append(
            f"| {f.vulnerability_id} | {f.library} | {f.severity} "
            f"| {f.installed_version} | {fix} |"
        )

    return "\n".join(lines)
