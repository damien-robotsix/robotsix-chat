"""Tests for src/robotsix_chat/common/unified_diff.py."""

import pytest

from robotsix_chat.common.unified_diff import apply_patch


class TestApplyPatch:
    """Tests for the apply_patch function."""

    # ------------------------------------------------------------------
    # Basic single-hunk patches
    # ------------------------------------------------------------------

    def test_simple_addition(self) -> None:
        """A single hunk that adds a line."""
        original = "line1\nline2\nline3\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            " line2\n"
            "+inserted\n"
            " line3\n"
        )
        result = apply_patch(original, patch)
        assert result == "line1\nline2\ninserted\nline3\n"

    def test_simple_removal(self) -> None:
        """A single hunk that removes a line."""
        original = "line1\nline2\nline3\n"
        patch = "--- a/file\n+++ b/file\n@@ -1,3 +1,2 @@\n line1\n-line2\n line3\n"
        result = apply_patch(original, patch)
        assert result == "line1\nline3\n"

    def test_simple_modification(self) -> None:
        """A single hunk that replaces a line."""
        original = "line1\nline2\nline3\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -1,3 +1,3 @@\n"
            " line1\n"
            "-line2\n"
            "+modified\n"
            " line3\n"
        )
        result = apply_patch(original, patch)
        assert result == "line1\nmodified\nline3\n"

    def test_add_and_remove(self) -> None:
        """A hunk that both adds and removes lines."""
        original = "a\nb\nc\nd\n"
        patch = "--- a/file\n+++ b/file\n@@ -1,4 +1,4 @@\n a\n+x\n b\n-c\n d\n"
        result = apply_patch(original, patch)
        assert result == "a\nx\nb\nd\n"

    # ------------------------------------------------------------------
    # Multiple hunks
    # ------------------------------------------------------------------

    def test_multiple_hunks(self) -> None:
        """Two hunks that both apply cleanly."""
        original = "line1\nline2\nline3\nline4\nline5\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            "+inserted\n"
            " line2\n"
            "@@ -4,2 +5,1 @@\n"
            "-line4\n"
            " line5\n"
        )
        result = apply_patch(original, patch)
        assert result == "line1\ninserted\nline2\nline3\nline5\n"

    def test_multiple_hunks_offset_tracking(self) -> None:
        """Second hunk applied at correct offset after first hunk changes line count."""
        original = "a\nb\nc\nd\ne\nf\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -1,2 +1,3 @@\n"
            " a\n"
            "+x\n"
            " b\n"
            "@@ -4,2 +5,1 @@\n"
            "-d\n"
            " e\n"
        )
        result = apply_patch(original, patch)
        # First hunk inserts 'x' after 'a' → a,x,b,c,d,e,f
        # cumulative_offset = +1, so second hunk at old_start=4 → result index 4+1-1=4
        # removes 'd' → a,x,b,c,e,f
        assert result == "a\nx\nb\nc\ne\nf\n"

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------

    def test_empty_patch(self) -> None:
        """An empty patch returns the original text unchanged."""
        original = "hello\nworld\n"
        result = apply_patch(original, "")
        assert result == original

    def test_patch_with_only_headers(self) -> None:
        """A patch containing only file headers is a no-op."""
        original = "hello\n"
        patch = "--- a/file\n+++ b/file\n"
        result = apply_patch(original, patch)
        assert result == original

    def test_all_context(self) -> None:
        """A hunk with only context lines (no changes) is a no-op."""
        original = "line1\nline2\n"
        patch = "--- a/file\n+++ b/file\n@@ -1,2 +1,2 @@\n line1\n line2\n"
        result = apply_patch(original, patch)
        assert result == original

    def test_empty_original(self) -> None:
        """Patching an empty file with additions."""
        original = ""
        patch = "--- a/file\n+++ b/file\n@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        result = apply_patch(original, patch)
        assert result == "line1\nline2\n"

    def test_no_newline_at_eof_marker(self) -> None:
        r"""The '\ No newline at end of file' marker is ignored."""
        original = "line1\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -1,1 +1,1 @@\n"
            "-line1\n"
            "+modified\n"
            "\\ No newline at end of file\n"
        )
        result = apply_patch(original, patch)
        assert result == "modified\n"

    def test_empty_context_line(self) -> None:
        """An empty context line (bare newline in patch body)."""
        original = "a\n\nb\n"
        patch = "--- a/file\n+++ b/file\n@@ -1,3 +1,4 @@\n a\n\n+inserted\n b\n"
        result = apply_patch(original, patch)
        assert result == "a\n\ninserted\nb\n"

    def test_hunk_header_no_count(self) -> None:
        """Hunk header with omitted count (e.g. '@@ -5 +5 @@')."""
        original = "a\nb\nc\nd\ne\n"
        patch = "--- a/file\n+++ b/file\n@@ -2 +2,2 @@\n-b\n+x\n+y\n"
        result = apply_patch(original, patch)
        assert result == "a\nx\ny\nc\nd\ne\n"

    # ------------------------------------------------------------------
    # Error cases
    # ------------------------------------------------------------------

    def test_context_mismatch(self) -> None:
        """Context line doesn't match the original content at that position."""
        original = "line1\nline2\nline3\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -1,2 +1,3 @@\n"
            " line1\n"
            "+inserted\n"
            " WRONG_CONTEXT\n"
        )
        with pytest.raises(ValueError, match="context mismatch"):
            apply_patch(original, patch)

    def test_removal_mismatch(self) -> None:
        """Removal line doesn't match the original content."""
        original = "line1\nline2\nline3\n"
        patch = (
            "--- a/file\n+++ b/file\n@@ -1,3 +1,2 @@\n line1\n-WRONG_REMOVAL\n line3\n"
        )
        with pytest.raises(ValueError, match="removal mismatch"):
            apply_patch(original, patch)

    def test_context_exceeds_file_length(self) -> None:
        """Context line position is beyond the file length."""
        original = "line1\n"
        patch = "--- a/file\n+++ b/file\n@@ -1,100 +1,101 @@\n line1\n line2\n"
        with pytest.raises(ValueError, match="exceeds file length"):
            apply_patch(original, patch)

    def test_removal_exceeds_file_length(self) -> None:
        """Removal position is beyond the file length."""
        original = "line1\n"
        patch = "--- a/file\n+++ b/file\n@@ -100,1 +99,0 @@\n-line100\n"
        with pytest.raises(ValueError, match="exceeds file length"):
            apply_patch(original, patch)

    # ------------------------------------------------------------------
    # Realistic git-diff scenarios
    # ------------------------------------------------------------------

    def test_git_diff_style_patch(self) -> None:
        """A realistic patch similar to what git diff would produce."""
        original = "def foo():\n    x = 1\n    y = 2\n    return x + y\n"
        patch = (
            "--- a/foo.py\n"
            "+++ b/foo.py\n"
            "@@ -1,4 +1,5 @@\n"
            " def foo():\n"
            "     x = 1\n"
            "+    z = 3\n"
            "     y = 2\n"
            "     return x + y\n"
        )
        result = apply_patch(original, patch)
        expected = "def foo():\n    x = 1\n    z = 3\n    y = 2\n    return x + y\n"
        assert result == expected

    def test_append_at_end(self) -> None:
        """Adding lines at the end of a file.

        A hunk positioned at old_start=3 (one past the last line, since
        the file has 2 lines) inserts after the last line.
        """
        original = "line1\nline2\n"
        patch = "--- a/file\n+++ b/file\n@@ -3,0 +3,1 @@\n+line3\n"
        result = apply_patch(original, patch)
        assert result == "line1\nline2\nline3\n"

    def test_preserves_trailing_newline(self) -> None:
        """The function preserves trailing newlines via keepends=True splitting."""
        original = "hello\n"
        patch = "--- a/file\n+++ b/file\n@@ -1,1 +1,1 @@\n-hello\n+world\n"
        result = apply_patch(original, patch)
        assert result == "world\n"

    def test_no_trailing_newline_preserved(self) -> None:
        """+ lines in the patch dictate the result's line endings, not the original."""
        original = "hello"
        patch = "--- a/file\n+++ b/file\n@@ -1,1 +1,1 @@\n-hello\n+world\n"
        result = apply_patch(original, patch)
        assert result == "world\n"

    def test_unknown_line_prefix_skipped(self) -> None:
        """Lines with unknown prefixes are silently skipped."""
        original = "a\nb\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -1,2 +1,3 @@\n"
            " a\n"
            "?unknown prefix\n"
            "+inserted\n"
            " b\n"
        )
        result = apply_patch(original, patch)
        assert result == "a\ninserted\nb\n"

    def test_addition_at_beginning(self) -> None:
        """Add a line at the very beginning of the file."""
        original = "line1\nline2\n"
        patch = (
            "--- a/file\n"
            "+++ b/file\n"
            "@@ -0,0 +1,1 @@\n"
            "+line0\n"
            "@@ -1,2 +2,2 @@\n"
            " line1\n"
            " line2\n"
        )
        result = apply_patch(original, patch)
        assert result == "line0\nline1\nline2\n"

    def test_remove_all_lines(self) -> None:
        """Remove all lines from a file."""
        original = "line1\nline2\n"
        patch = "--- a/file\n+++ b/file\n@@ -1,2 +0,0 @@\n-line1\n-line2\n"
        result = apply_patch(original, patch)
        assert result == ""
