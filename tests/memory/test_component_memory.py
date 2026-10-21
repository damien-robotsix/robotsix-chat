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


# Real shape of GET /health/hindsight when the retain path is failing —
# mirrors robotsix-memory #85 (balance exhausted since 2026-09-24).
HINDSIGHT_FAILING = {
    "status": "degraded",
    "hindsight": "ok",
    "retain": {
        "status": "failing",
        "last_error": "This request requires more credits",
        "banks": {
            "fleet-operator": {
                "status": "failing",
                "last_write_at": "2026-09-17T15:13:07+00:00",
                "failed": 10,
                "checked": 10,
            }
        },
    },
}

HINDSIGHT_HEALTHY = {
    "status": "ok",
    "hindsight": "ok",
    "retain": {"status": "ok", "last_error": None, "banks": {}},
}


@respx.mock
async def test_write_health_failing_flips_status() -> None:
    respx.get(f"{URL}/health/hindsight").mock(
        return_value=httpx.Response(200, json=HINDSIGHT_FAILING)
    )
    memory = ComponentMemory(URL)
    assert memory.status()["degraded"] is False  # unknown until first read
    await memory.refresh_write_health()
    status = memory.status()
    assert status["degraded"] is True
    assert status["writes_failing"] is True
    assert "requires more credits" in status["write_last_error"]
    assert "requires more credits" in status["reason"]


@respx.mock
async def test_write_health_healthy_keeps_ok() -> None:
    respx.get(f"{URL}/health/hindsight").mock(
        return_value=httpx.Response(200, json=HINDSIGHT_HEALTHY)
    )
    memory = ComponentMemory(URL)
    await memory.refresh_write_health()
    status = memory.status()
    assert status["degraded"] is False
    assert status["writes_failing"] is False
    assert status["write_last_error"] is None


@respx.mock
async def test_write_health_unreachable_stays_unknown() -> None:
    respx.get(f"{URL}/health/hindsight").mock(side_effect=httpx.ConnectError("down"))
    memory = ComponentMemory(URL)
    assert await memory.refresh_write_health() is None
    assert memory.status()["writes_failing"] is False


@respx.mock
async def test_write_alert_opens_once_per_cause_and_closes_on_recovery() -> None:
    opened: list[str] = []
    closed: list[bool] = []

    async def _on_failing(last_error: str) -> None:
        opened.append(last_error)

    async def _on_recovered() -> None:
        closed.append(True)

    memory = ComponentMemory(URL)
    memory.set_write_alert_callbacks(on_failing=_on_failing, on_recovered=_on_recovered)

    route = respx.get(f"{URL}/health/hindsight")

    # Three failing reads, same cause -> opened exactly once.
    route.mock(return_value=httpx.Response(200, json=HINDSIGHT_FAILING))
    for _ in range(3):
        await memory.refresh_write_health()
    assert opened == ["This request requires more credits"]
    assert closed == []

    # Recovery -> closed once.
    route.mock(return_value=httpx.Response(200, json=HINDSIGHT_HEALTHY))
    await memory.refresh_write_health()
    assert closed == [True]

    # Failing again after recovery -> re-opens.
    route.mock(return_value=httpx.Response(200, json=HINDSIGHT_FAILING))
    await memory.refresh_write_health()
    assert opened == [
        "This request requires more credits",
        "This request requires more credits",
    ]


@respx.mock
async def test_write_alert_reopens_on_distinct_cause() -> None:
    opened: list[str] = []

    async def _on_failing(last_error: str) -> None:
        opened.append(last_error)

    memory = ComponentMemory(URL)
    memory.set_write_alert_callbacks(on_failing=_on_failing, on_recovered=None)
    route = respx.get(f"{URL}/health/hindsight")

    route.mock(return_value=httpx.Response(200, json=HINDSIGHT_FAILING))
    await memory.refresh_write_health()
    other = {
        "status": "degraded",
        "retain": {"status": "failing", "last_error": "upstream 503"},
    }
    route.mock(return_value=httpx.Response(200, json=other))
    await memory.refresh_write_health()
    assert opened == ["This request requires more credits", "upstream 503"]


async def test_build_memory_prefers_component() -> None:
    reset_build_memory_cache()
    memory = build_memory(MemoryComponentSettings(enabled=True, url=URL))
    assert isinstance(memory, ComponentMemory)
    # Same settings -> cached instance.
    again = build_memory(MemoryComponentSettings(enabled=True, url=URL))
    assert again is memory
    reset_build_memory_cache()


async def test_build_memory_component_disabled_is_null() -> None:
    reset_build_memory_cache()
    from robotsix_chat.memory import NullMemory

    assert isinstance(build_memory(MemoryComponentSettings(enabled=False)), NullMemory)
    assert isinstance(build_memory(None), NullMemory)
    reset_build_memory_cache()
