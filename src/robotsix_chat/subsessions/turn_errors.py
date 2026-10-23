"""Exception classifiers for subsession agent turns.

Decide whether a failed turn is a retryable condition (GitHub rate limit,
unexpected model behaviour) for the transient-retry loop in
:mod:`.turn_runner`.
"""

from __future__ import annotations


def _is_github_rate_limit_error(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a GitHub API rate-limit error (403/429).

    Inspects the exception's ``__cause__`` and ``__context__`` chain
    for ``RuntimeError`` messages matching the pattern emitted by
    :class:`~robotsix_chat.repo.direct.client.DirectRepoClient`.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        exc_id = id(current)
        if exc_id in seen:
            break
        seen.add(exc_id)
        if isinstance(current, RuntimeError):
            msg = str(current)
            if "GitHub API" in msg and (
                "error 429" in msg
                or ("error 403" in msg and "rate limit" in msg.lower())
            ):
                return True
        current = current.__cause__ or current.__context__
    return False


def _is_unexpected_model_behavior(exc: BaseException) -> bool:
    """Return ``True`` when *exc* is a pydantic-ai ``UnexpectedModelBehavior``.

    The LLM provider occasionally returns an unexpected response shape
    (empty stream, malformed JSON delta, missing candidate) that pydantic-ai
    surfaces as ``UnexpectedModelBehavior``.  These are typically transient —
    a retry almost always succeeds — so monitor subsessions should ride them
    out with backoff rather than failing permanently.

    Walks the ``__cause__`` / ``__context__`` chain because agent frameworks
    routinely wrap the original error.
    """
    # Lazy import to avoid a hard dependency at module level — pydantic-ai
    # is always present in this repo, but the import is cheap and keeps
    # the function self-contained.
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        exc_id = id(current)
        if exc_id in seen:
            break
        seen.add(exc_id)
        if isinstance(current, UnexpectedModelBehavior):
            return True
        current = current.__cause__ or current.__context__
    return False
