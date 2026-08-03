"""Tests for autonomous self-refinement persistence and lifecycle."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from robotsix_chat.autonomous.refinement import (
    DefinitionRefinementState,
    RefinementEntry,
    RefinementStore,
    _build_refinement_prompt,
    _summarise_history,
)

# Module-level constants (private).
_MAX_ACCEPTED_REFINEMENTS = 10
_MAX_ADDENDUM_CHARS = 2_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(
    *,
    entry_id: str | None = None,
    status: str = "pending",
    proposed_addendum: str = "lesson learned",
    timestamp: float | None = None,
) -> RefinementEntry:
    """Create a RefinementEntry with defaults for testing."""
    import time

    return RefinementEntry(
        id=entry_id or uuid.uuid4().hex,
        timestamp=timestamp or time.time(),
        base_prompt="base prompt",
        previous_addendum="",
        proposed_addendum=proposed_addendum,
        feedback_summary="summary",
        session_id="session-1",
        status=status,
    )


def _sample_store(tmp_path: Path) -> RefinementStore:
    """Return a RefinementStore backed by a temp file (no agent factory)."""
    persist = tmp_path / "refinements.json"
    return RefinementStore(str(persist))


# ---------------------------------------------------------------------------
# _load / _save round-trip
# ---------------------------------------------------------------------------


class TestPersistenceRoundTrip:
    """Round-trip tests for _load / _save."""

    def test_load_empty_when_file_absent(self, tmp_path: Path) -> None:
        """_load returns an empty dict when the file does not exist."""
        store = _sample_store(tmp_path)
        assert store._load() == {}

    def test_save_then_load_preserves_state(self, tmp_path: Path) -> None:
        """State saved to disk is faithfully reloaded."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="accepted")
        state = DefinitionRefinementState(
            definition_name="def1",
            base_prompt="prompt",
            accepted_addendum="addendum",
            entries=[entry],
        )
        store._states["def1"] = state
        store._save()

        loaded = store._load()
        assert "def1" in loaded
        restored = loaded["def1"]
        assert restored.definition_name == "def1"
        assert restored.base_prompt == "prompt"
        assert restored.accepted_addendum == "addendum"
        assert len(restored.entries) == 1
        assert restored.entries[0].id == "e1"
        assert restored.entries[0].status == "accepted"

    def test_load_handles_corrupt_json(self, tmp_path: Path) -> None:
        """_load returns empty dict when the file contains invalid JSON."""
        persist = tmp_path / "refinements.json"
        persist.write_text("not json")
        store = RefinementStore(str(persist))
        assert store._load() == {}

    def test_load_skips_unparsable_entry(self, tmp_path: Path) -> None:
        """_load skips entries that fail to parse, keeping valid ones."""
        persist = tmp_path / "refinements.json"
        import time

        data = {
            "def1": {
                "definition_name": "def1",
                "base_prompt": "p",
                "accepted_addendum": "",
                "entries": [
                    {
                        "id": "ok",
                        "timestamp": time.time(),
                        "status": "pending",
                        "base_prompt": "p",
                        "previous_addendum": "",
                        "proposed_addendum": "x",
                        "feedback_summary": "s",
                        "session_id": "s1",
                    }
                ],
            },
            "def2": "not_a_dict",  # unparsable
        }
        persist.write_text(json.dumps(data))
        store = RefinementStore(str(persist))
        # def1 should load, def2 should be skipped.
        assert "def1" in store._states
        assert "def2" not in store._states

    def test_save_creates_parent_directory(self, tmp_path: Path) -> None:
        """_save creates parent directories as needed."""
        persist = tmp_path / "subdir" / "deep" / "refinements.json"
        store = RefinementStore(str(persist))
        store._states["def1"] = DefinitionRefinementState(definition_name="def1")
        store._save()
        assert persist.exists()
        loaded = store._load()
        assert "def1" in loaded


# ---------------------------------------------------------------------------
# get_state, get_entries, effective_prompt
# ---------------------------------------------------------------------------


class TestStateAccessors:
    """Tests for get_state, get_entries, and effective_prompt."""

    def test_get_state_creates_new_on_miss(self, tmp_path: Path) -> None:
        """get_state creates and persists a new DefinitionRefinementState on miss."""
        store = _sample_store(tmp_path)
        state = store.get_state("def1", base_prompt="prompt A")
        assert state.definition_name == "def1"
        assert state.base_prompt == "prompt A"
        assert state.entries == []
        assert state.accepted_addendum == ""

    def test_get_state_returns_existing(self, tmp_path: Path) -> None:
        """get_state returns the existing state when already loaded."""
        store = _sample_store(tmp_path)
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", base_prompt="prompt A"
        )
        state = store.get_state("def1")
        assert state.definition_name == "def1"
        assert state.base_prompt == "prompt A"

    def test_get_state_updates_changed_base_prompt(self, tmp_path: Path) -> None:
        """When base_prompt changes, the stored base is updated and addendum reset."""
        store = _sample_store(tmp_path)
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1",
            base_prompt="old prompt",
            accepted_addendum="old addendum",
        )
        state = store.get_state("def1", base_prompt="new prompt")
        assert state.base_prompt == "new prompt"
        assert state.accepted_addendum == ""  # reset

    def test_get_state_preserves_addendum_on_same_base(self, tmp_path: Path) -> None:
        """When base_prompt is unchanged, the addendum is left intact."""
        store = _sample_store(tmp_path)
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1",
            base_prompt="prompt",
            accepted_addendum="addendum",
        )
        state = store.get_state("def1", base_prompt="prompt")
        assert state.base_prompt == "prompt"
        assert state.accepted_addendum == "addendum"

    def test_get_entries_returns_empty_list_for_unknown(self, tmp_path: Path) -> None:
        """get_entries returns an empty list for unknown definitions."""
        store = _sample_store(tmp_path)
        assert store.get_entries("nonexistent") == []

    def test_get_entries_returns_all_entries(self, tmp_path: Path) -> None:
        """get_entries returns a copy of the entries list."""
        store = _sample_store(tmp_path)
        e1 = _make_entry(entry_id="e1")
        e2 = _make_entry(entry_id="e2")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[e1, e2]
        )
        entries = store.get_entries("def1")
        assert len(entries) == 2
        assert entries[0].id == "e1"
        assert entries[1].id == "e2"

    def test_effective_prompt_no_addendum(self, tmp_path: Path) -> None:
        """effective_prompt returns base when there is no addendum."""
        store = _sample_store(tmp_path)
        result = store.effective_prompt("def1", base_prompt="base prompt")
        assert result == "base prompt"

    def test_effective_prompt_with_addendum(self, tmp_path: Path) -> None:
        """effective_prompt returns base + addendum when one is accepted."""
        store = _sample_store(tmp_path)
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1",
            base_prompt="base prompt",
            accepted_addendum="lesson here",
        )
        result = store.effective_prompt("def1", base_prompt="base prompt")
        assert result == "base prompt\n\nlesson here"


# ---------------------------------------------------------------------------
# accept_refinement / reject_refinement
# ---------------------------------------------------------------------------


class TestAcceptRejectRefinement:
    """Tests for accept_refinement and reject_refinement."""

    def test_accept_pending_entry(self, tmp_path: Path) -> None:
        """accept_refinement sets a pending entry to accepted and updates addendum."""
        store = _sample_store(tmp_path)
        entry = _make_entry(
            entry_id="e1", status="pending", proposed_addendum="new lesson"
        )
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", base_prompt="prompt", entries=[entry]
        )
        assert store.accept_refinement("def1", "e1") is True
        assert entry.status == "accepted"
        assert store._states["def1"].accepted_addendum == "new lesson"

    def test_accept_unknown_definition(self, tmp_path: Path) -> None:
        """accept_refinement on unknown definition returns False."""
        store = _sample_store(tmp_path)
        assert store.accept_refinement("nonexistent", "e1") is False

    def test_accept_unknown_entry_id(self, tmp_path: Path) -> None:
        """accept_refinement with unknown entry id returns False."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="pending")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.accept_refinement("def1", "unknown-id") is False

    def test_accept_already_accepted_entry(self, tmp_path: Path) -> None:
        """accept_refinement on an already-accepted entry returns False."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="accepted", proposed_addendum="old")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1",
            accepted_addendum="old",
            entries=[entry],
        )
        assert store.accept_refinement("def1", "e1") is False

    def test_accept_already_rejected_entry(self, tmp_path: Path) -> None:
        """accept_refinement on an already-rejected entry returns False."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="rejected")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.accept_refinement("def1", "e1") is False

    def test_reject_pending_entry(self, tmp_path: Path) -> None:
        """reject_refinement sets a pending entry to rejected."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="pending")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.reject_refinement("def1", "e1") is True
        assert entry.status == "rejected"

    def test_reject_unknown_definition(self, tmp_path: Path) -> None:
        """reject_refinement on unknown definition returns False."""
        store = _sample_store(tmp_path)
        assert store.reject_refinement("nonexistent", "e1") is False

    def test_reject_unknown_entry_id(self, tmp_path: Path) -> None:
        """reject_refinement with unknown entry id returns False."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="pending")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.reject_refinement("def1", "unknown-id") is False

    def test_reject_already_accepted_entry(self, tmp_path: Path) -> None:
        """reject_refinement on an already-accepted entry returns False."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="accepted")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.reject_refinement("def1", "e1") is False

    def test_reject_already_rejected_entry(self, tmp_path: Path) -> None:
        """reject_refinement on an already-rejected entry returns False."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="rejected")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.reject_refinement("def1", "e1") is False

    def test_accept_idempotent_repeated_calls(self, tmp_path: Path) -> None:
        """Multiple accept calls: first succeeds, second fails."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="pending", proposed_addendum="lesson")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.accept_refinement("def1", "e1") is True
        assert store.accept_refinement("def1", "e1") is False

    def test_reject_idempotent_repeated_calls(self, tmp_path: Path) -> None:
        """Multiple reject calls: first succeeds, second fails."""
        store = _sample_store(tmp_path)
        entry = _make_entry(entry_id="e1", status="pending")
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", entries=[entry]
        )
        assert store.reject_refinement("def1", "e1") is True
        assert store.reject_refinement("def1", "e1") is False


# ---------------------------------------------------------------------------
# reset_refinements
# ---------------------------------------------------------------------------


class TestResetRefinements:
    """Tests for reset_refinements."""

    def test_reset_removes_state(self, tmp_path: Path) -> None:
        """reset_refinements removes the definition state entirely."""
        store = _sample_store(tmp_path)
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", base_prompt="prompt"
        )
        assert store.reset_refinements("def1") is True
        assert "def1" not in store._states

    def test_reset_unknown_definition(self, tmp_path: Path) -> None:
        """reset_refinements on unknown definition returns False."""
        store = _sample_store(tmp_path)
        assert store.reset_refinements("nonexistent") is False

    def test_reset_then_get_state_creates_new(self, tmp_path: Path) -> None:
        """After reset, get_state creates a fresh state."""
        store = _sample_store(tmp_path)
        store._states["def1"] = DefinitionRefinementState(
            definition_name="def1", base_prompt="prompt", accepted_addendum="old"
        )
        store.reset_refinements("def1")
        state = store.get_state("def1", base_prompt="new")
        assert state.accepted_addendum == ""
        assert state.entries == []

    def test_reset_persists(self, tmp_path: Path) -> None:
        """reset_refinements persists the removal to disk."""
        store = _sample_store(tmp_path)
        store._states["def1"] = DefinitionRefinementState(definition_name="def1")
        store._save()
        assert store.reset_refinements("def1") is True
        loaded = store._load()
        assert "def1" not in loaded


# ---------------------------------------------------------------------------
# _compact
# ---------------------------------------------------------------------------


class TestCompact:
    """Tests for the _compact method."""

    def test_no_compaction_when_under_limit(self, tmp_path: Path) -> None:
        """_compact is a no-op when entries and addendum are within bounds."""
        store = _sample_store(tmp_path)
        entries = [
            _make_entry(entry_id=f"e{i}", status="accepted")
            for i in range(_MAX_ACCEPTED_REFINEMENTS)
        ]
        state = DefinitionRefinementState(
            definition_name="def1",
            accepted_addendum="short",
            entries=entries,
        )
        store._compact(state)
        assert len(state.entries) == _MAX_ACCEPTED_REFINEMENTS

    def test_compaction_drops_oldest_accepted(self, tmp_path: Path) -> None:
        """When accepted entries exceed the max, oldest are dropped."""
        store = _sample_store(tmp_path)
        entries = [
            _make_entry(entry_id=f"e{i}", status="accepted")
            for i in range(_MAX_ACCEPTED_REFINEMENTS + 3)
        ]
        # Also add some non-accepted entries that should survive.
        pending = _make_entry(entry_id="pending-1", status="pending")
        rejected = _make_entry(entry_id="rejected-1", status="rejected")
        state = DefinitionRefinementState(
            definition_name="def1",
            accepted_addendum="ok",
            entries=[*entries, pending, rejected],
        )
        store._compact(state)
        # Only _MAX_ACCEPTED_REFINEMENTS accepted entries remain.
        accepted_remaining = [e for e in state.entries if e.status == "accepted"]
        assert len(accepted_remaining) == _MAX_ACCEPTED_REFINEMENTS
        # The oldest 3 were dropped; the newest _MAX_ACCEPTED_REFINEMENTS survive.
        kept_ids = {e.id for e in accepted_remaining}
        for i in range(3):
            assert f"e{i}" not in kept_ids
        for i in range(3, _MAX_ACCEPTED_REFINEMENTS + 3):
            assert f"e{i}" in kept_ids
        # Non-accepted entries survive.
        assert any(e.id == "pending-1" for e in state.entries)
        assert any(e.id == "rejected-1" for e in state.entries)

    def test_compaction_truncates_addendum(self, tmp_path: Path) -> None:
        """When the accepted addendum exceeds _MAX_ADDENDUM_CHARS, it is truncated."""
        store = _sample_store(tmp_path)
        long_addendum = "x" * (_MAX_ADDENDUM_CHARS + 500)
        state = DefinitionRefinementState(
            definition_name="def1",
            accepted_addendum=long_addendum,
            entries=[],
        )
        store._compact(state)
        assert len(state.accepted_addendum) < len(long_addendum)
        assert "[addendum truncated" in state.accepted_addendum

    def test_compaction_at_exact_addendum_limit(self, tmp_path: Path) -> None:
        """An addendum exactly at _MAX_ADDENDUM_CHARS is not truncated."""
        store = _sample_store(tmp_path)
        exact = "y" * _MAX_ADDENDUM_CHARS
        state = DefinitionRefinementState(
            definition_name="def1",
            accepted_addendum=exact,
            entries=[],
        )
        store._compact(state)
        assert state.accepted_addendum == exact
        assert "[addendum truncated" not in state.accepted_addendum


# ---------------------------------------------------------------------------
# propose_refinement
# ---------------------------------------------------------------------------


class TestProposeRefinement:
    """Tests for propose_refinement."""

    def test_agent_factory_none_returns_none(self, tmp_path: Path) -> None:
        """propose_refinement returns None when agent_factory is None."""
        store = _sample_store(tmp_path)
        import asyncio

        result = asyncio.run(
            store.propose_refinement(
                "def1",
                base_prompt="prompt",
                session_id="s1",
                conversation_history="history",
            )
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_with_mock_agent_returns_entry(self, tmp_path: Path) -> None:
        """propose_refinement with a mock agent returns a pending RefinementEntry."""
        agent = MagicMock()
        agent.stream = MagicMock()

        async def _fake_stream(*args, **kwargs):
            yield "new lesson"
            return

        agent.stream.return_value = _fake_stream()

        store = RefinementStore(
            str(tmp_path / "refinements.json"), agent_factory=lambda: agent
        )
        entry = await store.propose_refinement(
            "def1",
            base_prompt="base prompt",
            session_id="s1",
            conversation_history="some history",
        )
        assert entry is not None
        assert entry.status == "pending"
        assert entry.proposed_addendum == "new lesson"
        assert entry.base_prompt == "base prompt"
        assert entry.session_id == "s1"
        # Entry is persisted.
        loaded = store._load()
        assert len(loaded["def1"].entries) == 1
        assert loaded["def1"].entries[0].id == entry.id

    @pytest.mark.asyncio
    async def test_auto_accept(self, tmp_path: Path) -> None:
        """propose_refinement with auto_accept=True immediately accepts the entry."""
        agent = MagicMock()
        agent.stream = MagicMock()

        async def _fake_stream(*args, **kwargs):
            yield "auto lesson"
            return

        agent.stream.return_value = _fake_stream()

        store = RefinementStore(
            str(tmp_path / "refinements.json"), agent_factory=lambda: agent
        )
        entry = await store.propose_refinement(
            "def1",
            base_prompt="bp",
            session_id="s1",
            conversation_history="history",
            auto_accept=True,
        )
        assert entry is not None
        assert entry.status == "accepted"
        state = store.get_state("def1")
        assert state.accepted_addendum == "auto lesson"

    @pytest.mark.asyncio
    async def test_empty_proposed_addendum_returns_none(self, tmp_path: Path) -> None:
        """Empty/whitespace-only addendum → propose_refinement returns None."""
        agent = MagicMock()
        agent.stream = MagicMock()

        async def _empty_stream(*args, **kwargs):
            yield "   "
            return

        agent.stream.return_value = _empty_stream()

        store = RefinementStore(
            str(tmp_path / "refinements.json"), agent_factory=lambda: agent
        )
        entry = await store.propose_refinement(
            "def1",
            base_prompt="bp",
            session_id="s1",
            conversation_history="history",
        )
        assert entry is None

    @pytest.mark.asyncio
    async def test_llm_exception_returns_none(self, tmp_path: Path) -> None:
        """When the LLM stream raises, propose_refinement returns None."""
        agent = MagicMock()
        agent.stream = MagicMock()

        async def _crashing_stream(*args, **kwargs):
            raise RuntimeError("LLM crashed")
            yield  # unreachable; keeps async generator type valid

        agent.stream.return_value = _crashing_stream()

        store = RefinementStore(
            str(tmp_path / "refinements.json"), agent_factory=lambda: agent
        )
        entry = await store.propose_refinement(
            "def1",
            base_prompt="bp",
            session_id="s1",
            conversation_history="history",
        )
        assert entry is None

    @pytest.mark.asyncio
    async def test_propose_refinement_passes_correct_prompt(
        self, tmp_path: Path
    ) -> None:
        """propose_refinement passes the refinement prompt to agent.stream."""
        agent = MagicMock()
        agent.stream = MagicMock()
        captured_prompt: list[str] = []

        async def _capture(message, *args, **kwargs):
            captured_prompt.append(message)
            yield "lesson"
            return

        agent.stream.side_effect = _capture

        store = RefinementStore(
            str(tmp_path / "refinements.json"), agent_factory=lambda: agent
        )
        await store.propose_refinement(
            "def1",
            base_prompt="base prompt",
            session_id="s1",
            conversation_history="transcript",
        )
        assert len(captured_prompt) == 1
        assert "base prompt" in captured_prompt[0]
        assert "transcript" in captured_prompt[0]


# ---------------------------------------------------------------------------
# _build_refinement_prompt
# ---------------------------------------------------------------------------


class TestBuildRefinementPrompt:
    """Tests for the _build_refinement_prompt helper."""

    def test_includes_base_prompt_and_history(self) -> None:
        """The prompt includes the base prompt, current addendum, and transcript."""
        result = _build_refinement_prompt(
            base_prompt="do X",
            current_addendum="avoid Y",
            conversation_history="step 1\nstep 2",
        )
        assert "do X" in result
        assert "avoid Y" in result
        assert "step 1" in result
        assert "step 2" in result

    def test_shows_none_for_empty_addendum(self) -> None:
        """When current_addendum is empty, '(none)' is shown."""
        result = _build_refinement_prompt(
            base_prompt="do X",
            current_addendum="",
            conversation_history="steps",
        )
        assert "(none)" in result

    def test_output_only_addendum_instruction(self) -> None:
        """The prompt instructs the LLM to output only the addendum text."""
        result = _build_refinement_prompt(
            base_prompt="do X",
            current_addendum="",
            conversation_history="steps",
        )
        assert "Output ONLY the addendum text" in result


# ---------------------------------------------------------------------------
# _summarise_history
# ---------------------------------------------------------------------------


class TestSummariseHistory:
    """Tests for the _summarise_history helper."""

    def test_empty_history(self) -> None:
        """Empty history returns a placeholder."""
        assert _summarise_history("") == "(empty run)"

    def test_short_history_returned_verbatim(self) -> None:
        """Short history (≤ 500 chars) is returned unchanged (after strip)."""
        text = "hello " * 50  # ~300 chars
        assert _summarise_history(text) == text.strip()

    def test_long_history_summarised(self) -> None:
        """Long history is summarised with head + tail."""
        head = "BEGIN " * 50  # 300 chars
        tail = " END" * 80  # 320 chars
        text = head + " MIDDLE " * 500 + tail
        result = _summarise_history(text)
        assert len(result) < len(text)
        assert "..." in result
        assert "BEGIN" in result
        assert "END" in result

    def test_history_exactly_500(self) -> None:
        """History exactly 500 chars is returned verbatim."""
        text = "x" * 500
        assert _summarise_history(text) == text


# ---------------------------------------------------------------------------
# Thread safety (basic smoke test)
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Basic smoke tests for per-definition lock lifecycle."""

    def test_get_lock_creates_and_reuses(self, tmp_path: Path) -> None:
        """_get_lock creates a lock and returns the same lock on subsequent calls."""
        store = _sample_store(tmp_path)
        lock1 = store._get_lock("def1")
        lock2 = store._get_lock("def1")
        assert lock1 is lock2

    def test_get_lock_different_definitions(self, tmp_path: Path) -> None:
        """_get_lock returns distinct locks for different definitions."""
        store = _sample_store(tmp_path)
        lock_a = store._get_lock("def-a")
        lock_b = store._get_lock("def-b")
        assert lock_a is not lock_b


# ---------------------------------------------------------------------------
# RefinementEntry / DefinitionRefinementState dataclass
# ---------------------------------------------------------------------------


class TestRefinementEntry:
    """Unit tests for the RefinementEntry dataclass."""

    def test_defaults(self) -> None:
        """All fields accept explicit values."""
        import time

        ts = time.time()
        entry = RefinementEntry(
            id="abc",
            timestamp=ts,
            base_prompt="bp",
            previous_addendum="prev",
            proposed_addendum="prop",
            feedback_summary="fs",
            session_id="s1",
            status="pending",
        )
        assert entry.id == "abc"
        assert entry.timestamp == ts
        assert entry.base_prompt == "bp"
        assert entry.previous_addendum == "prev"
        assert entry.proposed_addendum == "prop"
        assert entry.feedback_summary == "fs"
        assert entry.session_id == "s1"
        assert entry.status == "pending"


class TestDefinitionRefinementState:
    """Unit tests for the DefinitionRefinementState dataclass."""

    def test_defaults(self) -> None:
        """Default values: empty base_prompt, empty addendum, empty entries list."""
        state = DefinitionRefinementState(definition_name="def1")
        assert state.definition_name == "def1"
        assert state.base_prompt == ""
        assert state.accepted_addendum == ""
        assert state.entries == []
