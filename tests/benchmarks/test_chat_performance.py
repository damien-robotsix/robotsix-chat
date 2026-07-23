"""Microbenchmarks for the chat server's critical request paths."""

from __future__ import annotations

import asyncio
import base64


class TestChatEndpointPerformance:
    """Benchmarks for the SSE chat endpoint (the main user-facing path)."""

    def test_chat_sse_stream_short(self, benchmark):
        """A short 3-token reply: the most common case."""
        from tests.conftest import mock_app

        async def _run():
            async with mock_app() as f:  # noqa: SIM117
                async with f.client.stream(
                    "POST", "/chat", json={"message": "Hi"}
                ) as resp:
                    async for _ in resp.aiter_bytes():
                        pass
                    return resp.status_code

        status = benchmark(lambda: asyncio.run(_run()))
        assert status == 200

    def test_chat_sse_stream_long(self, benchmark):
        """A 100-token reply: stress the SSE frame generator without an LLM."""
        from tests.conftest import mock_app

        tokens = ["token "] * 100

        async def _run():
            async with mock_app(tokens=tokens) as f:  # noqa: SIM117
                async with f.client.stream(
                    "POST",
                    "/chat",
                    json={"message": "Write 100 tokens"},
                ) as resp:
                    async for _ in resp.aiter_bytes():
                        pass
                    return resp.status_code

        status = benchmark(lambda: asyncio.run(_run()))
        assert status == 200

    def test_chat_with_image(self, benchmark):
        """SSE streaming with an image attached (medium payload)."""
        from tests.conftest import mock_app

        small_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk",
        )

        async def _run():
            async with mock_app() as f:
                payload = {
                    "message": "What's in this image?",
                    "images": [["image/png", small_png]],
                }
                async with f.client.stream("POST", "/chat", json=payload) as resp:
                    async for _ in resp.aiter_bytes():
                        pass
                    return resp.status_code

        status = benchmark(lambda: asyncio.run(_run()))
        assert status == 200

    def test_chat_validation_overhead(self, benchmark):
        """The 400-path: measure the cost of rejecting an invalid payload."""
        from tests.conftest import mock_app

        async def _run():
            async with mock_app() as f:
                resp = await f.client.post("/chat", json={})
                return resp.status_code

        status = benchmark(lambda: asyncio.run(_run()))
        assert status == 400


class TestInfrastructure:
    """Benchmarks for non-chat paths that affect overall request latency."""

    def test_health_endpoint(self, benchmark):
        """The /health probe — should be sub-millisecond."""
        from tests.conftest import mock_app

        async def _run():
            async with mock_app() as f:
                resp = await f.client.get("/health")
                return resp.status_code

        status = benchmark(lambda: asyncio.run(_run()))
        assert status == 200

    def test_ui_static(self, benchmark):
        """Static file serving for the chat UI."""
        from tests.conftest import mock_app

        async def _run():
            async with mock_app() as f:
                resp = await f.client.get("/")
                return resp.status_code

        status = benchmark(lambda: asyncio.run(_run()))
        assert status == 200
