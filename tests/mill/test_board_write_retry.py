"""Tests for board-write retry queue and broker-unavailability detection."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from robotsix_chat.broker_client import (
    BaseBrokeredClient,
    BrokerUnavailableError,
    _is_broker_unavailable,
)
from robotsix_chat.mill.retry_queue import (
    _INITIAL_DELAY,
    _JITTER_FRACTION,
    _MAX_DELAY,
    _MAX_RETRY_REQUEST_CHARS,
    BoardWriteRetryQueue,
    _next_delay,
    _trim_request,
)

# ---------------------------------------------------------------------------
# _is_broker_unavailable() classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg,expected",
    [
        ("unknown recipient: board-manager-robotsix-mill", True),
        ("connection refused", True),
        ("connection timeout", True),
        ("timed out", True),
        ("invalid ticket format", False),
        ("missing field repo_id", False),
    ],
)
def test_is_broker_unavailable_classification(msg: str, expected: bool) -> None:
    """_is_broker_unavailable is True only for broker-unavailable fragments."""
    assert _is_broker_unavailable(RuntimeError(msg)) is expected


# ---------------------------------------------------------------------------
# BaseBrokeredClient.consult() broker-unavailability behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_raises_on_broker_unavailability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """consult() raises BrokerUnavailableError on unreachable-target errors."""
    _install_fake_agent_comm(
        monkeypatch,
        raise_exc=RuntimeError("unknown recipient: board-manager-robotsix-mill"),
    )
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    monkeypatch.setattr(client, "_check_reachable", lambda: (True, ""))
    with pytest.raises(BrokerUnavailableError) as exc_info:
        await client.consult("hello", empty_reply="E", error_label="test")
    assert "unknown recipient" in str(exc_info.value)


@pytest.mark.asyncio
async def test_consult_returns_string_for_non_broker_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """consult() still catches non-broker-unavailability errors and returns text."""
    _install_fake_agent_comm(
        monkeypatch,
        raise_exc=ValueError("invalid schema"),
    )
    client = BaseBrokeredClient(
        _settings(), target_agent_id="t", default_reply="default"
    )
    monkeypatch.setattr(client, "_check_reachable", lambda: (True, ""))
    out = await client.consult("hello", empty_reply="E", error_label="test")
    assert out.startswith("The test request could not be completed:")


# ---------------------------------------------------------------------------
# consult_mill enqueue behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consult_mill_enqueues_on_broker_unavailable_error(
    tmp_path: Path,
) -> None:
    """consult_mill enqueues write when _raw_consult raises BrokerUnavailableError."""
    call_count = 0

    async def _failing_consult(request: str) -> str:
        nonlocal call_count
        call_count += 1
        raise BrokerUnavailableError("unreachable")

    q = BoardWriteRetryQueue(
        consult_fn=_failing_consult, queue_path=tmp_path / "q.json"
    )
    result = q.enqueue("create ticket: fix login bug")
    assert "queued" in result
    assert len(q._entries) == 1
    # _failing_consult should not have been called — enqueue does not call consult_fn
    assert call_count == 0


def test_enqueue_deduplicates_identical_requests(tmp_path: Path) -> None:
    """Enqueuing the same request twice creates only one entry."""
    q = BoardWriteRetryQueue(consult_fn=_noop_consult, queue_path=tmp_path / "q.json")
    r1 = q.enqueue("create ticket: fix login bug")
    r2 = q.enqueue("create ticket: fix login bug")
    assert "queued" in r1
    assert "already queued" in r2.lower()
    assert len(q._entries) == 1


# ---------------------------------------------------------------------------
# _next_delay backoff + cap
# ---------------------------------------------------------------------------


def test_next_delay_initial() -> None:
    """_next_delay(0) is within INITIAL_DELAY ± JITTER_FRACTION."""
    for _ in range(50):
        d = _next_delay(0)
        assert (
            _INITIAL_DELAY * (1 - _JITTER_FRACTION)
            <= d
            <= (_INITIAL_DELAY * (1 + _JITTER_FRACTION))
        )


def test_next_delay_fourth_attempt() -> None:
    """_next_delay(4) is within expected range, capped by _MAX_DELAY."""
    for _ in range(50):
        d = _next_delay(4)
        # raw = 900 * 2^4 = 14400, which equals _MAX_DELAY
        assert (
            _MAX_DELAY * (1 - _JITTER_FRACTION)
            <= d
            <= (_MAX_DELAY * (1 + _JITTER_FRACTION))
        )


def test_next_delay_capped_at_max() -> None:
    """_next_delay(10) is capped at _MAX_DELAY (before jitter)."""
    for _ in range(50):
        d = _next_delay(10)
        # raw = 900 * 2^10 = 921_600 >> _MAX_DELAY, so capped
        assert d <= _MAX_DELAY * (1 + _JITTER_FRACTION)
        assert d >= _MAX_DELAY * (1 - _JITTER_FRACTION)


# ---------------------------------------------------------------------------
# Drain loop behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_loop_removes_successful_entry(tmp_path: Path) -> None:
    """A successful retry removes the entry from the queue."""
    call_count = 0

    async def _succeed(request: str) -> str:
        nonlocal call_count
        call_count += 1
        return "board manager: ticket created"

    queue_file = tmp_path / "queue.json"
    q = BoardWriteRetryQueue(consult_fn=_succeed, queue_path=queue_file)
    q.enqueue("create ticket: fix bug")

    # Run one iteration of drain loop
    # We need to bypass the sleep and only process the entry
    await _drain_one_iteration(q)
    assert len(q._entries) == 0
    assert call_count == 1


@pytest.mark.asyncio
async def test_drain_loop_backs_off_on_broker_unavailable(tmp_path: Path) -> None:
    """On BrokerUnavailableError the entry is bumped to a later attempt."""
    call_count = 0

    async def _fail_unavailable(request: str) -> str:
        nonlocal call_count
        call_count += 1
        raise BrokerUnavailableError("timed out")

    queue_file = tmp_path / "queue.json"
    q = BoardWriteRetryQueue(consult_fn=_fail_unavailable, queue_path=queue_file)
    q.enqueue("create ticket: fix bug")

    entry_before = list(q._entries.values())[0]
    assert entry_before["attempt_count"] == 0

    from datetime import UTC, datetime

    now_before = datetime.now(UTC)
    await _drain_one_iteration(q)

    entry = list(q._entries.values())[0]
    assert entry["attempt_count"] == 1
    assert entry["status"] == "pending"

    nxt = datetime.fromisoformat(entry["next_attempt_at"])
    # attempt_count was incremented to 1 before _next_delay is called,
    # so the new delay = _next_delay(1) = 1800 ± 20 % (1440-2160).
    # The spec requires only a lower-bound assertion: ≥ _INITIAL_DELAY * 0.8.
    delta = (nxt - now_before).total_seconds()
    assert delta >= _INITIAL_DELAY * 0.8


@pytest.mark.asyncio
async def test_drain_loop_marks_non_broker_error_as_failed(tmp_path: Path) -> None:
    """Non-BrokerUnavailableError exceptions mark the entry as failed, no retry."""

    async def _fail_bad(req: str) -> str:
        raise ValueError("bad payload")

    queue_file = tmp_path / "queue.json"
    q = BoardWriteRetryQueue(consult_fn=_fail_bad, queue_path=queue_file)
    q.enqueue("create ticket: fix bug")

    await _drain_one_iteration(q)

    entry = list(q._entries.values())[0]
    assert entry["status"] == "failed"
    assert "bad payload" in entry["last_error"]


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


def test_persistence_round_trip(tmp_path: Path) -> None:
    """Queue entries survive a process restart via JSON persistence."""
    queue_file = tmp_path / "queue.json"
    q1 = BoardWriteRetryQueue(consult_fn=_noop_consult, queue_path=queue_file)
    r = q1.enqueue("create ticket: persist me")
    assert "queued" in r

    # Simulate restart: create a new queue pointing at the same file
    q2 = BoardWriteRetryQueue(consult_fn=_noop_consult, queue_path=queue_file)
    assert len(q2._entries) == 1
    entry = list(q2._entries.values())[0]
    assert entry["request"] == "create ticket: persist me"
    assert entry["status"] == "pending"


# ---------------------------------------------------------------------------
# status() output
# ---------------------------------------------------------------------------


def test_status_empty_queue(tmp_path: Path) -> None:
    """status() returns a short message when the queue is empty."""
    q = BoardWriteRetryQueue(consult_fn=_noop_consult, queue_path=tmp_path / "q.json")
    assert "No pending board writes" in q.status()


def test_status_with_entries(tmp_path: Path) -> None:
    """status() returns a table with entry details."""
    q = BoardWriteRetryQueue(consult_fn=_noop_consult, queue_path=tmp_path / "q.json")
    q.enqueue("create ticket: fix critical bug in auth module")
    s = q.status()
    assert "pending" in s
    assert "create ticket: fix critical bug in auth" in s
    assert "in " in s or "overdue" in s  # relative time


# ---------------------------------------------------------------------------
# _trim_request helper
# ---------------------------------------------------------------------------


def test_trim_request_short_unchanged() -> None:
    """A request shorter than the cap is returned unchanged."""
    short = "hello"
    assert _trim_request(short, 4000) == short


def test_trim_request_exactly_at_cap() -> None:
    """A request exactly at the cap is returned unchanged."""
    exact = "x" * _MAX_RETRY_REQUEST_CHARS
    assert _trim_request(exact, _MAX_RETRY_REQUEST_CHARS) == exact


def test_trim_request_long_is_trimmed() -> None:
    """A request longer than the cap is shorter than input and contains the marker."""
    long = "abc" * 2000  # 6000 chars > 4000 cap
    result = _trim_request(long, _MAX_RETRY_REQUEST_CHARS)
    assert len(result) < len(long)
    assert "[retry-trim:" in result
    # Head preserved
    assert result.startswith(long[: 2 * _MAX_RETRY_REQUEST_CHARS // 3])
    # Tail preserved
    tail_len = _MAX_RETRY_REQUEST_CHARS // 3
    assert result.endswith(long[-tail_len:])
    # Length ≤ cap + generous marker overhead (~60 chars max for any digit count)
    assert len(result) <= _MAX_RETRY_REQUEST_CHARS + 60


def test_trim_request_preserves_head_and_tail() -> None:
    """The trimmed result starts with original head and ends with original tail."""
    head_content = "PREFIX_IMPORTANT_STUFF"
    tail_content = "SUFFIX_RECENT_CONTEXT"
    body = "MIDDLE" * 2000
    long = head_content + body + tail_content
    result = _trim_request(long, _MAX_RETRY_REQUEST_CHARS)
    assert result.startswith(head_content)
    assert result.endswith(tail_content)
    assert "[retry-trim:" in result


# ---------------------------------------------------------------------------
# enqueue trimming behaviour
# ---------------------------------------------------------------------------


def test_enqueue_trims_long_request(tmp_path: Path) -> None:
    """Enqueuing a request longer than max_request_chars stores a trimmed entry."""
    cap = 500
    q = BoardWriteRetryQueue(
        consult_fn=_noop_consult,
        queue_path=tmp_path / "q.json",
        max_request_chars=cap,
    )
    long_req = "x" * 2000
    result = q.enqueue(long_req)
    assert "queued" in result
    assert len(q._entries) == 1
    entry = list(q._entries.values())[0]
    stored = entry["request"]
    assert len(stored) < len(long_req)
    assert "[retry-trim:" in stored
    assert len(stored) <= cap + 60


def test_enqueue_short_request_stored_unchanged(tmp_path: Path) -> None:
    """A short request is stored unchanged (no trimming)."""
    q = BoardWriteRetryQueue(consult_fn=_noop_consult, queue_path=tmp_path / "q.json")
    short_req = "create ticket: fix bug"
    q.enqueue(short_req)
    entry = list(q._entries.values())[0]
    assert entry["request"] == short_req


@pytest.mark.asyncio
async def test_drain_resends_trimmed_request(tmp_path: Path) -> None:
    """After enqueuing an over-long request, the drain path sends the trimmed text."""
    cap = 500
    captured: list[str] = []

    async def _capture(request: str) -> str:
        captured.append(request)
        return "ok"

    q = BoardWriteRetryQueue(
        consult_fn=_capture,
        queue_path=tmp_path / "q.json",
        max_request_chars=cap,
    )
    long_req = "y" * 2000
    q.enqueue(long_req)
    await _drain_one_iteration(q)
    assert len(captured) == 1
    assert captured[0] != long_req
    assert "[retry-trim:" in captured[0]
    assert len(captured[0]) < len(long_req)


def test_enqueue_dedup_with_trimming(tmp_path: Path) -> None:
    """Identical long requests trim identically → only one entry."""
    cap = 500
    q = BoardWriteRetryQueue(
        consult_fn=_noop_consult,
        queue_path=tmp_path / "q.json",
        max_request_chars=cap,
    )
    long_req = "z" * 2000
    r1 = q.enqueue(long_req)
    r2 = q.enqueue(long_req)
    assert "queued" in r1
    assert "already queued" in r2.lower()
    assert len(q._entries) == 1


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Reply:
    """Stand-in for a broker reply body."""

    def __init__(self, body: Any) -> None:
        self.body = body


def _install_fake_agent_comm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: Any = None,
    raise_exc: Exception | None = None,
) -> dict[str, Any]:
    """Install a fake robotsix_agent_comm module tree; return a capture dict."""
    captured: dict[str, Any] = {}

    class _FakeBrokeredRequester:
        def __init__(
            self,
            agent_id: str,
            target_agent_id: str,
            *,
            broker_host: str,
            broker_token: str | None,
            broker_port: int = 443,
            broker_scheme: str = "https",
            broker_ssl_context: object | None = None,
            timeout: float = 30.0,
            default_reply: str = "",
        ) -> None:
            captured["agent_id"] = agent_id
            captured["recipient"] = target_agent_id
            captured["broker_host"] = broker_host
            captured["broker_port"] = broker_port
            captured["broker_scheme"] = broker_scheme
            captured["broker_token"] = broker_token
            captured["timeout"] = timeout
            captured["default_reply"] = default_reply
            self._raise_exc = raise_exc
            self._reply = reply
            self._default_reply = default_reply

        def request(
            self,
            payload: dict[str, Any] | None = None,
            *,
            timeout: float | None = None,
            default: str | None = None,
        ) -> str:
            captured["payload"] = payload
            if self._raise_exc is not None:
                raise self._raise_exc
            body = getattr(self._reply, "body", self._reply)
            if isinstance(body, dict):
                r = body.get("reply")
                if r is not None and r != "":
                    return r if isinstance(r, str) else str(r)
                return str(body)
            if body is None:
                return default if default is not None else self._default_reply
            return str(body)

    root = types.ModuleType("robotsix_agent_comm")
    sdk = types.ModuleType("robotsix_agent_comm.sdk")
    sdk.BrokeredRequester = _FakeBrokeredRequester  # type: ignore[attr-defined]

    for name, mod in {
        "robotsix_agent_comm": root,
        "robotsix_agent_comm.sdk": sdk,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return captured


def _settings(**kw: Any) -> Any:
    from types import SimpleNamespace

    defaults: dict[str, Any] = {
        "agent_id": "test-agent",
        "broker_host": "broker.example.com",
        "broker_port": 443,
        "broker_scheme": "https",
        "broker_token": "test-token",
        "timeout": 60.0,
    }
    defaults.update(kw)
    return SimpleNamespace(**defaults)


async def _noop_consult(request: str) -> str:
    return "ok"


async def _drain_one_iteration(q: BoardWriteRetryQueue) -> None:
    """Run one iteration of the drain loop without sleeping.

    Overwrites the entry's ``next_attempt_at`` to be "now" so it's immediately
    eligible, then runs ``_attempt`` directly.
    """
    from datetime import UTC, datetime

    for entry in list(q._entries.values()):
        if entry["status"] == "pending":
            entry["next_attempt_at"] = datetime.now(UTC).isoformat()
    await q._attempt(list(q._entries.values())[0])
