"""Class-based tests for the chat SSE server.

Covers per-session message idempotency (``TestMessageIdempotency``) and the
main-agent Langfuse env export (``TestExportLangfuseEnv``). Split out of
``test_server.py`` (module-size).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest
from pydantic import SecretStr

from robotsix_chat.chat.conversation import ConversationStore
from robotsix_chat.chat.server import (
    SSE_DONE_TYPE,
    SSE_ERROR_TYPE,
    SSE_TOKEN_TYPE,
)
from robotsix_chat.chat.server.cli import _export_langfuse_env
from robotsix_chat.config import LangfuseProjectCreds, LangfuseSettings, Settings
from tests.chat.server.test_server import _parse_sse
from tests.conftest import mock_app

# ---------------------------------------------------------------------------
# Message idempotency
# ---------------------------------------------------------------------------


class TestMessageIdempotency:
    """Tests for per-session message idempotency via ``MessageIdempotencyStore``."""

    @pytest.mark.asyncio
    async def test_same_message_id_concurrent(self) -> None:
        """Concurrent POSTs with same message_id → one agent call, both get reply."""
        async with mock_app(tokens=["Hello", " ", "world!"]) as f:

            async def post() -> Any:
                return await f.client.post(
                    "/chat",
                    json={
                        "message": "hi",
                        "message_id": "abc-123",
                        "session_id": "s1",
                        "owner_id": "o1",
                    },
                )

            r1, r2 = await asyncio.gather(post(), post())

        assert f.agent.call_count == 1

        frames1 = _parse_sse(r1)
        frames2 = _parse_sse(r2)

        # Both responses contain a done frame.
        assert any(f["type"] == SSE_DONE_TYPE for f in frames1)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames2)

        # Both contain the same reply text.
        reply1 = "".join(
            str(f["content"]) for f in frames1 if f["type"] == SSE_TOKEN_TYPE
        )
        reply2 = "".join(
            str(f["content"]) for f in frames2 if f["type"] == SSE_TOKEN_TYPE
        )
        assert reply1 == reply2 == "Hello world!"

    @pytest.mark.asyncio
    async def test_same_message_id_sequential(self) -> None:
        """First POST completes; second with same message_id replays from store."""
        async with mock_app(tokens=["Hello", " ", "world!"]) as f:
            r1 = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc-456",
                    "session_id": "s2",
                    "owner_id": "o2",
                },
            )
            r2 = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc-456",
                    "session_id": "s2",
                    "owner_id": "o2",
                },
            )

        # Agent called exactly once (first request only).
        assert f.agent.call_count == 1

        frames1 = _parse_sse(r1)
        frames2 = _parse_sse(r2)

        assert any(f["type"] == SSE_DONE_TYPE for f in frames1)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames2)

        # Second response replays the full reply in a single token frame.
        token_frames2 = [f for f in frames2 if f["type"] == SSE_TOKEN_TYPE]
        assert len(token_frames2) == 1
        assert token_frames2[0]["content"] == "Hello world!"

    @pytest.mark.asyncio
    async def test_distinct_message_ids_run_independently(self) -> None:
        """Two POSTs with different message_ids → two agent invocations."""
        async with mock_app(tokens=["ok"]) as f:
            await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "id-1",
                    "session_id": "s3",
                    "owner_id": "o3",
                },
            )
            await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "id-2",
                    "session_id": "s3",
                    "owner_id": "o3",
                },
            )

        assert f.agent.call_count == 2

    @pytest.mark.asyncio
    async def test_no_message_id_backward_compat(self) -> None:
        """POST without message_id field — agent runs, reply returned, no error."""
        async with mock_app(tokens=["ok"]) as f:
            response = await f.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "session_id": "s4",
                    "owner_id": "o4",
                },
            )

        assert response.status_code == 200
        assert f.agent.call_count == 1
        frames = _parse_sse(response)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames)
        assert any(f["type"] == SSE_TOKEN_TYPE for f in frames)

    @pytest.mark.asyncio
    async def test_retry_after_stream_error_reruns(self) -> None:
        """First POST errors → no completed entry → second POST re-runs agent."""
        # First app instance raises an error.
        async with mock_app(
            error=RuntimeError("boom"),
            tokens=["ok"],  # won't be reached
        ) as f1:
            r1 = await f1.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc",
                    "session_id": "s5",
                    "owner_id": "o5",
                },
            )

        frames1 = _parse_sse(r1)
        assert any(f["type"] == SSE_ERROR_TYPE for f in frames1)
        assert not any(f["type"] == SSE_DONE_TYPE for f in frames1)

        # Second app instance with a non-erroring agent.
        async with mock_app(tokens=["ok"]) as f2:
            r2 = await f2.client.post(
                "/chat",
                json={
                    "message": "hi",
                    "message_id": "abc",
                    "session_id": "s5",
                    "owner_id": "o5",
                },
            )

        assert f2.agent.call_count == 1
        frames2 = _parse_sse(r2)
        assert any(f["type"] == SSE_DONE_TYPE for f in frames2)

    @pytest.mark.asyncio
    async def test_history_read_under_lock(self) -> None:
        """Second message sees the first message's reply in agent.history."""
        store = ConversationStore()

        async with mock_app(tokens=["first reply"], conversation_store=store) as f1:
            await f1.client.post(
                "/chat",
                json={
                    "message": "msg1",
                    "message_id": "id-a",
                    "session_id": "s6",
                    "owner_id": "o6",
                },
            )

        async with mock_app(tokens=["second reply"], conversation_store=store) as f2:
            await f2.client.post(
                "/chat",
                json={
                    "message": "msg2",
                    "message_id": "id-b",
                    "session_id": "s6",
                    "owner_id": "o6",
                },
            )

        # The second call's agent.history must include the first message's reply.
        assert f2.agent.history == [("msg1", "first reply")]


class TestExportLangfuseEnv:
    """Env export for the main-agent Langfuse exporter."""

    _VARS = (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_BASE_URL",
    )

    @pytest.fixture(autouse=True)
    def _restore_langfuse_env(self):
        """Restore the LANGFUSE_* env vars after each test.

        ``_export_langfuse_env`` writes ``os.environ`` directly, which
        monkeypatch does not track — without cleanup the exported values
        leak into later tests that build an agent, making llmio attempt an
        OTLP tracer setup. The module split surfaced this pre-existing leak
        via randomized test ordering.
        """
        yield
        for var in self._VARS:
            os.environ.pop(var, None)

    def test_exports_base_url_and_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both host names are exported: llmio reads LANGFUSE_BASE_URL."""
        for var in self._VARS:
            monkeypatch.delenv(var, raising=False)
        sk = "sk-lf-test"  # pragma: allowlist secret
        settings = Settings(
            langfuse=LangfuseSettings(
                host="https://langfuse.example.com",
                projects={
                    "robotsix-chat": LangfuseProjectCreds(
                        public_key=SecretStr("pk-lf-test"),
                        secret_key=SecretStr(sk),
                    )
                },
            )
        )

        _export_langfuse_env(settings)

        assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-lf-test"
        assert os.environ["LANGFUSE_SECRET_KEY"] == sk
        assert os.environ["LANGFUSE_HOST"] == "https://langfuse.example.com"
        assert os.environ["LANGFUSE_BASE_URL"] == "https://langfuse.example.com"

    def test_noop_without_public_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent credentials leave the environment untouched."""
        for var in self._VARS:
            monkeypatch.delenv(var, raising=False)

        _export_langfuse_env(Settings())

        for var in self._VARS:
            assert var not in os.environ

    def test_overrides_pre_existing_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Config values overwrite pre-existing env vars (setdefault→direct)."""
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-stale")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-stale")
        monkeypatch.setenv("LANGFUSE_HOST", "https://stale.example.com")
        monkeypatch.setenv("LANGFUSE_BASE_URL", "https://stale.example.com")

        sk = "sk-config"  # pragma: allowlist secret
        settings = Settings(
            langfuse=LangfuseSettings(
                host="https://config.example.com",
                projects={
                    "robotsix-chat": LangfuseProjectCreds(
                        public_key=SecretStr("pk-config"),
                        secret_key=SecretStr(sk),
                    )
                },
            )
        )

        _export_langfuse_env(settings)

        assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-config"
        assert os.environ["LANGFUSE_SECRET_KEY"] == sk
        assert os.environ["LANGFUSE_HOST"] == "https://config.example.com"
        assert os.environ["LANGFUSE_BASE_URL"] == "https://config.example.com"
