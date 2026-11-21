"""Reply-classification unit tests for the subsession worker.

Split out of ``test_worker.py`` (module_size): covers ``_is_no_change``,
``_is_queued`` and ``_is_duplicate_reply`` plus the periodic integration
tests that suppress duplicate / no-change replies from the event sink.
"""

from __future__ import annotations

import pytest

from robotsix_chat.chat.events import SSE_SUBSESSION_RESULT_TYPE
from robotsix_chat.subsessions import SubsessionKind, SubsessionStatus
from robotsix_chat.subsessions.worker import (
    _is_duplicate_reply,
    _is_no_change,
    _is_queued,
)
from tests.common.subsession_fakes import (
    FakeAgent,
    RecordingSink,
    build_env,
    make_settings,
)
from tests.subsessions.conftest import OWNER, _await_worker, _spawn

# -- _is_no_change / _is_duplicate_reply unit tests ------------------------


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("NO_CHANGE", True),
        ("NO_CHANGE ", True),
        ("no_change", True),
        ("NO_CHANGE.", True),  # startswith catches trailing punctuation
        ("No change", True),  # space variant also caught
        ("No changes", True),
        ("Nothing changed", True),
        ("Nothing has changed", True),
        ("No updates", True),
        ("Unchanged", True),
        ("No new", True),
        ("Everything is the same", True),
        ("All quiet", True),
        ("Status unchanged", True),
        ("No significant change", True),
        ("No meaningful change", True),
        ("  no changes  ", True),
        ("Ticket #123 moved to done", False),
        ("Something actually happened", False),
    ],
)
def test_is_no_change(reply: str, expected: bool) -> None:
    """``_is_no_change`` recognises the sentinel and common paraphrases."""
    assert _is_no_change(reply) == expected


@pytest.mark.parametrize(
    "reply,expected",
    [
        ("QUEUED", True),
        ("QUEUED ", True),
        ("queued", True),
        ("QUEUED.", True),  # startswith catches trailing punctuation
        ("Queued for implementation", True),
        ("Waiting for implementation", True),
        ("In queue", True),
        ("Implementation queued", True),
        ("Awaiting implementation", True),
        ("Pending implementation", True),
        ("  queued  ", True),
        ("NO_CHANGE", False),
        ("Ticket #123 is queued and waiting", False),  # must start with sentinel
        ("Something actually happened", False),
    ],
)
def test_is_queued(reply: str, expected: bool) -> None:
    """``_is_queued`` recognises the queued sentinel and common paraphrases."""
    assert _is_queued(reply) == expected


def test_is_duplicate_reply_none_previous() -> None:
    """A reply is never a duplicate when there's no previous result."""
    assert _is_duplicate_reply("anything", None) is False


def test_is_duplicate_reply_exact_match() -> None:
    """Exact string match is a duplicate."""
    assert _is_duplicate_reply("hello", "hello") is True


def test_is_duplicate_reply_case_insensitive() -> None:
    """Case differences are ignored."""
    assert _is_duplicate_reply("Hello World", "hello world") is True


def test_is_duplicate_reply_whitespace_insensitive() -> None:
    """Leading/trailing whitespace differences are ignored."""
    assert _is_duplicate_reply("  hello  ", "hello") is True


def test_is_duplicate_reply_different() -> None:
    """Different content is not a duplicate."""
    assert _is_duplicate_reply("hello", "goodbye") is False


# -- integration: duplicate non-NO_CHANGE replies are suppressed ------------


@pytest.mark.asyncio
async def test_periodic_duplicate_replies_are_suppressed() -> None:
    """Verbose replies that repeat verbatim are suppressed from the event sink.

    Intermediate runs are never delivered to the parent conversation store
    regardless of suppression — only the terminal summary arrives.
    """
    agent = FakeAgent(["Status: all clear", "Status: all clear"])
    env = build_env(agent=agent)

    sub_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02, max_runs=2
    )
    await _await_worker(env, sub_id)

    # Only the terminal summary is delivered — both intermediate runs stay silent.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_no_change_phrases_are_suppressed() -> None:
    """Common LLM paraphrases of 'no change' are suppressed."""
    agent = FakeAgent(["No changes", "Nothing changed"])
    sink = RecordingSink()
    env = build_env(agent=agent, event_sink=sink)

    sub_id = _spawn(
        env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02, max_runs=2
    )
    await _await_worker(env, sub_id)

    # Neither run should produce a result frame.
    assert sink.of_type(SSE_SUBSESSION_RESULT_TYPE) == []

    # Only the terminal summary is delivered — no per-run turn.
    history = env.conversation_store.history(OWNER)
    assert len(history) == 1
    assert "max_runs" in history[0][0]


@pytest.mark.asyncio
async def test_periodic_no_change_phrases_count_toward_auto_stop() -> None:
    """No-change phrases increment the consecutive counter for auto-stop."""
    agent = FakeAgent(["No changes", "Nothing changed", "NO_CHANGE"])
    env = build_env(agent=agent, settings=make_settings(auto_stop_no_change_runs=2))

    sub_id = _spawn(env, kind=SubsessionKind.PERIODIC, interval_seconds=0.02)
    await _await_worker(env, sub_id)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "no_change_auto_stop"
    # Stopped after 2 consecutive no-change runs, not all 3.
    assert len(agent.calls) == 2
