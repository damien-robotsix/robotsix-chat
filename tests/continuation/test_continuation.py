"""Tests for the post-restart continuation store and tools."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from robotsix_chat.config.models import ContinuationSettings
from robotsix_chat.continuation import build_continuation_tools, load_continuation_skill
from robotsix_chat.continuation.store import ContinuationStore

# ---------------------------------------------------------------------------
# ContinuationStore
# ---------------------------------------------------------------------------


class TestContinuationStore:
    """Unit tests for the ContinuationStore."""

    def test_default_empty(self) -> None:
        """A fresh store has no pending continuation and zero consecutive count."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            assert store.consecutive_count == 0
            sid, prompt = store.consume_pending()
            assert sid is None
            assert prompt is None

    def test_schedule_and_consume(self) -> None:
        """Scheduling a continuation makes it available for consumption."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            result = store.schedule("sess-1", "resume: finish deploying")
            assert "Continuation armed" in result

            sid, prompt = store.consume_pending()
            assert sid == "sess-1"
            assert prompt == "resume: finish deploying"

    def test_one_shot(self) -> None:
        """A continuation is consumed on first call; second call returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            store.schedule("sess-1", "do work")

            sid, prompt = store.consume_pending()
            assert sid == "sess-1"

            # Second consume returns nothing.
            sid2, prompt2 = store.consume_pending()
            assert sid2 is None
            assert prompt2 is None

    def test_consecutive_count_increments(self) -> None:
        """Each consume_pending increments the consecutive counter."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path, max_consecutive=5)
            store.schedule("sess-1", "do work")
            assert store.consecutive_count == 0

            store.consume_pending()
            assert store.consecutive_count == 1

            # Schedule and consume again.
            store.schedule("sess-2", "more work")
            store.consume_pending()
            assert store.consecutive_count == 2

    def test_guardrail_blocks_after_limit(self) -> None:
        """When consecutive_count >= max_consecutive, consume_pending blocks."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path, max_consecutive=3)
            # Simulate 3 previous consecutive continuations.
            for i in range(3):
                store.schedule(f"sess-{i}", "work")
                store.consume_pending()
            assert store.consecutive_count == 3

            # Schedule one more — should be blocked.
            store.schedule("sess-4", "blocked work")
            sid, prompt = store.consume_pending()
            assert sid is None
            assert prompt is None

    def test_reset_consecutive(self) -> None:
        """reset_consecutive() zeros the counter."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            store.schedule("sess-1", "work")
            store.consume_pending()
            assert store.consecutive_count == 1

            store.reset_consecutive()
            assert store.consecutive_count == 0

    def test_cancel(self) -> None:
        """Cancelling a pending continuation clears it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            store.schedule("sess-1", "work")
            result = store.cancel()
            assert "cancelled" in result.lower()

            sid, prompt = store.consume_pending()
            assert sid is None

    def test_cancel_when_none_pending(self) -> None:
        """Cancelling with nothing pending returns a clear message."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            result = store.cancel()
            assert "No pending" in result

    def test_pending_info(self) -> None:
        """pending_info() returns a dict summarising the store state."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            info = store.pending_info()
            assert info["pending"] is False

            store.schedule("sess-1", "resume work")
            info = store.pending_info()
            assert info["pending"] is True
            assert info["session_id"] == "sess-1"
            assert "resume work" in info["prompt_preview"]

    def test_survives_persistence_roundtrip(self) -> None:
        """State written to disk is correctly read back by a new store instance."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store1 = ContinuationStore(path=path)
            store1.schedule("sess-1", "resume work")
            store1.consume_pending()
            assert store1.consecutive_count == 1

            # New instance reads the same file.
            store2 = ContinuationStore(path=path)
            assert store2.consecutive_count == 1
            # The pending was consumed, so nothing pending.
            sid, _ = store2.consume_pending()
            assert sid is None

    def test_tolerates_corrupt_file(self) -> None:
        """A corrupt JSON file is tolerated — store starts empty."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            path.write_text("not json {{{")
            store = ContinuationStore(path=path)
            assert store.consecutive_count == 0
            sid, _ = store.consume_pending()
            assert sid is None

    def test_overwrite_pending(self) -> None:
        """Scheduling a second continuation overwrites the first."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            store.schedule("sess-1", "first work")
            store.schedule("sess-2", "second work")

            sid, prompt = store.consume_pending()
            assert sid == "sess-2"
            assert prompt == "second work"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class TestContinuationTools:
    """Tests for build_continuation_tools and the tool functions."""

    def test_disabled_returns_empty(self) -> None:
        """When settings.enabled is False, no tools are returned."""
        settings = ContinuationSettings(enabled=False)
        tools = build_continuation_tools(settings)
        assert tools == []

    def test_enabled_returns_three_tools(self) -> None:
        """When enabled, three tools are returned."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            settings = ContinuationSettings(
                enabled=True,
                store_path=str(path),
                max_consecutive=3,
            )
            tools = build_continuation_tools(settings)
            assert len(tools) == 3
            names = [t.__name__ for t in tools]
            assert "schedule_continuation" in names
            assert "cancel_continuation" in names
            assert "get_continuation_status" in names

    @pytest.mark.asyncio
    async def test_schedule_tool(self) -> None:
        """The schedule_continuation tool arms a continuation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            settings = ContinuationSettings(
                enabled=True,
                store_path=str(path),
                max_consecutive=3,
            )
            tools = build_continuation_tools(settings, continuation_store=store)
            schedule_tool = tools[0]
            result = await schedule_tool("sess-1", "resume work")
            assert "armed" in result.lower()
            assert store.pending_info()["pending"] is True

    @pytest.mark.asyncio
    async def test_cancel_tool(self) -> None:
        """The cancel_continuation tool cancels a pending continuation."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            store.schedule("sess-1", "work")
            settings = ContinuationSettings(
                enabled=True,
                store_path=str(path),
                max_consecutive=3,
            )
            tools = build_continuation_tools(settings, continuation_store=store)
            # cancel_continuation is the second tool.
            cancel_tool = tools[1]
            result = await cancel_tool()
            assert "cancelled" in result.lower()
            assert store.pending_info()["pending"] is False

    @pytest.mark.asyncio
    async def test_status_tool(self) -> None:
        """The get_continuation_status tool returns JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuation.json"
            store = ContinuationStore(path=path)
            settings = ContinuationSettings(
                enabled=True,
                store_path=str(path),
                max_consecutive=3,
            )
            tools = build_continuation_tools(settings, continuation_store=store)
            status_tool = tools[2]
            result = await status_tool()
            data = json.loads(result)
            assert data["pending"] is False


# ---------------------------------------------------------------------------
# Skill loader
# ---------------------------------------------------------------------------


class TestContinuationSkill:
    """Tests for load_continuation_skill."""

    def test_skill_returns_non_empty(self) -> None:
        """The skill file exists and returns markdown content."""
        skill = load_continuation_skill()
        assert len(skill) > 0
        assert "schedule_continuation" in skill
        assert "Guardrails" in skill
