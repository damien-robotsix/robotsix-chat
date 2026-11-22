"""Unit tests for the unified-diff ``apply_patch`` function.

Exercises ``robotsix_chat.common.unified_diff.apply_patch``, the canonical
diff-application entry point.  The same matrix is covered through the
``DirectRepoClient.apply_patch`` static delegate (identical implementation)
in ``test_direct_repo_patching.py``; the guarded tool integration
(``patch_direct_repo_file``, ``apply_patch_to_file``) also lives there.
"""

from __future__ import annotations

import pytest

from robotsix_chat.common.unified_diff import apply_patch

# ---------------------------------------------------------------------------
# apply_patch — unified diff patch application unit tests
# ---------------------------------------------------------------------------


def test_apply_patch_insert_at_beginning_of_empty_file() -> None:
    """@@ -0,0 +1,N @@ hunk against an empty file inserts at position 0."""
    patch = "@@ -0,0 +1,2 @@\n+line 1\n+line 2\n"
    result = apply_patch("", patch)
    assert result == "line 1\nline 2\n"


def test_apply_patch_insert_at_beginning_of_non_empty_file() -> None:
    """@@ -0,0 +1,N @@ hunk against a non-empty file inserts before line 1."""
    original = "existing line\n"
    patch = "@@ -0,0 +1,1 @@\n+new first line\n"
    result = apply_patch(original, patch)
    assert result == "new first line\nexisting line\n"


def test_apply_patch_normal_context_hunk() -> None:
    """A standard hunk with context, removal, and addition."""
    original = "line 1\nline 2\nline 3\nline 4\nline 5\n"
    patch = "@@ -2,3 +2,4 @@\n line 2\n-line 3\n+new line 3a\n+new line 3b\n line 4\n"
    result = apply_patch(original, patch)
    assert result == "line 1\nline 2\nnew line 3a\nnew line 3b\nline 4\nline 5\n"


def test_apply_patch_multiple_hunks() -> None:
    """Two hunks applied in order with cumulative offset tracking."""
    original = "a\nb\nc\nd\ne\nf\n"
    patch = "@@ -2,2 +2,3 @@\n b\n-c\n+cc\n+ccc\n d\n@@ -5,1 +6,0 @@\n-e\n"
    result = apply_patch(original, patch)
    assert result == "a\nb\ncc\nccc\nd\nf\n"


def test_apply_patch_removal_only() -> None:
    """Hunk that only removes lines."""
    original = "keep\nremove me\nalso keep\n"
    patch = "@@ -2,1 +1,0 @@\n-remove me\n"
    result = apply_patch(original, patch)
    assert result == "keep\nalso keep\n"


def test_apply_patch_addition_only() -> None:
    """Hunk that only adds lines (context-only with additions)."""
    original = "header\nfooter\n"
    patch = "@@ -2,1 +2,3 @@\n footer\n+middle 1\n+middle 2\n"
    result = apply_patch(original, patch)
    assert result == "header\nfooter\nmiddle 1\nmiddle 2\n"


def test_apply_patch_context_mismatch_raises_value_error() -> None:
    """Context line does not match original → ValueError."""
    original = "line 1\nline 2\n"
    patch = "@@ -1,1 +1,1 @@\n wrong\n+replacement\n"
    with pytest.raises(ValueError, match="context mismatch"):
        apply_patch(original, patch)


def test_apply_patch_no_newline_at_eof_marker() -> None:
    r"""The \\ No newline at end of file marker is skipped."""
    original = "line 1\nline 2"
    patch = (
        "@@ -1,2 +1,3 @@\n line 1\n-line 2\n+line 2\n+line 3\n"
        "\\ No newline at end of file\n"
    )
    result = apply_patch(original, patch)
    assert result == "line 1\nline 2\nline 3\n"


def test_apply_patch_insert_at_zero_with_prior_offset() -> None:
    """A hunk at @@ -0,0 after a prior hunk that shifted lines.

    The cumulative offset from the first hunk should not cause a negative
    orig_pos when the second hunk starts at old_start=0.
    """
    original = "a\nb\nc\n"
    patch = "@@ -3,1 +3,2 @@\n c\n+d\n@@ -0,0 +1,1 @@\n+preface\n"
    result = apply_patch(original, patch)
    assert result == "preface\na\nb\nc\nd\n"


def test_apply_patch_empty_hunk_at_zero() -> None:
    """@@ -0,0 +0,0 @@ (empty add at beginning) is a no-op."""
    original = "a\nb\n"
    patch = "@@ -0,0 +0,0 @@\n"
    result = apply_patch(original, patch)
    assert result == "a\nb\n"


def test_apply_patch_preserves_trailing_newline() -> None:
    """Files ending with newline keep it after patching."""
    original = "line 1\n"
    patch = "@@ -0,0 +1,1 @@\n+line 0\n"
    result = apply_patch(original, patch)
    assert result == "line 0\nline 1\n"


def test_apply_patch_preserves_no_trailing_newline() -> None:
    """Files without trailing newline keep that property after patching."""
    original = "only line"
    patch = "@@ -0,0 +1,1 @@\n+prefix\n"
    result = apply_patch(original, patch)
    # When original has no trailing newline, splitlines(keepends=True)
    # returns ["only line"] (no \n). The result should also lack a
    # trailing newline.
    assert result == "prefix\nonly line"
