"""Tests for the robotsix-memory ComponentMemory backend.

Fixture shapes mirror the LIVE component response captured 2026-09-04
(wrapper passes the Hindsight engine payload through under ``results``).
"""

from __future__ import annotations

import httpx
import pytest
import respx

from robotsix_chat.config.models import MemoryComponentSettings
from robotsix_chat.memory import build_memory, reset_build_memory_cache
from robotsix_chat.memory.component import ComponentMemory, _render_recall_block

pytestmark = pytest.mark.asyncio

URL = "http://memory:8080"

# Trimmed live shape from the deployed component (2026-09-04).
LIVE_RECALL_PAYLOAD = {
    "owner_id": "operator",
    "results": {
        "results": [
            {
                "id": "eed3c47a",
                "text": (
                    "The robotsix fleet migrated its long-term memory from "
                    "cognee to the robotsix-memory component on 2026-09-04."
                ),
                "type": "world",
                "entities": ["robotsix fleet", "cognee"],
            }
        ],
        "entities": {
            "cognee": {
                "entity_id": "cee59b94",
                "canonical_name": "cognee",
                "observations": [
                    {"text": "cognee was retired after repeated crashes."}
                ],
            }
        },
    },
}


@respx.mock
async def test_recall_renders_live_shape() -> None:
    respx.get(f"{URL}/recall").mock(
        return_value=httpx.Response(200, json=LIVE_RECALL_PAYLOAD)
    )
    memory = ComponentMemory(URL)
    block = await memory.recall("what happened to cognee?")
    assert "migrated its long-term memory" in block
    assert "[world]" in block
    assert "Consolidated observations:" in block
    assert "cognee: cognee was retired" in block
    assert memory.status()["degraded"] is False


@respx.mock
async def test_recall_sends_operator_scope_and_limit() -> None:
    route = respx.get(f"{URL}/recall").mock(
        return_value=httpx.Response(200, json={"results": {"results": []}})
    )
    memory = ComponentMemory(URL, recall_limit=8)
    block = await memory.recall("query text")
    assert block == ""
    params = route.calls[0].request.url.params
    assert params["owner_id"] == "operator"
    assert params["limit"] == "8"


@respx.mock
async def test_recall_failure_degrades_after_three() -> None:
    respx.get(f"{URL}/recall").mock(side_effect=httpx.ConnectError("down"))
    memory = ComponentMemory(URL)
    for _ in range(3):
        assert await memory.recall("q") == ""
    status = memory.status()
    assert status["degraded"] is True
    assert status["consecutive_recall_failures"] == 3


@respx.mock
async def test_recall_success_resets_failure_streak() -> None:
    memory = ComponentMemory(URL)
    respx.get(f"{URL}/recall").mock(side_effect=httpx.ConnectError("down"))
    await memory.recall("q")
    respx.get(f"{URL}/recall").mock(
        return_value=httpx.Response(200, json={"results": {"results": []}})
    )
    await memory.recall("q")
    assert memory.status()["degraded"] is False
    assert memory.status()["consecutive_recall_failures"] == 0


async def test_remember_is_noop() -> None:
    memory = ComponentMemory(URL)
    assert await memory.remember("user", "assistant") is None


@respx.mock
async def test_recall_deep_uses_reflect() -> None:
    route = respx.post(f"{URL}/reflect").mock(
        return_value=httpx.Response(
            200,
            json={"owner_id": "operator", "reflection": {"answer": "Reasoned answer."}},
        )
    )
    memory = ComponentMemory(URL)
    out = await memory.recall_deep("why did the fleet migrate?")
    assert out == "Reasoned answer."
    import json as _json

    body = _json.loads(route.calls[0].request.content)
    assert body["owner_id"] == "operator"


@respx.mock
async def test_recall_deep_failure_returns_empty() -> None:
    respx.post(f"{URL}/reflect").mock(return_value=httpx.Response(502, json={}))
    memory = ComponentMemory(URL)
    assert await memory.recall_deep("q") == ""


async def test_render_unknown_shape_is_empty() -> None:
    assert _render_recall_block({"weird": True}) == ""
    assert _render_recall_block(None) == ""
    assert _render_recall_block({"results": "nope"}) == ""


async def test_build_memory_prefers_component(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_build_memory_cache()
    from robotsix_chat.config import MemorySettings

    memory = build_memory(
        MemorySettings(enabled=True),
        memory_component=MemoryComponentSettings(enabled=True, url=URL),
    )
    assert isinstance(memory, ComponentMemory)
    # Same settings -> cached instance.
    again = build_memory(
        MemorySettings(enabled=True),
        memory_component=MemoryComponentSettings(enabled=True, url=URL),
    )
    assert again is memory
    reset_build_memory_cache()


async def test_build_memory_component_disabled_falls_through() -> None:
    reset_build_memory_cache()
    from robotsix_chat.config import MemorySettings
    from robotsix_chat.memory import NullMemory

    memory = build_memory(
        MemorySettings(enabled=False),
        memory_component=MemoryComponentSettings(enabled=False),
    )
    assert isinstance(memory, NullMemory)
    reset_build_memory_cache()
