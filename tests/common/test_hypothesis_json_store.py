"""Property-based roundtrip tests for JsonStoreBase dataclass serialization.

Exercises the default ``_to_dict`` / ``_from_dict`` hooks across all three
production ``JsonStoreBase`` subclasses: ``KnowledgeEntry``,
``DiagnosticBundle``, and ``FixProposal``.  Each test generates a random
instance via Hypothesis, serializes through ``_to_dict``, deserializes back
through ``_from_dict``, and asserts the roundtripped object equals the
original — without touching the filesystem.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from robotsix_chat.common.json_store import JsonStoreBase
from robotsix_chat.diagnostics.fixes import FixProposal
from robotsix_chat.diagnostics.store import DiagnosticBundle
from robotsix_chat.knowledge.store import KnowledgeEntry

# ---------------------------------------------------------------------------
# Minimal store subclasses — only exist to expose _to_dict / _from_dict.
# The default path points somewhere that never exists so _load() is a no-op.
# ---------------------------------------------------------------------------


class _KnowledgeTestStore(JsonStoreBase[KnowledgeEntry]):
    _store_name = "knowledge-test"
    _default_path = "/nonexistent/knowledge_test.json"


class _DiagnosticTestStore(JsonStoreBase[DiagnosticBundle]):
    _store_name = "diagnostic-test"
    _default_path = "/nonexistent/diagnostic_test.json"


class _FixProposalTestStore(JsonStoreBase[FixProposal]):
    _store_name = "fix-proposal-test"
    _default_path = "/nonexistent/fix_proposal_test.json"


# ---------------------------------------------------------------------------
# Property-based roundtrip tests
# ---------------------------------------------------------------------------


@given(st.builds(KnowledgeEntry))
def test_knowledge_entry_roundtrip(entry: KnowledgeEntry) -> None:
    """KnowledgeEntry survives _to_dict → _from_dict roundtrip."""
    store = _KnowledgeTestStore()
    dumped = store._to_dict(entry)
    restored = _KnowledgeTestStore._from_dict(dumped)
    assert restored == entry


@given(st.builds(DiagnosticBundle, details=st.none() | st.fixed_dictionaries({})))
def test_diagnostic_bundle_roundtrip(bundle: DiagnosticBundle) -> None:
    """DiagnosticBundle survives _to_dict → _from_dict roundtrip."""
    store = _DiagnosticTestStore()
    dumped = store._to_dict(bundle)
    restored = _DiagnosticTestStore._from_dict(dumped)
    assert restored == bundle


@given(
    st.builds(
        FixProposal,
        status=st.sampled_from(["proposed", "applied", "rejected"]),
    )
)
def test_fix_proposal_roundtrip(proposal: FixProposal) -> None:
    """FixProposal survives _to_dict → _from_dict roundtrip."""
    store = _FixProposalTestStore()
    dumped = store._to_dict(proposal)
    restored = _FixProposalTestStore._from_dict(dumped)
    assert restored == proposal
