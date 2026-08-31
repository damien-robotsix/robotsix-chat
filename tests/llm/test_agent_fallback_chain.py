"""The fallback trigger matches Claude unavailability through the chain.

Regression: the CLI launders a usage-limit failure into a generic
``Exception("Claude Code returned an error result: success")`` carrying the
typed error only as ``__context__`` — the turn died without trying the
fallback tiers.
"""

from __future__ import annotations

from robotsix_llmio.claude_sdk import (
    ClaudeSDKAuthError,
    ClaudeSDKUsageExhaustedError,
)

from robotsix_chat.llm.agent import _chained_claude_unavailability


def _chained(inner: BaseException, outer: BaseException) -> BaseException:
    try:
        raise inner
    except BaseException:
        try:
            raise outer
        except BaseException as caught:
            return caught


def test_direct_match() -> None:
    exc = ClaudeSDKUsageExhaustedError("limit · resets 1:10pm (UTC)")
    assert _chained_claude_unavailability(exc) is exc


def test_laundered_context_match() -> None:
    root = ClaudeSDKUsageExhaustedError("limit · resets 1:10pm (UTC)")
    exc = _chained(root, Exception("Claude Code returned an error result: success"))
    assert _chained_claude_unavailability(exc) is root


def test_auth_root_detected() -> None:
    root = ClaudeSDKAuthError("credential dead")
    exc = _chained(root, RuntimeError("wrapper"))
    assert isinstance(_chained_claude_unavailability(exc), ClaudeSDKAuthError)


def test_unrelated_exception_returns_none() -> None:
    assert _chained_claude_unavailability(RuntimeError("boom")) is None
