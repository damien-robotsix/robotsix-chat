"""Regression test: MessageCoalescer must retain its background task.

``asyncio.create_task()`` only keeps a *weak* reference internally, and
Python's own docs warn that a task with no other strong reference "may get
garbage collected at any time, even before it's done" — every other
``create_task()`` call site in this codebase stores the task in a
long-lived set with a done-callback to avoid exactly this; the coalescer
was the one place that didn't. A plain ``gc.collect()`` doesn't reliably
reproduce the failure in this CPython/asyncio version (the event loop's own
scheduling keeps a task alive while it has a pending callback), so this
test instead verifies the actual fix directly: the task is reachable via a
weakref *only* through ``MessageCoalescer``'s own retained reference, and
is released once the run completes.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import AsyncIterator

import pytest

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server.idempotency import MessageIdempotencyStore
from robotsix_chat.chat.server.routes.chat import MessageCoalescer, RunSerializer


class _SlowAgent:
    """Yields tokens only after an external event fires.

    So a test can inspect the in-flight task before the run completes.
    """

    def __init__(self, release: asyncio.Event, tokens: list[str]) -> None:
        self._release = release
        self._tokens = tokens

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
        await self._release.wait()
        for token in self._tokens:
            yield token


@pytest.mark.asyncio
async def test_background_task_is_retained_while_running_and_released_after() -> None:
    """The processor task is tracked while running and released once done."""
    release = asyncio.Event()
    agent = _SlowAgent(release, ["hi"])
    store = ConversationStore()
    coalescer = MessageCoalescer(debounce_seconds=0.0)
    run_serializer = RunSerializer()
    msg_id_store = MessageIdempotencyStore()

    session_id = "sess-1"
    await coalescer.submit(
        session_id,
        "hello",
        None,
        None,
        agent=agent,
        store=store,
        run_serializer=run_serializer,
        msg_id_store=msg_id_store,
        lock_key=session_id,
        owner_id=session_id,
        had_session=True,
    )

    # Let the processor task start and reach agent.stream()'s await point.
    await asyncio.sleep(0.05)

    # Exactly one task must be tracked while the run is in flight — this is
    # what protects it from the asyncio weak-reference GC pitfall.
    assert len(coalescer._background_tasks) == 1
    task = next(iter(coalescer._background_tasks))
    task_ref = weakref.ref(task)
    del task

    release.set()
    for _ in range(200):
        if not coalescer._background_tasks:
            break
        await asyncio.sleep(0.01)

    # The done-callback must have removed it once the run finished.
    assert coalescer._background_tasks == set()
    finished_task = task_ref()
    assert finished_task is None or finished_task.done()

    _, history = store.begin(session_id)
    assert history == [("hello", "hi")]
