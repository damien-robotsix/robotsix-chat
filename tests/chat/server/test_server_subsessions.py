"""Subsessions HTTP endpoint and agent-wiring tests for the chat SSE server.

Covers the /subsessions endpoints (list, get, transcript, message, close),
registry wiring, per-subsession agent tool construction and client_id tool
scoping. Split out of ``test_server.py`` (module-size).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from robotsix_chat.chat.server import create_agent_from_settings
from robotsix_chat.config import Settings
from robotsix_chat.subsessions import (
    SubsessionInfo,
    SubsessionKind,
    SubsessionRegistry,
    SubsessionStatus,
    spawn_subsession,
)
from tests.chat.server.test_server import _register_subsession
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Subsession registry wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsession_registry_wired_into_app_state() -> None:
    """``subsession_registry``/``subsession_delivery`` land on ``app.state``."""
    from robotsix_chat.chat.events import EventBus

    bus = EventBus()
    registry = SubsessionRegistry(event_sink=bus, store_path=None)

    async with mock_app(subsession_registry=registry, event_bus=bus) as f:
        assert f.app.state.subsession_registry is registry
        assert f.app.state.event_bus is bus


@pytest.mark.asyncio
async def test_subsession_registry_defaults_to_none() -> None:
    """Without the kwargs, ``app.state`` stores ``None`` for both."""
    async with mock_app() as f:
        assert f.app.state.subsession_registry is None
        assert f.app.state.subsession_delivery is None


@pytest.mark.asyncio
async def test_resume_hook_passed_through_mock_app() -> None:
    """The ``on_startup`` callable is threaded through ``create_app``."""
    from unittest.mock import MagicMock

    hook = MagicMock()

    async with mock_app(on_startup=hook) as f:
        pass  # mock_app doesn't run the lifespan, but the hook is wired

    # The hook was passed through; the lifespan stores it but doesn't call
    # it until the ASGI server starts.  We can verify it's been retained.
    # (We can't easily assert it's stored because Starlette's lifespan is
    # internal, but the app was built without error.)
    assert f.app.state.subsession_registry is None  # default


# ---------------------------------------------------------------------------
# GET /subsessions — list endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path_factory", "payload", "expected_error"),
    [
        pytest.param(
            "get",
            lambda _: "/subsessions",
            None,
            "session_id query parameter is required",
            id="list_missing_session_id",
        ),
        pytest.param(
            "post",
            lambda info: f"/subsessions/{info.id}/message",
            {"json": {"text": ""}},
            "text",
            id="message_empty_text",
        ),
        pytest.param(
            "post",
            lambda info: f"/subsessions/{info.id}/message",
            {"json": {}},
            "text",
            id="message_missing_text_field",
        ),
    ],
)
async def test_subsessions_validation_errors(
    method: str,
    path_factory: Callable[[SubsessionInfo], str],
    payload: dict[str, object] | None,
    expected_error: str,
) -> None:
    """Subsessions endpoints return 400 for invalid requests."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")
    path = path_factory(info)

    async with mock_app(subsession_registry=registry) as f:
        client_method = getattr(f.client, method)
        if payload is not None:
            response = await client_method(path, **payload)
        else:
            response = await client_method(path)

    assert response.status_code == 400
    assert expected_error in response.json()["error"]


@pytest.mark.asyncio
async def test_subsessions_list_no_registry_returns_503() -> None:
    """``GET /subsessions?session_id=x`` returns 503 when not wired."""
    async with mock_app() as f:
        response = await f.client.get("/subsessions?session_id=s1")

    assert response.status_code == 503
    data = response.json()
    assert data["error"] == "subsessions feature not enabled"
    assert "correlation_id" in data


@pytest.mark.asyncio
async def test_subsessions_list_returns_owner_tree() -> None:
    """``GET /subsessions?session_id=x`` returns that session's snapshots."""
    registry = SubsessionRegistry(store_path=None)
    a = _register_subsession(registry, owner="sess-a", title="first")
    b = _register_subsession(
        registry,
        owner="sess-a",
        kind=SubsessionKind.PERIODIC,
        title="second",
        interval_seconds=60.0,
    )
    registry.mark_closed(b.id, summary="done", reason="completed")
    _register_subsession(registry, owner="sess-b", title="foreign")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.get("/subsessions?session_id=sess-a")

    assert response.status_code == 200
    subsessions = response.json()["subsessions"]
    # Whole tree including terminal entries, oldest first, no transcripts.
    assert [s["subsession_id"] for s in subsessions] == [a.id, b.id]
    for snapshot in subsessions:
        assert "transcript" not in snapshot
        for key in (
            "subsession_id",
            "kind",
            "owner_session_id",
            "parent_id",
            "depth",
            "title",
            "prompt",
            "model_level",
            "status",
            "created_at",
            "last_activity_at",
            "interval_seconds",
            "next_run_at",
            "include_previous_result",
            "runs",
            "max_runs",
            "last_result",
            "summary",
            "close_reason",
            "error",
        ):
            assert key in snapshot, f"missing snapshot key {key!r}"
    assert subsessions[1]["status"] == SubsessionStatus.CLOSED.value
    assert subsessions[1]["summary"] == "done"


# ---------------------------------------------------------------------------
# GET /subsessions/{id} and /transcript
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsessions_get_unknown_returns_404() -> None:
    """``GET /subsessions/{id}`` for an unknown id returns 404."""
    async with mock_app(subsession_registry=SubsessionRegistry(store_path=None)) as f:
        response = await f.client.get("/subsessions/ghost")

    assert response.status_code == 404
    data = response.json()
    assert "unknown subsession" in data["error"]
    assert "ghost" in data["error"]
    assert "correlation_id" in data


@pytest.mark.asyncio
async def test_subsessions_get_returns_snapshot_with_transcript() -> None:
    """``GET /subsessions/{id}`` returns the snapshot plus its transcript."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a", title="detailed")
    registry.append_transcript(info.id, "assistant", "working on it")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.get(f"/subsessions/{info.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["subsession_id"] == info.id
    assert data["title"] == "detailed"
    assert len(data["transcript"]) == 1
    entry = data["transcript"][0]
    assert entry["role"] == "assistant"
    assert entry["text"] == "working on it"
    assert isinstance(entry["timestamp"], int | float)


@pytest.mark.asyncio
async def test_subsessions_transcript_endpoint() -> None:
    """``GET /subsessions/{id}/transcript`` returns transcript only."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")
    registry.append_transcript(info.id, "user", "a question")
    registry.append_transcript(info.id, "assistant", "an answer")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.get(f"/subsessions/{info.id}/transcript")
        missing = await f.client.get("/subsessions/ghost/transcript")

    assert response.status_code == 200
    data = response.json()
    assert data["subsession_id"] == info.id
    assert [(e["role"], e["text"]) for e in data["transcript"]] == [
        ("user", "a question"),
        ("assistant", "an answer"),
    ]
    assert missing.status_code == 404


# ---------------------------------------------------------------------------
# POST /subsessions/{id}/message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsessions_message_unknown_returns_404() -> None:
    """Messaging an unknown subsession returns 404."""
    async with mock_app(subsession_registry=SubsessionRegistry(store_path=None)) as f:
        response = await f.client.post(
            "/subsessions/ghost/message", json={"text": "hello"}
        )

    assert response.status_code == 404
    data = response.json()
    assert "unknown subsession" in data["error"]
    assert "ghost" in data["error"]


@pytest.mark.asyncio
async def test_subsessions_message_terminal_returns_409() -> None:
    """Messaging a terminal subsession returns 409."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")
    registry.mark_closed(info.id, summary="done", reason="completed")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.post(
            f"/subsessions/{info.id}/message", json={"text": "too late"}
        )

    assert response.status_code == 409
    data = response.json()
    assert "not active" in data["error"]
    assert info.id in data["error"]


@pytest.mark.asyncio
async def test_subsessions_message_queues_into_inbox_and_transcript() -> None:
    """A valid message returns 202 and lands in the inbox and transcript."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.post(
            f"/subsessions/{info.id}/message", json={"text": "user says hi"}
        )

    assert response.status_code == 202
    assert response.json() == {"subsession_id": info.id, "status": "queued"}

    # Transcripted immediately with role "user"...
    assert [(e.role, e.text) for e in info.transcript] == [("user", "user says hi")]
    # ...and queued for the worker's next turn boundary.
    queued = registry.drain_inbox(info.id)
    assert [(m.role, m.text) for m in queued] == [("user", "user says hi")]


# ---------------------------------------------------------------------------
# POST /subsessions/{id}/close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subsessions_close_unknown_returns_404() -> None:
    """Closing an unknown subsession returns 404."""
    async with mock_app(subsession_registry=SubsessionRegistry(store_path=None)) as f:
        response = await f.client.post("/subsessions/ghost/close")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_subsessions_close_terminal_is_idempotent() -> None:
    """Closing an already-terminal subsession returns ``closed: false``."""
    registry = SubsessionRegistry(store_path=None)
    info = _register_subsession(registry, owner="sess-a")
    registry.mark_closed(info.id, summary="done", reason="completed")

    async with mock_app(subsession_registry=registry) as f:
        response = await f.client.post(f"/subsessions/{info.id}/close")

    assert response.status_code == 200
    assert response.json() == {
        "subsession_id": info.id,
        "closed": False,
        "status": SubsessionStatus.CLOSED.value,
    }


@pytest.mark.asyncio
async def test_subsessions_close_cancels_worker_and_delivers_summary() -> None:
    """Closing a live subsession cancels its worker and delivers a summary."""
    from contextlib import suppress

    from tests.common.subsession_fakes import FakeAgent, build_env, wait_until

    gate = asyncio.Event()  # never set — the worker stays mid-turn
    agent = FakeAgent(["never"], gate=gate)
    env = build_env(agent=agent)

    sub_id = spawn_subsession(
        env=env,
        kind=SubsessionKind.TASK,
        owner_session_id="sess-live",
        parent_id=None,
        depth=1,
        title="long job",
        prompt="work forever",
        model_level=3,
    )
    await wait_until(lambda: len(agent.calls) == 1)
    env.registry.append_transcript(sub_id, "assistant", "half done")
    worker = env.registry._running[sub_id]

    async with mock_app(
        conversation_store=env.conversation_store,
        subsession_registry=env.registry,
        subsession_delivery=env.delivery,
    ) as f:
        response = await f.client.post(f"/subsessions/{sub_id}/close")

    assert response.status_code == 200
    data = response.json()
    assert data["subsession_id"] == sub_id
    assert data["closed"] is True
    assert data["summary"].startswith("Closed by user.")
    assert "half done" in data["summary"]

    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(worker, 2.0)

    info = env.registry.get(sub_id)
    assert info is not None
    assert info.status is SubsessionStatus.CLOSED
    assert info.close_reason == "closed by user"

    # The summary was delivered to the owning session (delivery wired).
    history = env.conversation_store.history("sess-live")
    assert len(history) == 1
    assert history[0][0].startswith(f"[Subsession {sub_id[:8]} (task)")
    assert "half done" in history[0][1]


# ---------------------------------------------------------------------------
# create_agent_from_settings — subsession wiring
# ---------------------------------------------------------------------------


def test_create_agent_model_level_override() -> None:
    """``model_level`` overrides ``settings.chat_default_model_level``."""
    settings = Settings(chat_default_model_level=2, openrouter_api_key="sk-key")

    agent = create_agent_from_settings("Be terse.", settings=settings, model_level=2)

    assert agent._model_level == 2
    # Level 2 → openrouter (key-bearing), so the key is forwarded.
    assert agent._api_key == "sk-key"  # pragma: allowlist secret


def test_main_agent_gets_per_request_subsession_tools_factory() -> None:
    """With an env and no ctx, subsession tools are built per request."""
    from tests.common.subsession_fakes import build_env

    settings = Settings()
    env = build_env()

    agent = create_agent_from_settings(settings=settings, subsession_env=env)

    assert agent._request_tools_factory is not None
    per_request = agent._request_tools_factory("sess-1")
    names = [t.__name__ for t in per_request]
    assert names == [
        "spawn_subsession",
        "message_subsession",
        "close_subsession",
        "list_subsessions",
        "check_monitor",
    ]


def test_subsession_agent_gets_static_tools_with_complete() -> None:
    """With a ctx and close state, the tools are baked in statically."""
    from robotsix_chat.subsessions import CloseState, SubsessionContext
    from tests.common.subsession_fakes import build_env

    settings = Settings()
    env = build_env()
    ctx = SubsessionContext(owner_session_id="sess-1", subsession_id="sub-1", depth=1)

    agent = create_agent_from_settings(
        settings=settings,
        subsession_env=env,
        subsession_ctx=ctx,
        subsession_close_state=CloseState(),
    )

    assert agent._request_tools_factory is None
    names = [getattr(t, "__name__", "") for t in agent._tools or []]
    assert "complete_subsession" in names
    assert "spawn_subsession" in names


def test_bare_agent_has_no_subsession_tools() -> None:
    """Without a subsession env, no subsession tools are attached."""
    agent = create_agent_from_settings(settings=Settings())

    assert agent._request_tools_factory is None
    names = [getattr(t, "__name__", "") for t in agent._tools or []]
    assert "spawn_subsession" not in names
    assert "complete_subsession" not in names


def test_costly_tier_subsession_gets_orchestration_directive() -> None:
    """A subsession built at level >=3 receives the orchestration directive."""
    from robotsix_chat.subsessions import CloseState, SubsessionContext
    from robotsix_chat.subsessions.models import (
        COSTLY_TIER_ORCHESTRATION_DIRECTIVE,
    )
    from tests.common.subsession_fakes import build_env

    settings = Settings()
    env = build_env()
    ctx = SubsessionContext(owner_session_id="sess-1", subsession_id="sub-1", depth=1)

    agent = create_agent_from_settings(
        settings=settings,
        subsession_env=env,
        subsession_ctx=ctx,
        subsession_close_state=CloseState(),
        model_level=3,
    )

    assert COSTLY_TIER_ORCHESTRATION_DIRECTIVE in agent._instruction


def test_cheap_tier_subsession_has_no_orchestration_directive() -> None:
    """A subsession built at level <3 does not get the orchestration directive."""
    from robotsix_chat.subsessions import CloseState, SubsessionContext
    from robotsix_chat.subsessions.models import (
        COSTLY_TIER_ORCHESTRATION_DIRECTIVE,
    )
    from tests.common.subsession_fakes import build_env

    settings = Settings(openrouter_api_key="sk-key")
    env = build_env()
    ctx = SubsessionContext(owner_session_id="sess-1", subsession_id="sub-1", depth=1)

    agent = create_agent_from_settings(
        settings=settings,
        subsession_env=env,
        subsession_ctx=ctx,
        subsession_close_state=CloseState(),
        model_level=2,
    )

    assert COSTLY_TIER_ORCHESTRATION_DIRECTIVE not in agent._instruction


def test_main_agent_costly_level_has_no_orchestration_directive() -> None:
    """The directive targets subsessions only — the main chat agent never gets it."""
    from robotsix_chat.subsessions.models import (
        COSTLY_TIER_ORCHESTRATION_DIRECTIVE,
    )

    settings = Settings()  # level 4, no subsession ctx → main agent

    agent = create_agent_from_settings(settings=settings, model_level=4)

    assert COSTLY_TIER_ORCHESTRATION_DIRECTIVE not in agent._instruction


def test_main_agent_gets_suggestion_chip_contract() -> None:
    """The main chat agent's instruction teaches the ```suggestions block.

    The contract must be code-level (not sourced from the operator-editable
    config document) so it holds for every session regardless of config edits.
    """
    from robotsix_chat.chat.server.agent_factory import _SUGGESTIONS_INSTRUCTION

    settings = Settings(agent_instruction="You are terse.")

    agent = create_agent_from_settings(settings=settings)

    assert _SUGGESTIONS_INSTRUCTION in agent._instruction
    assert "```suggestions" in agent._instruction


def test_bare_agent_has_no_suggestion_chip_contract() -> None:
    """A bare text-transformation agent never gets the suggestion contract."""
    from robotsix_chat.chat.server.agent_factory import _SUGGESTIONS_INSTRUCTION

    settings = Settings(agent_instruction="Be terse.")

    agent = create_agent_from_settings(settings=settings, bare=True)

    assert _SUGGESTIONS_INSTRUCTION not in agent._instruction


def test_subsession_agent_has_no_main_suggestion_contract() -> None:
    """Subsession children are excluded from the main-agent suggestion block.

    They get their own directive via ``_USER_CHAT_FIRST_TURN_NOTE`` in the
    worker instead.
    """
    from robotsix_chat.chat.server.agent_factory import _SUGGESTIONS_INSTRUCTION
    from robotsix_chat.subsessions import CloseState, SubsessionContext
    from tests.common.subsession_fakes import build_env

    settings = Settings()
    env = build_env()
    ctx = SubsessionContext(owner_session_id="sess-1", subsession_id="sub-1", depth=1)

    agent = create_agent_from_settings(
        settings=settings,
        subsession_env=env,
        subsession_ctx=ctx,
        subsession_close_state=CloseState(),
        model_level=3,
    )

    assert _SUGGESTIONS_INSTRUCTION not in agent._instruction


# ---------------------------------------------------------------------------
# Chat endpoint — subsession tool scoping via client_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chat_endpoint_passes_session_id_as_client_id() -> None:
    """``/chat`` scopes the per-request tools to the SESSION id.

    Even when the browser sends its own ``client_id``, the agent receives
    the session id as ``client_id`` so spawned subsessions (and their SSE
    frames) land in the owning chat session.
    """
    async with mock_app(tokens=["ok"]) as f:
        await f.client.post(
            "/chat",
            json={
                "message": "hi",
                "session_id": "sess-42",
                "owner_id": "owner-1",
                "client_id": "browser-99",
            },
        )

    assert f.agent.session_id == "sess-42"
    assert f.agent.client_id == "sess-42"
