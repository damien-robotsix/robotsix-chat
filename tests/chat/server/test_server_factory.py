"""Factory-wiring tests for the chat SSE server.

Covers image attachment handling, ``create_agent_from_settings``,
``run_server_from_config`` and the server error handlers. Split out of
``test_server.py`` (module-size).
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest
from pydantic import SecretStr

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server import create_agent_from_settings, run_server_from_config
from robotsix_chat.config import Settings
from robotsix_chat.config.models import (
    DirectRepoSettings,
    GitHubActionsSettings,
    GitHubSecuritySettings,
    MemoryComponentSettings,
)
from robotsix_chat.llm import LlmioChatAgent
from robotsix_chat.memory import NullMemory
from robotsix_chat.subsessions import SubsessionRegistry
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Chat endpoint — image attachments
# ---------------------------------------------------------------------------


def _png_pixel() -> bytes:
    """Smallest valid PNG — 1×1 red pixel."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5"
        "+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    )


_PNG_B64 = base64.b64encode(_png_pixel()).decode()
"""Pre-computed base64 of the 1x1 red pixel PNG — used in parametrized tests."""

_OVERSIZED_B64 = base64.b64encode(b"x" * 50).decode()
"""A 50-byte payload encoded as base64 — used for the oversized-image test."""


@pytest.mark.asyncio
async def test_chat_endpoint_passes_images_to_agent() -> None:
    """A request with a valid ``images`` array reaches the agent with decoded bytes."""
    png_bytes = _png_pixel()
    data_b64 = base64.b64encode(png_bytes).decode()

    async with mock_app(tokens=["ok"]) as f:
        response = await f.client.post(
            "/chat",
            json={
                "message": "look at this",
                "images": [{"media_type": "image/png", "data": data_b64}],
            },
        )

    assert response.status_code == 200
    assert f.agent.called_with == "look at this"
    assert f.agent.images == [("image/png", png_bytes)]


@pytest.mark.asyncio
async def test_chat_endpoint_images_only_no_message() -> None:
    """A request with only images (no text) is accepted."""
    png_bytes = _png_pixel()
    data_b64 = base64.b64encode(png_bytes).decode()

    async with mock_app(tokens=["seen it"]) as f:
        response = await f.client.post(
            "/chat",
            json={"images": [{"media_type": "image/png", "data": data_b64}]},
        )

    assert response.status_code == 200
    # message passed through as empty string
    assert f.agent.called_with == ""
    assert f.agent.images == [("image/png", png_bytes)]


@pytest.mark.asyncio
async def test_chat_endpoint_multiple_images() -> None:
    """Multiple valid images are all decoded and forwarded."""
    png_bytes = _png_pixel()
    data_b64 = base64.b64encode(png_bytes).decode()

    async with mock_app(tokens=["ok"]) as f:
        await f.client.post(
            "/chat",
            json={
                "message": "compare",
                "images": [
                    {"media_type": "image/png", "data": data_b64},
                    {"media_type": "image/jpeg", "data": data_b64},
                ],
            },
        )

    assert f.agent.images == [
        ("image/png", png_bytes),
        ("image/jpeg", png_bytes),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_substring", "mock_app_kwargs"),
    [
        pytest.param({"json": {}}, "message", {}, id="neither_message_nor_images"),
        pytest.param(
            {"json": {"message": "hi", "images": "not-a-list"}},
            "array",
            {},
            id="images_not_a_list",
        ),
        pytest.param(
            {
                "json": {
                    "message": "hi",
                    "images": [
                        {"media_type": "image/png", "data": _PNG_B64},
                        {"media_type": "image/png", "data": _PNG_B64},
                        {"media_type": "image/png", "data": _PNG_B64},
                    ],
                }
            },
            "too many images",
            {"max_images_per_message": 2},
            id="too_many_images",
        ),
        pytest.param(
            {
                "json": {
                    "message": "hi",
                    "images": [{"media_type": "image/png", "data": _OVERSIZED_B64}],
                }
            },
            "exceeds maximum",
            {"max_image_bytes": 40},
            id="oversized_image",
        ),
        pytest.param(
            {
                "json": {
                    "message": "hi",
                    "images": [
                        {
                            "media_type": "image/bmp",
                            "data": base64.b64encode(b"fake").decode(),
                        }
                    ],
                }
            },
            "not allowed",
            {},
            id="disallowed_media_type",
        ),
        pytest.param(
            {
                "json": {
                    "message": "hi",
                    "images": [{"media_type": "image/png", "data": "!!!not-base64!!!"}],
                }
            },
            "not valid base64",
            {},
            id="non_base64_data",
        ),
        pytest.param(
            {
                "json": {
                    "message": "hi",
                    "images": [{"data": base64.b64encode(b"x").decode()}],
                }
            },
            "media_type",
            {},
            id="missing_media_type",
        ),
        pytest.param(
            {"json": {"message": "hi", "images": [{"media_type": "image/png"}]}},
            "data",
            {},
            id="missing_data",
        ),
        pytest.param(
            {"json": {"message": "hi", "images": ["not-a-dict"]}},
            "expected a JSON object",
            {},
            id="image_entry_not_a_dict",
        ),
    ],
)
async def test_chat_endpoint_rejects_invalid_image_input(
    payload: dict[str, object],
    expected_substring: str,
    mock_app_kwargs: dict[str, object],
) -> None:
    """The /chat endpoint returns 400 for invalid image input."""
    async with mock_app(**mock_app_kwargs) as f:
        response = await f.client.post("/chat", **payload)

    assert response.status_code == 400
    assert expected_substring in response.json()["error"]


# ---------------------------------------------------------------------------
# create_agent_from_settings
# ---------------------------------------------------------------------------


def test_create_agent_from_settings_explicit() -> None:
    """``create_agent_from_settings`` wires llmio fields from a ``Settings``."""
    settings = Settings(
        chat_default_model_level=2, openrouter_api_key="sk-from-settings"
    )

    agent = create_agent_from_settings("Be concise.", settings=settings)

    assert isinstance(agent, LlmioChatAgent)
    assert agent._model_level == 2
    # Default-enabled capabilities append skill supplements; the base
    # instruction is still wired in as the prefix.
    assert agent._instruction.startswith("Be concise.")
    # Level 2 → openrouter (key-bearing), so the key is forwarded.
    assert agent._api_key == "sk-from-settings"  # pragma: allowlist secret


def test_create_agent_from_settings_forwards_anthropic_key() -> None:
    """``anthropic_api_key`` reaches the agent for the direct Anthropic slot."""
    settings = Settings(
        anthropic_api_key=SecretStr("sk-ant-from-settings")  # pragma: allowlist secret
    )

    agent = create_agent_from_settings("Be concise.", settings=settings)

    assert (
        agent._anthropic_api_key == "sk-ant-from-settings"  # pragma: allowlist secret
    )


def test_create_agent_from_settings_keyless_level_drops_key() -> None:
    """The default level constructs with no api_key (keyless default slot)."""
    settings = Settings()  # model_level 2, keyless default slot

    agent = create_agent_from_settings("Be helpful.", settings=settings)

    assert isinstance(agent, LlmioChatAgent)
    assert agent._model_level == 2
    assert agent._api_key == ""


def test_create_agent_from_settings_instruction_from_config() -> None:
    """A ``None`` instruction falls back to ``settings.agent_instruction``."""
    settings = Settings(agent_instruction="You are terse.")

    agent = create_agent_from_settings(settings=settings)

    # Default-enabled capabilities append skill supplements; the base
    # instruction from config is still wired in as the prefix.
    assert agent._instruction.startswith("You are terse.")


def test_create_agent_from_settings_bare_skips_memory_and_tools() -> None:
    """``bare=True`` yields a NullMemory, tool-less agent with a bare instruction.

    Regression test: the summary agent (built with ``bare=True``) must never
    pay for cross-session memory recall or agentic tool access — a
    production incident showed ``ChatMemory.recall()`` alone taking 90+
    seconds, dwarfing the actual (cheap-tier) model call for a single
    bounded text-transformation task.
    """
    settings = Settings(agent_instruction="Be terse.")

    agent = create_agent_from_settings(settings=settings, bare=True)

    assert isinstance(agent._memory, NullMemory)
    assert agent._tools == []
    assert agent._instruction.startswith("Be terse.")


def test_create_agent_from_settings_memory_disabled_skips_cognee() -> None:
    """``memory_enabled=False`` yields a NullMemory without building cognee.

    Background agents (subsession workers, periodic session turns) run
    unattended and would otherwise recall + cognify on every turn around the
    clock — the cost bleed this gate exists to stop.  Unlike ``bare``, tools
    and subsession wiring are retained; only long-term memory is dropped.
    """
    settings = Settings(
        agent_instruction="Be terse.",
        memory_component={"enabled": False},
    )

    with patch(
        "robotsix_chat.chat.server.agent_factory.build_memory",
        side_effect=AssertionError("build_memory must not be called when gated"),
    ):
        agent = create_agent_from_settings(settings=settings, memory_enabled=False)

    assert isinstance(agent._memory, NullMemory)


def test_create_agent_from_settings_gated_memory_still_reads_component() -> None:
    """With the memory component enabled, a gated agent gets read-only recall.

    Component recall is a cheap HTTP lookup (no LLM pipeline), so unattended
    agents keep the context the main conversation already learned; writes
    stay off (ComponentMemory writes are no-ops anyway).
    """
    settings = Settings(agent_instruction="Be terse.")
    sentinel = NullMemory()

    with patch(
        "robotsix_chat.chat.server.agent_factory.build_memory", return_value=sentinel
    ) as build:
        agent = create_agent_from_settings(settings=settings, memory_enabled=False)

    build.assert_called_once_with(settings.memory_component)
    from robotsix_chat.memory import ReadOnlyMemory

    assert isinstance(agent._memory, ReadOnlyMemory)


def test_create_agent_from_settings_memory_enabled_builds_memory() -> None:
    """The default (``memory_enabled=True``) routes through ``build_memory``.

    Guards against a gate that accidentally disables memory for the
    interactive main-chat agent too.
    """
    settings = Settings(agent_instruction="Be terse.")
    sentinel = NullMemory()

    with patch(
        "robotsix_chat.chat.server.agent_factory.build_memory", return_value=sentinel
    ) as build:
        agent = create_agent_from_settings(settings=settings)

    build.assert_called_once_with(settings.memory_component)
    assert agent._memory is sentinel


def test_list_available_tools_present_in_tool_suite() -> None:
    """The ``list_available_tools`` tool is included in the non-bare agent."""
    settings = Settings(agent_instruction="Be terse.")

    agent = create_agent_from_settings(settings=settings, bare=False)

    assert agent._tools is not None
    tool_names = [getattr(t, "__name__", "") for t in agent._tools]
    assert "list_available_tools" in tool_names


@pytest.mark.asyncio
async def test_list_available_tools_returns_tool_names() -> None:
    """``list_available_tools`` returns a formatted list of tool names."""
    settings = Settings(agent_instruction="Be terse.")

    agent = create_agent_from_settings(settings=settings, bare=False)

    assert agent._tools is not None
    tool = next(
        t for t in agent._tools if getattr(t, "__name__", "") == "list_available_tools"
    )

    result = await tool()

    assert "Available tools" in result
    assert "list_available_tools" in result
    # Verify the count matches.
    count_str = result.split("(")[1].split(")")[0]
    assert count_str.isdigit()


@pytest.mark.asyncio
async def test_list_available_tools_query_filters_names() -> None:
    """``query`` narrows the roster; an unmatched query says so.

    A board-gates-drain turn sent ``query`` on 2026-09-08 and was
    schema-rejected for it.
    """
    settings = Settings(agent_instruction="Be terse.")
    agent = create_agent_from_settings(settings=settings, bare=False)
    assert agent._tools is not None
    tool = next(
        t for t in agent._tools if getattr(t, "__name__", "") == "list_available_tools"
    )
    result = await tool(query="list_available")
    assert result.startswith("Tools matching 'list_available'")
    assert "list_available_tools" in result
    assert "No tools match" in await tool(query="zzz_no_such_tool")


def test_list_available_tools_absent_in_bare_agent() -> None:
    """``bare=True`` agents have no tools, including ``list_available_tools``."""
    settings = Settings(agent_instruction="Be terse.")

    agent = create_agent_from_settings(settings=settings, bare=True)

    assert agent._tools == []


@pytest.mark.asyncio
async def test_create_agent_from_settings_uses_load_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_agent_from_settings`` resolves config when *settings* is None."""
    # Patch load_config to return a Settings with desired values.
    from robotsix_chat.config import Settings

    with patch(
        "robotsix_chat.config.settings.load_config",
        return_value=Settings(
            chat_default_model_level=1, openrouter_api_key=SecretStr("sk-env-test")
        ),
    ):
        result = create_agent_from_settings("Helpful bot.")

    assert isinstance(result, LlmioChatAgent)
    assert result._model_level == 1
    assert result._api_key == "sk-env-test"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# run_server_from_config — LLM wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_server_from_config_creates_agent_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create an ``LlmioChatAgent`` from ``Settings``.

    ``run_server_from_config()`` with no *agent* argument creates the
    agent and forwards server options.
    """
    from robotsix_chat.config import Settings

    with (
        patch(
            "robotsix_chat.config.settings.load_config",
            return_value=Settings(
                chat_default_model_level=2,
                server_host="127.0.0.1",
                server_port=8080,
            ),
        ),
        patch("robotsix_chat.chat.server.run_server") as mock_run_server,
    ):
        run_server_from_config()

        call_args = mock_run_server.call_args
        passed_agent = call_args[0][0]
        assert isinstance(passed_agent, LlmioChatAgent)
        assert passed_agent._model_level == 2
        assert passed_agent._instruction.startswith("You are a helpful assistant.")
        # A conversation store is built from settings and forwarded; assert the
        # rest of the server options explicitly.
        conversation_store = call_args[1].pop("conversation_store")
        assert isinstance(conversation_store, ConversationStore)
        event_bus = call_args[1].pop("event_bus")
        from robotsix_chat.chat.events import EventBus

        assert isinstance(event_bus, EventBus)
        run_serializer = call_args[1].pop("run_serializer")
        from robotsix_chat.chat.server import RunSerializer

        assert isinstance(run_serializer, RunSerializer)
        subsession_registry = call_args[1].pop("subsession_registry")
        assert isinstance(subsession_registry, SubsessionRegistry)
        subsession_delivery = call_args[1].pop("subsession_delivery")
        from robotsix_chat.subsessions import ParentDelivery

        assert isinstance(subsession_delivery, ParentDelivery)
        on_startup = call_args[1].pop("on_startup")
        assert callable(on_startup)
        summary_agent = call_args[1].pop("summary_agent")
        assert isinstance(summary_agent, LlmioChatAgent)
        direct_repo_settings = call_args[1].pop("direct_repo_settings")
        assert isinstance(direct_repo_settings, DirectRepoSettings)
        github_security_settings = call_args[1].pop("github_security_settings")
        assert isinstance(github_security_settings, GitHubSecuritySettings)
        github_actions_settings = call_args[1].pop("github_actions_settings")
        assert isinstance(github_actions_settings, GitHubActionsSettings)
        central_deploy_settings = call_args[1].pop("central_deploy_settings")
        from robotsix_chat.config.models import CentralDeploySettings

        assert isinstance(central_deploy_settings, CentralDeploySettings)
        feedback_runner = call_args[1].pop("feedback_runner")
        assert feedback_runner is None
        periodic_definitions = call_args[1].pop("periodic_definitions")
        assert periodic_definitions == []  # no presets in the default config
        periodic_agent_factory = call_args[1].pop("periodic_agent_factory")
        assert callable(periodic_agent_factory)
        on_startup_async = call_args[1].pop("on_startup_async")
        assert callable(on_startup_async)
        on_shutdown = call_args[1].pop("on_shutdown")
        assert callable(on_shutdown)
        diagnostic_store = call_args[1].pop("diagnostic_store")
        from robotsix_chat.diagnostics import DiagnosticStore

        assert isinstance(diagnostic_store, DiagnosticStore)
        knowledge_store = call_args[1].pop("knowledge_store")
        from robotsix_chat.knowledge.store import KnowledgeStore

        assert isinstance(knowledge_store, KnowledgeStore)
        continuation_store = call_args[1].pop("continuation_store")
        from robotsix_chat.continuation.store import ContinuationStore

        assert isinstance(continuation_store, ContinuationStore)
        from robotsix_chat.config.models import (
            EvergoingSettings,
            HealthSettings,
            MobileAuthSettings,
        )

        mobile_auth = call_args[1].pop("mobile_auth")
        assert isinstance(mobile_auth, MobileAuthSettings)
        assert call_args[1] == {
            "host": "127.0.0.1",
            "port": 8080,
            "idle_timeout_minutes": 30,
            "max_images_per_message": 8,
            "max_image_bytes": 5_242_880,
            "allowed_image_media_types": [
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
            ],
            "cors_allow_origins": [],
            "correlation_id_header": "X-Request-ID",
            "health_settings": HealthSettings(
                enabled=True, check_interval_seconds=300.0
            ),
            "evergoing_settings": EvergoingSettings(
                trim_interval_seconds=1800.0, keep_recent_runs=5
            ),
            "memory_component_settings": MemoryComponentSettings(),
        }


@pytest.mark.asyncio
async def test_run_server_from_config_passes_explicit_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forward an explicit agent to ``run_server``.

    ``run_server_from_config(agent)`` passes *agent* through without
    creating a new one.
    """
    # Patch load_config to use defaults (level 4 = keyless, no key needed).
    with patch(
        "robotsix_chat.config.settings.load_config",
        return_value=Settings(),
    ):
        mock_agent = MagicMock()

        with patch("robotsix_chat.chat.server.run_server") as mock_run_server:
            run_server_from_config(mock_agent)

        mock_run_server.assert_called_once()
        passed_agent = mock_run_server.call_args[0][0]
        assert passed_agent is mock_agent


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_error_handler_returns_json_500() -> None:
    """An unhandled exception in an endpoint returns a JSON 500 response."""
    async with mock_app(raise_app_exceptions=False) as f:
        with patch(
            "robotsix_chat.chat.server._load_ui_html",
            side_effect=RuntimeError("UI resource missing"),
        ):
            response = await f.client.get("/")

    assert response.status_code == 500
    data = response.json()
    assert data["error"] == "internal server error"
    assert "correlation_id" in data


# ---------------------------------------------------------------------------
# Unknown routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_route_returns_404_json() -> None:
    """Return a JSON 404 for unknown routes."""
    async with mock_app() as f:
        response = await f.client.get("/nonexistent")

    assert response.status_code == 404
    data = response.json()
    assert data["error"] == "not found"
    assert "correlation_id" in data


@pytest.mark.asyncio
async def test_wrong_method_on_known_route_returns_405() -> None:
    """Return 405 when using the wrong HTTP method on a known route."""
    async with mock_app() as f:
        # POST /health is not a valid endpoint — Starlette returns 405.
        response = await f.client.post("/health")

    assert response.status_code == 405


# ---------------------------------------------------------------------------
# Error envelope consistency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_envelope_top_level_keys_consistent() -> None:
    """A 400, 404, forced 500, and validation error all share the same top-level keys.

    The error envelope must always contain ``error`` (a string message) and
    ``correlation_id`` (a string, possibly empty in tests) so API clients
    never have to branch on which key appeared.
    """
    expected_keys = {"error", "correlation_id"}

    async with mock_app(raise_app_exceptions=False) as f:
        # -- 400: invalid JSON body ---------------------------------------
        r400 = await f.client.post(
            "/chat",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert r400.status_code == 400
        d400 = r400.json()
        assert set(d400.keys()) == expected_keys
        assert isinstance(d400["error"], str)

        # -- 404: unknown route -------------------------------------------
        r404 = await f.client.get("/nonexistent")
        assert r404.status_code == 404
        d404 = r404.json()
        assert set(d404.keys()) == expected_keys
        assert isinstance(d404["error"], str)

        # -- 500: forced internal error -----------------------------------
        with patch(
            "robotsix_chat.chat.server._load_ui_html",
            side_effect=RuntimeError("boom"),
        ):
            r500 = await f.client.get("/")
        assert r500.status_code == 500
        d500 = r500.json()
        assert set(d500.keys()) == expected_keys
        assert isinstance(d500["error"], str)
