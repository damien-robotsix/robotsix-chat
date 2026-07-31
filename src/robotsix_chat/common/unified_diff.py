"""Pure unified-diff applicator — no HTTP, no I/O.

Applies a unified diff (as produced by ``diff -u`` or ``git diff``) to
original text content.  Supports the standard format::

    --- a/path
    +++ b/path
    @@ -start,count +start,count @@
     context
    -removed
    +added
     context

Multiple hunks (multiple ``@@`` headers) are supported.
"""

from __future__ import annotations

import re


def apply_patch(original: str, patch_text: str) -> str:
    """Apply a unified diff to *original* and return the patched content.

    Args:
        original: The original file content.
        patch_text: The unified diff to apply.

    Returns:
        The patched file content.

    Raises:
        ValueError: If a hunk cannot be applied (context mismatch).

    """
    orig_lines = original.splitlines(keepends=True)
    patch_lines = patch_text.splitlines(keepends=True)

    result = list(orig_lines)
    cumulative_offset = 0  # net lines added (positive) or removed (negative)

    idx = 0
    while idx < len(patch_lines):
        line = patch_lines[idx]

        # Skip file headers (--- / +++)
        if line.startswith("--- ") or line.startswith("+++ "):
            idx += 1
            continue

        # Parse hunk header: @@ -old_start,old_count +new_start,new_count @@
        m = re.match(
            r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@",
            line,
        )
        if not m:
            idx += 1
            continue

        old_start = int(m.group(1))
        idx += 1

        # Collect hunk body lines
        hunk_lines: list[str] = []
        while idx < len(patch_lines) and not patch_lines[idx].startswith("@@"):
            hunk_lines.append(patch_lines[idx])
            idx += 1

        # Apply the hunk
        # 0-indexed position in *result*
        orig_pos = max(0, old_start - 1 + cumulative_offset)
        hunk_offset_add = 0
        hunk_offset_del = 0
        hj = 0
        while hj < len(hunk_lines):
            hl = hunk_lines[hj]
            if hl.startswith(" "):  # context line
                if orig_pos >= len(result):
                    raise ValueError(
                        f"Hunk at line {old_start}: context line {hj + 1} "
                        f"exceeds file length ({len(result)} lines)."
                    )
                actual = result[orig_pos].rstrip("\n")
                expected = hl[1:].rstrip("\n")
                if actual != expected:
                    raise ValueError(
                        f"Hunk at line {old_start}: context mismatch at "
                        f"file line {orig_pos + 1}. "
                        f"Expected: {expected!r}, got: {actual!r}"
                    )
                orig_pos += 1
                hj += 1
            elif hl.startswith("-"):  # remove line
                if orig_pos >= len(result):
                    raise ValueError(
                        f"Hunk at line {old_start}: removal at line {hj + 1} "
                        f"exceeds file length ({len(result)} lines)."
                    )
                actual = result[orig_pos].rstrip("\n")
                expected = hl[1:].rstrip("\n")
                if actual != expected:
                    raise ValueError(
                        f"Hunk at line {old_start}: removal mismatch at "
                        f"file line {orig_pos + 1}. "
                        f"Expected to remove: {expected!r}, got: {actual!r}"
                    )
                del result[orig_pos]
                hunk_offset_del += 1
                # Don't advance orig_pos — line was removed
                hj += 1
            elif hl.startswith("+"):  # add line
                result.insert(orig_pos, hl[1:])
                orig_pos += 1
                hunk_offset_add += 1
                hj += 1
            elif hl == "\n" or hl == "":
                # Empty context line (no leading space)
                if orig_pos < len(result):
                    actual = result[orig_pos]
                    if actual not in ("\n", ""):
                        raise ValueError(
                            f"Hunk at line {old_start}: expected empty "
                            f"context line, got: {actual!r}"
                        )
                orig_pos += 1
                hj += 1
            elif hl.startswith("\\"):  # "No newline at end of file" marker
                hj += 1
            else:
                # Unknown line — skip
                hj += 1

        cumulative_offset += hunk_offset_add - hunk_offset_del

    return "".join(result)
