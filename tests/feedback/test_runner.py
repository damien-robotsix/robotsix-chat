"""Unit tests for :mod:`robotsix_chat.feedback.runner`.

Covers ``_build_feedback_prompt``, ``_parse_tickets``, and
``FeedbackRunner`` (with mocked I/O: ``respx`` for HTTP, ``MockAgent`` for
the LLM agent, and a fake ``SubsessionRegistry``), as well as
Langfuse trace helpers (``_trace_context``, ``_stamp_tags``,
``_stamp_outcome``) and the ``session_id`` / ``trace_metadata``
forwarding through ``_call_agent``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from pydantic import SecretStr

from robotsix_chat.config.models import FeedbackSettings
from robotsix_chat.feedback.runner import FeedbackRunner, _build_feedback_prompt
from robotsix_chat.subsessions.models import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionStatus,
)

# ---------------------------------------------------------------------------
# Helpers (comprehensive suite — prompt, parse, constructor, schedule,
# subsession summaries, agent calls, ticket filing, full run cycle)
# ---------------------------------------------------------------------------


def _settings(**kw: Any) -> FeedbackSettings:
    base: dict[str, Any] = {"enabled": True, "board_url": "http://test-board"}
    base.update(kw)
    return FeedbackSettings(**base)


class _FakeAgent:
    """Scriptable async generator agent for feedback tests."""

    def __init__(
        self,
        tokens: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.tokens = tokens or ["Hello"]
        self.error = error
        self.call_count = 0
        self.called_with: str | None = None

    async def stream(
        self,
        message: str,
        *,
        history: list[tuple[str, str]] | None = None,
        session_id: str | None = None,
        client_id: str | None = None,
        images: list[tuple[str, bytes]] | None = None,
        trace_metadata: dict[str, str] | None = None,
    ) -> AsyncIterator[str]:
        """Yield tokens or raise the configured error."""
        self.call_count += 1
        self.called_with = message
        if self.error is not None:
            raise self.error
        for token in self.tokens:
            yield token


class _FakeSubsessionRegistry:
    """Stub :class:`SubsessionRegistry` for testing summaries."""

    def __init__(self, infos: list[SubsessionInfo] | None = None) -> None:
        self._infos: list[SubsessionInfo] = infos or []

    def list_for_owner(self, owner_session_id: str) -> list[SubsessionInfo]:
        """Return infos whose owner matches."""
        return [i for i in self._infos if i.owner_session_id == owner_session_id]


def _make_info(
    id: str = "ss-1",
    *,
    owner: str = "sess-1",
    kind: SubsessionKind = SubsessionKind.TASK,
    status: SubsessionStatus = SubsessionStatus.CLOSED,
    summary: str = "completed task",
    close_reason: str | None = None,
) -> SubsessionInfo:
    """Build a :class:`SubsessionInfo` with sensible defaults."""
    return SubsessionInfo(
        id=id,
        kind=kind,
        owner_session_id=owner,
        parent_id=None,
        depth=1,
        title="test subsession",
        prompt="do something",
        model_level=1,
        status=status,
        created_at=0.0,
        last_activity_at=0.0,
        summary=summary,
        close_reason=close_reason,
    )


def _make_runner(
    settings: FeedbackSettings | None = None,
    agent: Any = None,
    *,
    subsession_registry: Any = None,
) -> FeedbackRunner:
    """Build a :class:`FeedbackRunner` with fakes; typed as the real class.

    The ``Any`` parameters avoid mypy complaints about injecting fakes
    where ``LlmioChatAgent`` / ``SubsessionRegistry`` are expected.
    """
    return FeedbackRunner(
        settings or _settings(),
        agent or _FakeAgent(),  # type: ignore[arg-type]
        subsession_registry=subsession_registry,
    )


def _ticket_json(tickets: list[dict[str, Any]] | None = None) -> str:
    """Return a valid feedback JSON string with optional ticket list."""
    if tickets is None:
        tickets = [
            {
                "title": "Fix X",
                "description": "X is broken because Y.",
                "kind": "code",
                "target_repo": "robotsix-chat",
            }
        ]
    return json.dumps({"analysis": "some analysis", "tickets": tickets})


# ---------------------------------------------------------------------------
# _build_feedback_prompt
# ---------------------------------------------------------------------------


class TestBuildFeedbackPrompt:
    """Tests for :func:`_build_feedback_prompt`."""

    def test_empty_turns(self) -> None:
        """Empty transcript produces the expected skeleton."""
        result = _build_feedback_prompt("compaction", "s1", [], [], ["robotsix-chat"])
        assert "Trigger: compaction" in result
        assert "Session ID: s1" in result
        assert "(empty)" in result
        assert "Subsession summaries: (none)" in result
        assert "Valid target repos: robotsix-chat" in result

    def test_with_turns(self) -> None:
        """Each turn appears in the transcript."""
        turns = [("hello", "hi there"), ("what's up?", "not much")]
        result = _build_feedback_prompt(
            "session_end", "s2", turns, [], ["robotsix-chat"]
        )
        assert "Trigger: session_end" in result
        assert "Session ID: s2" in result
        assert "User: hello" in result
        assert "Assistant: hi there" in result
        assert "User: what's up?" in result
        assert "Assistant: not much" in result

    def test_long_assistant_truncated(self) -> None:
        """Assistant messages longer than 3000 chars are truncated."""
        long_msg = "x" * 4000
        result = _build_feedback_prompt(
            "compaction", "s3", [("q", long_msg)], [], ["robotsix-chat"]
        )
        assert "x" * 3000 + "\u2026" in result
        assert "x" * 3001 not in result

    def test_with_subsession_summaries(self) -> None:
        """Subsession summaries are rendered when present."""
        summaries: list[dict[str, Any]] = [
            {"kind": "task", "status": "closed", "summary": "did work"},
            {"kind": "periodic", "status": "failed", "summary": "timeout"},
        ]
        result = _build_feedback_prompt(
            "compaction", "s4", [], summaries, ["robotsix-chat"]
        )
        assert "Subsession summaries:" in result
        assert "[0] kind=task status=closed" in result
        assert "did work" in result
        assert "[1] kind=periodic status=failed" in result
        assert "timeout" in result

    def test_subsession_missing_fields(self) -> None:
        """Missing kind/status/summary get sensible defaults."""
        summaries: list[dict[str, Any]] = [{}]
        result = _build_feedback_prompt(
            "compaction", "s5", [], summaries, ["robotsix-chat"]
        )
        assert "kind=unknown" in result
        assert "status=unknown" in result
        assert "(no summary)" in result

    def test_trigger_types(self) -> None:
        """Both ``compaction`` and ``session_end`` triggers are accepted."""
        for trigger in ("compaction", "session_end"):
            result = _build_feedback_prompt(trigger, "s", [], [], ["robotsix-chat"])
            assert f"Trigger: {trigger}" in result

    def test_multiple_repo_ids_in_prompt(self) -> None:
        """Multiple repo ids are listed in the prompt."""
        result = _build_feedback_prompt(
            "compaction", "s1", [], [], ["robotsix-chat", "robotsix-mill"]
        )
        assert "Valid target repos: robotsix-chat, robotsix-mill" in result


# ---------------------------------------------------------------------------
# _parse_tickets
# ---------------------------------------------------------------------------


class TestParseTickets:
    """Tests for :meth:`FeedbackRunner._parse_tickets`."""

    # -- valid input ----------------------------------------------------------

    def test_valid_tickets(self) -> None:
        """Well-formed JSON with valid tickets is parsed correctly."""
        text = _ticket_json(
            [
                {
                    "title": "Fix A",
                    "description": "desc A",
                    "kind": "code",
                    "target_repo": "robotsix-chat",
                },
                {
                    "title": "Improve B",
                    "description": "desc B",
                    "kind": "prompt",
                    "target_repo": "robotsix-chat",
                },
            ]
        )
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert len(tickets) == 2
        assert tickets[0] == {
            "title": "Fix A",
            "description": "desc A",
            "kind": "code",
            "target_repo": "robotsix-chat",
        }
        assert tickets[1] == {
            "title": "Improve B",
            "description": "desc B",
            "kind": "prompt",
            "target_repo": "robotsix-chat",
        }

    def test_empty_tickets_list(self) -> None:
        """An empty tickets list returns an empty result."""
        text = _ticket_json([])
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_markdown_fenced_json(self) -> None:
        """Markdown-fenced JSON (`` ```json ```) is stripped and parsed."""
        inner = _ticket_json()
        text = f"```json\n{inner}\n```"
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert len(tickets) == 1

    def test_markdown_fenced_no_lang(self) -> None:
        """Plain markdown fences (no language tag) are stripped."""
        inner = _ticket_json()
        text = f"```\n{inner}\n```"
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert len(tickets) == 1

    def test_markdown_fenced_trailing_newline(self) -> None:
        """Fence with a trailing newline after closing backticks."""
        inner = _ticket_json()
        text = f"```\n{inner}\n```\n"
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert len(tickets) == 1

    def test_all_valid_kinds(self) -> None:
        """All four valid ticket kinds are accepted."""
        for kind in ("prompt", "tool", "config", "code"):
            text = _ticket_json(
                [
                    {
                        "title": "T",
                        "description": "D",
                        "kind": kind,
                        "target_repo": "robotsix-chat",
                    }
                ]
            )
            tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
            assert len(tickets) == 1
            assert tickets[0]["kind"] == kind

    # -- invalid / edge-case input --------------------------------------------

    def test_non_json(self) -> None:
        """Non-JSON text returns an empty list."""
        tickets = FeedbackRunner._parse_tickets(
            "just some text, no json here", repo_ids=["robotsix-chat"]
        )
        assert tickets == []

    def test_json_array_not_object(self) -> None:
        """A JSON array (not an object) returns empty."""
        tickets = FeedbackRunner._parse_tickets("[1, 2, 3]", repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_missing_tickets_field(self) -> None:
        """JSON object without a ``tickets`` key returns empty."""
        text = json.dumps({"analysis": "ok"})
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_tickets_not_a_list(self) -> None:
        """``tickets`` field that is not a list returns empty."""
        text = json.dumps({"tickets": "not a list"})
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_invalid_kind(self) -> None:
        """Tickets with an unknown ``kind`` are filtered out."""
        text = _ticket_json(
            [
                {
                    "title": "T",
                    "description": "D",
                    "kind": "unknown",
                    "target_repo": "robotsix-chat",
                }
            ]
        )
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_missing_title(self) -> None:
        """Tickets without a title are filtered out."""
        text = _ticket_json(
            [
                {
                    "description": "D",
                    "kind": "code",
                    "target_repo": "robotsix-chat",
                }
            ]
        )
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_missing_description(self) -> None:
        """Tickets without a description are filtered out."""
        text = _ticket_json(
            [
                {
                    "title": "T",
                    "kind": "code",
                    "target_repo": "robotsix-chat",
                }
            ]
        )
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_mixed_valid_invalid(self) -> None:
        """Only valid tickets are kept from a mixed list."""
        mixed: list[Any] = [
            {
                "title": "Good",
                "description": "desc",
                "kind": "code",
                "target_repo": "robotsix-chat",
            },
            {"title": "", "description": "no title", "kind": "code"},
            {
                "title": "Bad Kind",
                "description": "desc",
                "kind": "nope",
                "target_repo": "robotsix-chat",
            },
            "not a dict at all",
        ]
        text = _ticket_json(mixed)
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert len(tickets) == 1
        assert tickets[0]["title"] == "Good"

    def test_json_decode_error_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A JSON decode error emits a warning log message."""
        with caplog.at_level(logging.WARNING):
            FeedbackRunner._parse_tickets("not json {{{", repo_ids=["robotsix-chat"])
        assert "non-JSON output" in caplog.text

    # -- markdown-edge cases --------------------------------------------------

    def test_markdown_open_no_close(self) -> None:
        """Opening fence with no closing fence still parses the body."""
        text = "```\n" + _ticket_json()
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert len(tickets) == 1

    def test_empty_string(self) -> None:
        """Empty string returns an empty list."""
        tickets = FeedbackRunner._parse_tickets("", repo_ids=["robotsix-chat"])
        assert tickets == []

    def test_whitespace_only(self) -> None:
        """Whitespace-only string returns an empty list."""
        tickets = FeedbackRunner._parse_tickets("   \n\t  ", repo_ids=["robotsix-chat"])
        assert tickets == []

    # -- target_repo validation -----------------------------------------------

    def test_missing_target_repo_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Tickets without a target_repo are skipped when repo_ids provided."""
        text = _ticket_json([{"title": "T", "description": "D", "kind": "code"}])
        with caplog.at_level(logging.WARNING):
            tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []
        assert "missing target_repo" in caplog.text

    def test_invalid_target_repo_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Tickets with an unregistered target_repo are skipped."""
        text = _ticket_json(
            [
                {
                    "title": "T",
                    "description": "D",
                    "kind": "code",
                    "target_repo": "nonexistent-repo",
                }
            ]
        )
        with caplog.at_level(logging.WARNING):
            tickets = FeedbackRunner._parse_tickets(text, repo_ids=["robotsix-chat"])
        assert tickets == []
        assert "not in allowed repos" in caplog.text

    def test_multiple_repo_ids_accepted(self) -> None:
        """Tickets targeting any configured repo are accepted."""
        text = _ticket_json(
            [
                {
                    "title": "Fix mill",
                    "description": "desc",
                    "kind": "code",
                    "target_repo": "robotsix-mill",
                }
            ]
        )
        tickets = FeedbackRunner._parse_tickets(
            text, repo_ids=["robotsix-chat", "robotsix-mill"]
        )
        assert len(tickets) == 1
        assert tickets[0]["target_repo"] == "robotsix-mill"

    def test_no_repo_ids_skips_validation(self) -> None:
        """When repo_ids is None, target_repo validation is skipped."""
        text = _ticket_json(
            [
                {
                    "title": "T",
                    "description": "D",
                    "kind": "code",
                    "target_repo": "anything",
                }
            ]
        )
        tickets = FeedbackRunner._parse_tickets(text, repo_ids=None)
        assert len(tickets) == 1
        assert tickets[0]["target_repo"] == "anything"


# ---------------------------------------------------------------------------
# FeedbackRunner — constructor
# ---------------------------------------------------------------------------


class TestFeedbackRunnerConstructor:
    """Tests for :class:`FeedbackRunner.__init__`."""

    def test_stores_settings_and_agent(self) -> None:
        """Constructor stores the passed settings and agent."""
        settings = _settings()
        agent = _FakeAgent()
        runner = _make_runner(settings, agent)
        assert runner._settings is settings
        assert runner._agent is agent  # type: ignore[comparison-overlap]
        assert runner._registry is None

    def test_stores_registry_when_given(self) -> None:
        """The optional subsession registry is stored when provided."""
        registry = _FakeSubsessionRegistry()
        runner = _make_runner(subsession_registry=registry)
        assert runner._registry is registry  # type: ignore[comparison-overlap]

    def test_strips_trailing_slash_from_board_url(self) -> None:
        """Trailing slashes are stripped from the board URL."""
        settings = _settings(board_url="http://test-board/")
        runner = _make_runner(settings)
        assert runner._board_url == "http://test-board"

    def test_board_url_empty_string(self) -> None:
        """An empty board_url is stored as an empty string."""
        settings = _settings(board_url="")
        runner = _make_runner(settings)
        assert runner._board_url == ""

    def test_board_token_extracted(self) -> None:
        """The SecretStr token is unwrapped and stored."""
        settings = _settings(board_api_token=SecretStr("secret-token"))
        runner = _make_runner(settings)
        assert runner._board_token == "secret-token"


# ---------------------------------------------------------------------------
# FeedbackRunner — schedule
# ---------------------------------------------------------------------------


class TestFeedbackRunnerSchedule:
    """Tests for :meth:`FeedbackRunner.schedule`."""

    def test_skips_when_board_url_empty(self, caplog: pytest.LogCaptureFixture) -> None:
        """An empty board_url causes an immediate debug-logged return."""
        settings = _settings(board_url="")
        runner = _make_runner(settings)
        with caplog.at_level(logging.DEBUG):
            runner.schedule("compaction", "sess-1", [])
        assert "no board_url configured" in caplog.text

    @pytest.mark.asyncio
    async def test_creates_background_task(self) -> None:
        """A non-empty board_url creates a named background task."""
        runner = _make_runner(_settings(board_url="http://board"))
        runner.schedule("session_end", "sess-2", [("hi", "hello")])
        tasks: set[Any] = getattr(runner, "_background_tasks", set())
        assert len(tasks) == 1
        (task,) = tasks
        assert task.get_name().startswith("feedback-session_end-sess-2")
        if not task.done():
            _ = await task

    @pytest.mark.asyncio
    async def test_task_cleans_up_after_completion(self) -> None:
        """The done callback removes the task from the strong-reference set."""
        runner = _make_runner(_settings(board_url="http://board"))
        runner.schedule("compaction", "sess-3", [])
        tasks: set[Any] = getattr(runner, "_background_tasks", set())
        assert len(tasks) == 1
        for t in list(tasks):
            if not t.done():
                _ = await t
        assert len(tasks) == 0


# ---------------------------------------------------------------------------
# FeedbackRunner — _collect_subsession_summaries
# ---------------------------------------------------------------------------


class TestCollectSubsessionSummaries:
    """Tests for :meth:`FeedbackRunner._collect_subsession_summaries`."""

    def test_returns_empty_when_registry_is_none(self) -> None:
        """None registry yields an empty list."""
        runner = _make_runner()
        assert runner._collect_subsession_summaries("sess-1") == []

    def test_returns_empty_when_no_matches(self) -> None:
        """No subsessions for the given owner returns empty."""
        registry = _FakeSubsessionRegistry([_make_info(owner="other")])
        runner = _make_runner(subsession_registry=registry)
        assert runner._collect_subsession_summaries("sess-1") == []

    def test_returns_summaries_for_matching_owner(self) -> None:
        """Only subsessions matching the owner session are returned."""
        registry = _FakeSubsessionRegistry(
            [
                _make_info(id="ss-a", owner="sess-1", summary="did A"),
                _make_info(id="ss-b", owner="sess-1", summary="did B"),
                _make_info(id="ss-c", owner="sess-2", summary="did C"),
            ]
        )
        runner = _make_runner(subsession_registry=registry)
        result = runner._collect_subsession_summaries("sess-1")
        assert len(result) == 2
        assert result[0]["id"] == "ss-a"
        assert result[0]["summary"] == "did A"
        assert result[1]["id"] == "ss-b"
        assert result[1]["summary"] == "did B"

    def test_includes_close_reason_when_present(self) -> None:
        """The close_reason field is included in the summary dict."""
        registry = _FakeSubsessionRegistry([_make_info(close_reason="user cancelled")])
        runner = _make_runner(subsession_registry=registry)
        result = runner._collect_subsession_summaries("sess-1")
        assert result[0]["close_reason"] == "user cancelled"

    def test_registry_exception_returns_empty(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A registry that raises still returns an empty list gracefully."""

        class _BrokenRegistry:
            def list_for_owner(self, owner_session_id: str) -> list[SubsessionInfo]:
                raise RuntimeError("boom")

        runner = FeedbackRunner(
            _settings(),
            _FakeAgent(),  # type: ignore[arg-type]
            subsession_registry=_BrokenRegistry(),  # type: ignore[arg-type]
        )
        with caplog.at_level(logging.ERROR):
            result = runner._collect_subsession_summaries("sess-1")
        assert result == []
        assert "Failed to collect subsession summaries" in caplog.text


# ---------------------------------------------------------------------------
# FeedbackRunner — _call_agent
# ---------------------------------------------------------------------------


class TestCallAgent:
    """Tests for :meth:`FeedbackRunner._call_agent`."""

    @pytest.mark.asyncio
    async def test_returns_agent_reply(self) -> None:
        """Tokens are concatenated and whitespace-stripped."""
        agent = _FakeAgent(tokens=["Hello", " ", "world!"])
        runner = _make_runner(agent=agent)
        result = await runner._call_agent("analyse this", session_id="sess-test")
        assert result == "Hello world!"
        assert agent.call_count == 1
        assert agent.called_with == "analyse this"

    @pytest.mark.asyncio
    async def test_returns_none_on_agent_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An exception from the agent is caught and logged; returns None."""
        agent = _FakeAgent(error=RuntimeError("agent down"))
        runner = _make_runner(agent=agent)
        with caplog.at_level(logging.ERROR):
            result = await runner._call_agent("analyse this", session_id="sess-test")
        assert result is None
        assert "Feedback agent call failed" in caplog.text

    @pytest.mark.asyncio
    async def test_strips_whitespace(self) -> None:
        """Leading and trailing whitespace is stripped from the reply."""
        agent = _FakeAgent(tokens=["  \n  response text  \n  "])
        runner = _make_runner(agent=agent)
        result = await runner._call_agent("prompt", session_id="sess-test")
        assert result == "response text"


# ---------------------------------------------------------------------------
# FeedbackRunner — _file_tickets
# ---------------------------------------------------------------------------


class TestFileTickets:
    """Tests for :meth:`FeedbackRunner._file_tickets`."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_board_url_empty(self) -> None:
        """An empty board URL short-circuits and returns (0, 0)."""
        runner = _make_runner(_settings(board_url=""))
        filed, failed = await runner._file_tickets(
            [
                {
                    "title": "T",
                    "description": "D",
                    "kind": "code",
                    "target_repo": "robotsix-chat",
                }
            ],
            trigger_type="compaction",
            session_id="s1",
        )
        assert filed == 0
        assert failed == 0

    @pytest.mark.asyncio
    async def test_posts_tickets_successfully(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """Each ticket is POSTed; the count of successful posts is returned."""
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        runner = _make_runner()
        tickets = [
            {
                "title": "Fix X",
                "description": "X is broken",
                "kind": "code",
                "target_repo": "robotsix-chat",
            },
            {
                "title": "Improve Y",
                "description": "Y needs work",
                "kind": "prompt",
                "target_repo": "robotsix-chat",
            },
        ]
        filed, failed = await runner._file_tickets(
            tickets, trigger_type="compaction", session_id="sess-1"
        )
        assert filed == 2
        assert failed == 0
        assert route.call_count == 2

    @pytest.mark.asyncio
    async def test_payload_includes_metadata(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """Each POST body carries repo_id, title, body, source_tag.

        Runner-level metadata (kind, session_id, trigger_type, origin) is folded
        into the body text, not exposed as top-level keys — mill's
        TicketIngest schema only accepts repo_id/title/body/source_tag.
        """
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        runner = _make_runner()
        await runner._file_tickets(
            [
                {
                    "title": "T",
                    "description": "D",
                    "kind": "config",
                    "target_repo": "robotsix-chat",
                }
            ],
            trigger_type="session_end",
            session_id="abc-123",
        )
        body = json.loads(route.calls.last.request.content)
        assert body["title"] == "T"
        assert body["repo_id"] == "robotsix-chat"
        assert body["source_tag"] == "robotsix-chat-feedback"
        # Mill ingest contract: must send 'body', never 'description'.
        assert "body" in body
        assert "description" not in body
        # Runner metadata folded into body, not top-level keys.
        assert "kind" not in body
        assert "source_session_id" not in body
        assert "trigger_type" not in body
        assert "kind: config" in body["body"]
        assert "session: abc-123" in body["body"]
        assert "trigger: session_end" in body["body"]
        assert "origin: robotsix-chat" in body["body"]

    @pytest.mark.asyncio
    async def test_includes_bearer_token(self, respx_mock: respx.MockRouter) -> None:
        """A configured API token is sent as a Bearer Authorization header."""
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        settings = _settings(board_api_token=SecretStr("my-token"))
        runner = _make_runner(settings)
        await runner._file_tickets(
            [
                {
                    "title": "T",
                    "description": "D",
                    "kind": "code",
                    "target_repo": "robotsix-chat",
                }
            ],
            trigger_type="compaction",
            session_id="s1",
        )
        assert route.calls.last.request.headers["authorization"] == "Bearer my-token"

    @pytest.mark.asyncio
    async def test_non_2xx_logs_warning(
        self, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-2xx responses log a warning and are not counted as filed."""
        respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(400, text="Bad request")
        )
        runner = _make_runner()
        with caplog.at_level(logging.WARNING):
            filed, failed = await runner._file_tickets(
                [
                    {
                        "title": "T",
                        "description": "D",
                        "kind": "code",
                        "target_repo": "robotsix-chat",
                    }
                ],
                trigger_type="compaction",
                session_id="s1",
            )
        assert filed == 0
        assert failed == 1
        assert "returned 400" in caplog.text

    @pytest.mark.asyncio
    async def test_http_exception_logs_and_continues(
        self, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """HTTP exceptions are caught, logged, and the loop continues."""
        respx_mock.post("http://test-board/tickets/ingest").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        runner = _make_runner()
        with caplog.at_level(logging.ERROR):
            filed, failed = await runner._file_tickets(
                [
                    {
                        "title": "T",
                        "description": "D",
                        "kind": "code",
                        "target_repo": "robotsix-chat",
                    }
                ],
                trigger_type="compaction",
                session_id="s1",
            )
        assert filed == 0
        assert failed == 1
        assert "Failed to file feedback ticket" in caplog.text

    @pytest.mark.asyncio
    async def test_filing_targets_cross_repo(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """A ticket targeted at robotsix-mill uses repo_id=robotsix-mill."""
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        runner = _make_runner(_settings())
        filed, failed = await runner._file_tickets(
            [
                {
                    "title": "Fix mill bug",
                    "description": "Mill is broken",
                    "kind": "code",
                    "target_repo": "robotsix-mill",
                }
            ],
            trigger_type="compaction",
            session_id="sess-mill",
        )
        assert filed == 1
        assert failed == 0
        body = json.loads(route.calls.last.request.content)
        assert body["repo_id"] == "robotsix-mill"
        assert body["title"] == "Fix mill bug"
        assert body["source_tag"] == "robotsix-chat-feedback"
        assert "origin: robotsix-chat" in body["body"]

    @pytest.mark.asyncio
    async def test_non_2xx_records_span_error(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """Non-2xx responses set span status to ERROR with the HTTP code."""
        respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(503, text="Service Unavailable")
        )
        runner = _make_runner()

        fake_span = MagicMock()
        fake_span.__enter__.return_value = fake_span
        with patch(
            "robotsix_chat.feedback.runner.start_span",
            return_value=fake_span,
        ):
            filed, failed = await runner._file_tickets(
                [
                    {
                        "title": "T",
                        "description": "D",
                        "kind": "code",
                        "target_repo": "robotsix-chat",
                    }
                ],
                trigger_type="compaction",
                session_id="s1",
            )
        assert filed == 0
        assert failed == 1
        # Span should have http.status_code, error.type, and ERROR status.
        fake_span.set_attribute.assert_any_call("http.status_code", 503)
        fake_span.set_attribute.assert_any_call("error.type", "http_503")
        fake_span.set_status.assert_called_once()
        # Don't assert exact Status object (depends on OTel version); just
        # verify it was called with an ERROR status code.
        status_arg = fake_span.set_status.call_args[0][0]
        assert status_arg.status_code.name == "ERROR"

    @pytest.mark.asyncio
    async def test_exception_records_span_error(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """HTTP exceptions record the error on the span and set ERROR status."""
        exc = httpx.ReadTimeout("timed out")
        respx_mock.post("http://test-board/tickets/ingest").mock(side_effect=exc)
        runner = _make_runner()

        fake_span = MagicMock()
        fake_span.__enter__.return_value = fake_span
        with patch(
            "robotsix_chat.feedback.runner.start_span",
            return_value=fake_span,
        ):
            filed, failed = await runner._file_tickets(
                [
                    {
                        "title": "T",
                        "description": "D",
                        "kind": "code",
                        "target_repo": "robotsix-chat",
                    }
                ],
                trigger_type="compaction",
                session_id="s1",
            )
        assert filed == 0
        assert failed == 1
        fake_span.record_exception.assert_called_once_with(exc)
        fake_span.set_status.assert_called_once()
        status_arg = fake_span.set_status.call_args[0][0]
        assert status_arg.status_code.name == "ERROR"

    @pytest.mark.asyncio
    async def test_span_error_instrumentation_never_breaks_loop(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """A broken span (raising on record_exception) does not abort filing."""
        respx_mock.post("http://test-board/tickets/ingest").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        runner = _make_runner()

        fake_span = MagicMock()
        fake_span.__enter__.return_value = fake_span
        fake_span.record_exception.side_effect = RuntimeError("span broken")
        with patch(
            "robotsix_chat.feedback.runner.start_span",
            return_value=fake_span,
        ):
            filed, failed = await runner._file_tickets(
                [
                    {
                        "title": "T",
                        "description": "D",
                        "kind": "code",
                        "target_repo": "robotsix-chat",
                    }
                ],
                trigger_type="compaction",
                session_id="s1",
            )
        # Loop completed; the ticket was counted as failed.
        assert filed == 0
        assert failed == 1


# FeedbackRunner — _run (integration-style)
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for :meth:`FeedbackRunner._run` (full cycle)."""

    @pytest.mark.asyncio
    async def test_full_cycle_files_tickets(
        self, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A complete run: agent → parse → HTTP POST → log summary."""
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        agent = _FakeAgent(tokens=[_ticket_json()])
        runner = _make_runner(agent=agent)
        with caplog.at_level(logging.INFO):
            await runner._run("compaction", "sess-1", [("hi", "hello")])
        assert route.call_count == 1
        assert "Feedback run complete" in caplog.text
        assert "filed=1/1 failed=0" in caplog.text

    @pytest.mark.asyncio
    async def test_agent_returns_non_json(self, respx_mock: respx.MockRouter) -> None:
        """Non-JSON agent output → empty parse → no HTTP calls."""
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        agent = _FakeAgent(tokens=["just some prose, no json"])
        runner = _make_runner(agent=agent)
        await runner._run("compaction", "sess-1", [("hi", "hello")])
        assert route.call_count == 0

    @pytest.mark.asyncio
    async def test_agent_returns_empty_tickets(
        self, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty tickets list → early return, no HTTP calls."""
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        agent = _FakeAgent(tokens=[_ticket_json([])])
        runner = _make_runner(agent=agent)
        with caplog.at_level(logging.INFO):
            await runner._run("session_end", "sess-1", [])
        assert route.call_count == 0
        assert "no actionable tickets" in caplog.text

    @pytest.mark.asyncio
    async def test_agent_call_fails(
        self, respx_mock: respx.MockRouter, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Agent raises → logged, _run returns gracefully, no HTTP."""
        route = respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        agent = _FakeAgent(error=RuntimeError("agent crashed"))
        runner = _make_runner(agent=agent)
        with caplog.at_level(logging.ERROR):
            await runner._run("compaction", "sess-1", [("hi", "hello")])
        assert route.call_count == 0
        assert "Feedback agent call failed" in caplog.text

    @pytest.mark.asyncio
    async def test_top_level_exception_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unexpected exception after agent call is caught by _run."""
        agent = _FakeAgent(tokens=[_ticket_json()])
        runner = _make_runner(agent=agent)
        with (
            caplog.at_level(logging.ERROR),
            patch.object(
                FeedbackRunner,
                "_parse_tickets",
                side_effect=RuntimeError("unexpected failure"),
            ),
        ):
            await runner._run("compaction", "sess-1", [])
        assert "Feedback run failed" in caplog.text

    @pytest.mark.asyncio
    async def test_includes_subsession_summaries(
        self, respx_mock: respx.MockRouter
    ) -> None:
        """Subsession summaries are folded into the agent prompt."""
        respx_mock.post("http://test-board/tickets/ingest").mock(
            return_value=httpx.Response(201)
        )
        registry = _FakeSubsessionRegistry([_make_info(id="ss-1", summary="work done")])
        agent = _FakeAgent(tokens=[_ticket_json()])
        runner = _make_runner(agent=agent, subsession_registry=registry)
        await runner._run("compaction", "sess-1", [("q", "a")])
        assert "work done" in (agent.called_with or "")


# ===========================================================================
# Cross-repo / multi-target tests
# ===========================================================================


class TestDynamicRepoResolution:
    """Tests for dynamic allowed-repo resolution at run time."""

    @pytest.mark.asyncio
    async def test_run_passes_resolved_repos_to_prompt(self) -> None:
        """_run includes dynamically-resolved target repos in the agent prompt."""
        respx_mock = pytest.importorskip("respx")
        agent = _FakeAgent(tokens=[_ticket_json()])
        runner = _make_runner(agent=agent)
        from robotsix_chat.feedback import runner as target_module

        with (
            respx_mock.mock,
            patch.object(
                target_module,
                "_resolve_allowed_repos",
                return_value=["robotsix-chat", "robotsix-mill"],
            ),
        ):
            respx_mock.mock.post("http://test-board/tickets/ingest").mock(
                return_value=httpx.Response(201)
            )
            await runner._run("compaction", "sess-dynamic", [("q", "a")])
        assert "Valid target repos: robotsix-chat, robotsix-mill" in (
            agent.called_with or ""
        )


# ===========================================================================
# Trace-observability tests (Langfuse trace naming, tagging, metadata)
# ===========================================================================


# ---------------------------------------------------------------------------
# Fake agent — captures stream() kwargs for assertion
# ---------------------------------------------------------------------------


class _CaptureAgent:
    """A fake agent that records the last ``stream()`` call's kwargs."""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens or ["{}"]
        self.last_kwargs: dict[str, object] = {}

    async def stream(self, message: str, **kwargs: object) -> AsyncIterator[str]:
        self.last_kwargs = kwargs
        for token in self.tokens:
            yield token


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings() -> FeedbackSettings:
    """FeedbackSettings with a board URL so the runner is active."""
    return FeedbackSettings(
        enabled=True,
        board_url="http://board.example.com",
        board_api_token="test-token",  # type: ignore[arg-type]
    )


@pytest.fixture
def agent() -> _CaptureAgent:
    """Return a fresh _CaptureAgent for each test."""
    return _CaptureAgent()


@pytest.fixture
def runner(settings: FeedbackSettings, agent: _CaptureAgent) -> FeedbackRunner:
    """FeedbackRunner wired to the fake agent."""
    return FeedbackRunner(settings, agent)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Tests: _call_agent passes session_id and trace_metadata to stream()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_agent_forwards_session_id(
    runner: FeedbackRunner, agent: _CaptureAgent
) -> None:
    """_call_agent passes session_id to agent.stream()."""
    await runner._call_agent("test prompt", session_id="sess-123")

    assert agent.last_kwargs.get("session_id") == "sess-123"


@pytest.mark.asyncio
async def test_call_agent_forwards_trace_metadata(
    runner: FeedbackRunner, agent: _CaptureAgent
) -> None:
    """_call_agent passes trace_metadata to agent.stream()."""
    meta = {"trigger_type": "compaction", "session_id": "sess-456"}
    await runner._call_agent("test prompt", session_id="sess-456", trace_metadata=meta)

    assert agent.last_kwargs.get("trace_metadata") == meta


@pytest.mark.asyncio
async def test_call_agent_passes_none_history_and_client_id(
    runner: FeedbackRunner, agent: _CaptureAgent
) -> None:
    """_call_agent still passes history=None, client_id=None (backward compat)."""
    await runner._call_agent("test prompt", session_id="sess-789")

    assert agent.last_kwargs.get("history") is None
    assert agent.last_kwargs.get("client_id") is None


# ---------------------------------------------------------------------------
# Tests: _trace_context returns appropriate context manager
# ---------------------------------------------------------------------------


def test_trace_context_with_start_trace_available() -> None:
    """When start_trace is available, _trace_context returns it."""
    fake_cm = MagicMock()
    with patch("robotsix_chat.feedback.runner.start_trace", return_value=fake_cm):
        ctx = FeedbackRunner._trace_context("feedback-compaction", "sess-1")
    assert ctx is fake_cm


def test_trace_context_when_start_trace_is_none() -> None:
    """When start_trace is None (tracing absent), returns a nullcontext."""
    with patch("robotsix_chat.feedback.runner.start_trace", None):
        ctx = FeedbackRunner._trace_context("feedback-compaction", "sess-1")
    # nullcontext is truthy and can be entered/exited
    with ctx:
        pass  # no error → works


# ---------------------------------------------------------------------------
# Tests: _stamp_tags sets langfuse.trace.tags on the recording span
# ---------------------------------------------------------------------------


def test_stamp_tags_sets_attribute() -> None:
    """_stamp_tags sets the langfuse.trace.tags attribute with JSON array."""
    fake_span = MagicMock()
    with patch(
        "robotsix_chat.feedback.runner.get_recording_span",
        return_value=fake_span,
    ):
        FeedbackRunner._stamp_tags("compaction")

    fake_span.set_attribute.assert_called_once_with(
        "langfuse.trace.tags", '["feedback", "compaction"]'
    )


def test_stamp_tags_noop_when_get_recording_span_is_none() -> None:
    """_stamp_tags is a no-op when get_recording_span is None."""
    with patch("robotsix_chat.feedback.runner.get_recording_span", None):
        # Should not raise
        FeedbackRunner._stamp_tags("compaction")


def test_stamp_tags_noop_when_span_is_none() -> None:
    """_stamp_tags is a no-op when get_recording_span returns None."""
    with patch(
        "robotsix_chat.feedback.runner.get_recording_span",
        return_value=None,
    ):
        # Should not raise
        FeedbackRunner._stamp_tags("compaction")


# ---------------------------------------------------------------------------
# Tests: _stamp_outcome sets feedback.* attributes
# ---------------------------------------------------------------------------


def test_stamp_outcome_sets_attributes() -> None:
    """_stamp_outcome sets feedback attributes for filed, failed, and total."""
    fake_span = MagicMock()
    with patch(
        "robotsix_chat.feedback.runner.get_recording_span",
        return_value=fake_span,
    ):
        FeedbackRunner._stamp_outcome(filed=3, total=5, failed=2)

    assert fake_span.set_attribute.call_count == 3
    fake_span.set_attribute.assert_any_call("feedback.filed_tickets", 3)
    fake_span.set_attribute.assert_any_call("feedback.failed_tickets", 2)
    fake_span.set_attribute.assert_any_call("feedback.total_tickets", 5)


def test_stamp_outcome_noop_when_get_recording_span_is_none() -> None:
    """_stamp_outcome is a no-op when get_recording_span is None."""
    with patch("robotsix_chat.feedback.runner.get_recording_span", None):
        FeedbackRunner._stamp_outcome(filed=0, total=0, failed=0)
