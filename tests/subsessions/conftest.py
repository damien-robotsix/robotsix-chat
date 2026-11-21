"""Shared helpers and fixtures for subsession worker tests.

Holds the small cross-cutting helpers that the worker test modules all use
``_spawn`` / ``_await_worker`` spawn a subsession with test-friendly
defaults and wait for its worker task; ``OWNER`` is the canonical owner
session id; and the autouse ``_instant_transient_backoff`` fixture collapses
the transient-retry backoff so retry paths run fast.
"""

from __future__ import annotations

import asyncio

import pytest

from robotsix_chat.subsessions import SubsessionEnv, SubsessionKind, spawn_subsession
from robotsix_chat.subsessions import turn_runner as turn_runner_mod

OWNER = "sess-main"


@pytest.fixture(autouse=True)
def _instant_transient_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse transient-retry backoff for every test in this module.

    The retry loop is ``robotsix_http.acall_with_retry``, whose delay is
    ``min(backoff_base ** attempt, backoff_cap)``. Pinning the cap to a
    tiny positive value makes every retry immediate while staying above the
    ``backoff_cap > 0`` validation that robotsix-http's ``RetryConfig``
    enforces (a cap of 0.0 is rejected), so the transient-error paths stay
    fast without a per-test knob — the two ``transient_error_backoff_*``
    settings these tests used to pass were removed along with the
    hand-rolled loop.
    """
    monkeypatch.setattr(turn_runner_mod, "_TRANSIENT_BACKOFF_CAP", 1e-9)


def _spawn(
    env: SubsessionEnv,
    *,
    kind: SubsessionKind = SubsessionKind.TASK,
    parent_id: str | None = None,
    depth: int = 1,
    title: str = "job",
    prompt: str = "do the thing",
    model_level: int = 3,
    **kwargs: object,
) -> str:
    """Spawn a subsession with sensible defaults for tests."""
    return spawn_subsession(
        env=env,
        kind=kind,
        owner_session_id=OWNER,
        parent_id=parent_id,
        depth=depth,
        title=title,
        prompt=prompt,
        model_level=model_level,
        **kwargs,  # type: ignore[arg-type]
    )


async def _await_worker(env: SubsessionEnv, sub_id: str, timeout: float = 2.0) -> None:
    """Wait for *sub_id*'s worker task to finish."""
    task = env.registry._running.get(sub_id)
    if task is not None:
        await asyncio.wait_for(task, timeout)
