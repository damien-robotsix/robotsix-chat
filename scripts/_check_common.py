"""Shared scaffolding for the CI consistency-check scripts.

The five ``scripts/check_*.py`` audit gates share an identical skeleton:
parse a canonical set of string constants from a Python source file,
scan the HTML/JS browser UI for bare string literals, and report any
value that is missing from (or unrecognised in) the UI.  Everything
except the regexes, file paths, and label strings lives here.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path


def parse_str_enum(
    models_path: Path,
    class_header_re: re.Pattern[str],
    value_re: re.Pattern[str],
) -> dict[str, str]:
    """Return {constant_name: value} for every member of a ``StrEnum`` class body.

    ``class_header_re`` matches the line where the enum class begins;
    ``value_re`` matches ``NAME = "value"`` lines inside that class body.
    """
    lines = models_path.read_text(encoding="utf-8").splitlines()
    in_class = False
    canonical: dict[str, str] = {}
    for line in lines:
        if class_header_re.match(line.rstrip()):
            in_class = True
            continue
        if in_class:
            # Class body ended — dedented or next class.
            if line and not line[0].isspace():
                break
            m = value_re.match(line.rstrip())
            if m:
                canonical[m.group(1)] = m.group("value")
    return canonical


def parse_constant_sets(
    py_path: Path,
    const_re: re.Pattern[str],
) -> dict[str, str]:
    """Return {constant_name: value} for every ``NAME = "value"`` assignment.

    Each line of the file is stripped and matched against ``const_re``; the
    constant name is the text before the first ``=`` on the matched line.
    """
    canonical: dict[str, str] = {}
    for line in py_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        m = const_re.match(stripped)
        if m:
            canonical[stripped.split("=")[0].strip()] = m.group("value")
    return canonical


def iter_matching_strings(
    path: Path,
    *regexes: re.Pattern[str],
) -> list[str]:
    """Yield every regex capture found in a file, stripping HTML comments.

    HTML comments (``<!-- ... -->``) are removed first so they are never
    scanned, matching the behaviour of the original JS-scanning helpers.
    Each regex is applied to the resulting text and the first capturing
    group of every match is collected.
    """
    text = path.read_text(encoding="utf-8")
    # Strip HTML comments so they aren't scanned.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    found: list[str] = []
    for regex in regexes:
        for m in regex.finditer(text):
            found.append(m.group(1))
    return found


def run_consistency_checks(
    *,
    canonical_values: set[str],
    scanned_values: set[str],
    missing_label: str | None,
    unrecognised_label: str | None,
    report_names_for_value: Callable[[str], str] | None = None,
) -> bool:
    """Print consistency violations between canonical and scanned values.

    When ``missing_label`` is set, reports canonical values absent from
    ``scanned_values``; when ``unrecognised_label`` is set, reports
    scanned values absent from ``canonical_values``.  Pass ``None`` to
    skip a direction.  ``report_names_for_value`` (if given) maps a
    canonical value to the Python constant name(s) shown on the
    ``missing`` line.  Returns True if any violation was printed.
    """
    violations = False

    if missing_label is not None:
        missing = canonical_values - scanned_values
        if missing:
            violations = True
            print(missing_label, file=sys.stderr)
            for val in sorted(missing):
                if report_names_for_value is not None:
                    print(
                        f"  {val}  (Python constant: {report_names_for_value(val)})",
                        file=sys.stderr,
                    )
                else:
                    print(f"  {val}", file=sys.stderr)
            print(file=sys.stderr)

    if unrecognised_label is not None:
        unrecognised = scanned_values - canonical_values
        if unrecognised:
            violations = True
            print(unrecognised_label, file=sys.stderr)
            for val in sorted(unrecognised):
                print(f"  {val}", file=sys.stderr)
            print(file=sys.stderr)

    return violations
