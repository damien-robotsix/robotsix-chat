"""Tests for the memory layer.

Covers :func:`build_memory`, ``NullMemory``, and the cognee backend's
graceful-degradation contract (cognee mocked, never imported for real).
"""

from __future__ import annotations

import base64
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import SecretStr

from robotsix_chat.config import (
    MemoryEmbeddingSettings,
    MemoryLlmSettings,
    MemorySettings,
)
from robotsix_chat.memory import NullMemory, build_memory
from robotsix_chat.memory.cognee import CogneeMemory, _format_results


def _enabled_settings(data_dir: str = "/data/cognee") -> MemorySettings:
    """Return a valid enabled MemorySettings with key and endpoint present."""
    return MemorySettings(
        enabled=True,
        data_dir=data_dir,
        llm=MemoryLlmSettings(api_key="sk-or-x"),  # pragma: allowlist secret
        embedding=MemoryEmbeddingSettings(endpoint="http://box:11434/v1"),
    )


# ---------------------------------------------------------------------------
# build_memory selection
# ---------------------------------------------------------------------------


def test_build_memory_disabled_returns_null() -> None:
    """Disabled memory yields a NullMemory regardless of other fields."""
    assert isinstance(build_memory(MemorySettings(enabled=False)), NullMemory)


def test_build_memory_enabled_without_cognee_returns_null(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cognee extra is absent, enabled memory degrades to NullMemory."""
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert isinstance(build_memory(_enabled_settings()), NullMemory)


def test_build_memory_enabled_with_cognee_returns_cognee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When cognee is importable, enabled memory yields a CogneeMemory."""
    import importlib.util

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "cognee" else None,
    )
    assert isinstance(build_memory(_enabled_settings()), CogneeMemory)


# ---------------------------------------------------------------------------
# NullMemory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_null_memory_is_inert() -> None:
    """Verify that NullMemory stores and recalls nothing."""
    mem = NullMemory()
    await mem.setup()
    await mem.remember("u", "a")
    assert await mem.recall("anything") == ""


# ---------------------------------------------------------------------------
# _format_results
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        ([], ""),
        (["France"], "France"),
        (["a", "", "b"], "a\nb"),
        ("plain", "plain"),
    ],
)
def test_format_results(value: Any, expected: str) -> None:
    """Verify _format_results produces the expected output for given input."""
    assert _format_results(value) == expected


def test_format_results_truncates() -> None:
    """Verify _format_results truncates output exceeding the max length."""
    out = _format_results(["x" * 9000])
    assert len(out) <= 4001
    assert out.endswith("…")


# ---------------------------------------------------------------------------
# CogneeMemory graceful degradation (cognee mocked)
# ---------------------------------------------------------------------------


def _install_fake_cognee(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a stub ``cognee`` module (awaitable mocks) and return it."""
    fake: Any = types.ModuleType("cognee")
    fake.config = MagicMock()

    class _SearchType:
        GRAPH_COMPLETION = "GRAPH_COMPLETION"

    fake.SearchType = _SearchType
    fake.search = AsyncMock(return_value=["recalled fact"])
    fake.add = AsyncMock(return_value=None)
    fake.cognify = AsyncMock(return_value=None)
    monkeypatch.setitem(sys.modules, "cognee", fake)
    return fake


@pytest.fixture
def cognee_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[CogneeMemory, Any]:
    """Fixture providing a CogneeMemory with a mocked cognee module."""
    fake = _install_fake_cognee(monkeypatch)
    mem = CogneeMemory(_enabled_settings(str(tmp_path / "cognee")))
    return mem, fake


@pytest.mark.asyncio
async def test_cognee_recall_returns_formatted(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Verify that cognee recall returns the mocked fact as a string."""
    mem, _ = cognee_memory
    assert await mem.recall("who?") == "recalled fact"


@pytest.mark.asyncio
async def test_cognee_recall_blank_query_skips(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Verify that a blank query skips cognee entirely."""
    mem, fake = cognee_memory
    assert await mem.recall("   ") == ""
    # A skipped recall never configures or queries cognee.
    fake.config.set_llm_provider.assert_not_called()
    fake.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_cognee_remember_calls_add_and_cognify(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Verify that remember calls add and cognify on the cognee module."""
    mem, fake = cognee_memory
    await mem.remember("hello", "hi there")
    fake.add.assert_awaited_once()
    fake.cognify.assert_awaited_once()


@pytest.mark.asyncio
async def test_cognee_recall_never_raises(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """A backend error during recall degrades to an empty string."""
    mem, fake = cognee_memory
    fake.search = AsyncMock(side_effect=RuntimeError("backend down"))
    assert await mem.recall("who?") == ""


@pytest.mark.asyncio
async def test_cognee_remember_never_raises(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """A backend error during remember is swallowed (logged, not raised)."""
    mem, fake = cognee_memory
    fake.add = AsyncMock(side_effect=RuntimeError("write failed"))
    await mem.remember("hello", "hi")  # must not raise


@pytest.mark.asyncio
async def test_configure_restores_langfuse_env(
    cognee_memory: tuple[CogneeMemory, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LANGFUSE_* creds are hidden for cognee's import, then restored.

    cognee force-enables a langfuse import when these are set; the chat's own
    llmio tracing still needs them after setup.
    """
    import os

    mem, _ = cognee_memory
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-keep")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-keep")
    await mem.setup()
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-keep"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-keep"  # pragma: allowlist secret


# ---------------------------------------------------------------------------
# litellm Langfuse callback (dedicated cognee creds)
# ---------------------------------------------------------------------------


def _install_fake_litellm(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a stub ``litellm`` module (plus integration submodules) and return it."""
    fake: Any = types.ModuleType("litellm")
    fake.success_callback = []
    fake.failure_callback = []
    fake.callbacks = []
    fake.langfuse_public_key = None
    fake.langfuse_secret_key = None
    fake.langfuse_host = None
    fake.langfuse_default_tags = None

    class OpenTelemetryConfig:
        def __init__(
            self,
            exporter: str = "console",
            endpoint: str | None = None,
            headers: str | None = None,
            skip_set_global: bool = False,
        ) -> None:
            self.skip_set_global = skip_set_global
            self.exporter = exporter
            self.endpoint = endpoint
            self.headers = headers

    class LangfuseOtelLogger:
        def __init__(self, config: Any = None, *args: Any, **kwargs: Any) -> None:
            self.config = config

    fake_integrations = types.ModuleType("litellm.integrations")
    fake_langfuse_pkg = types.ModuleType("litellm.integrations.langfuse")
    fake_langfuse_otel = types.ModuleType("litellm.integrations.langfuse.langfuse_otel")
    fake_langfuse_otel.LangfuseOtelLogger = LangfuseOtelLogger  # type: ignore[attr-defined]
    fake_opentelemetry = types.ModuleType("litellm.integrations.opentelemetry")
    fake_opentelemetry.OpenTelemetryConfig = OpenTelemetryConfig  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "litellm", fake)
    monkeypatch.setitem(sys.modules, "litellm.integrations", fake_integrations)
    monkeypatch.setitem(sys.modules, "litellm.integrations.langfuse", fake_langfuse_pkg)
    monkeypatch.setitem(
        sys.modules,
        "litellm.integrations.langfuse.langfuse_otel",
        fake_langfuse_otel,
    )
    monkeypatch.setitem(
        sys.modules, "litellm.integrations.opentelemetry", fake_opentelemetry
    )
    fake.LangfuseOtelLogger = LangfuseOtelLogger
    return fake


def _install_fake_opentelemetry(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install stub ``opentelemetry`` modules so the OTLP import guard passes."""
    fake_otel = types.ModuleType("opentelemetry")
    fake_otel_exporter = types.ModuleType("opentelemetry.exporter")
    fake_otel_exporter_otlp = types.ModuleType("opentelemetry.exporter.otlp")
    fake_otel_exporter_otlp_proto = types.ModuleType(
        "opentelemetry.exporter.otlp.proto"
    )
    fake_otel_exporter_otlp_proto_http = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.http"
    )
    fake_otel_exporter_otlp_proto_http_trace = types.ModuleType(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    )

    class OTLPSpanExporter:
        pass

    fake_otel_exporter_otlp_proto_http_trace.OTLPSpanExporter = OTLPSpanExporter

    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.exporter", fake_otel_exporter)
    monkeypatch.setitem(
        sys.modules, "opentelemetry.exporter.otlp", fake_otel_exporter_otlp
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto",
        fake_otel_exporter_otlp_proto,
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http",
        fake_otel_exporter_otlp_proto_http,
    )
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.http.trace_exporter",
        fake_otel_exporter_otlp_proto_http_trace,
    )
    return fake_otel_exporter_otlp_proto_http_trace


@pytest.fixture
def cognee_memory_with_langfuse_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> tuple[CogneeMemory, Any, Any, Any]:
    """CogneeMemory with dedicated Langfuse creds and stubbed litellm/otel."""
    fake_cognee = _install_fake_cognee(monkeypatch)
    fake_litellm = _install_fake_litellm(monkeypatch)
    fake_otel = _install_fake_opentelemetry(monkeypatch)
    settings = _enabled_settings(str(tmp_path / "cognee"))
    settings.langfuse_public_key = SecretStr("pk-lf-dedicated")
    settings.langfuse_secret_key = SecretStr("sk-lf-dedicated")
    mem = CogneeMemory(settings)
    return mem, fake_cognee, fake_litellm, fake_otel


@pytest.mark.asyncio
async def test_litellm_langfuse_callback_configured_with_dedicated_creds(
    cognee_memory_with_langfuse_creds: tuple[CogneeMemory, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When dedicated creds are set, litellm's Langfuse callback is wired."""
    mem, _, fake_litellm, _ = cognee_memory_with_langfuse_creds
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.robotsix.net")
    await mem.setup()

    # An explicitly-configured LangfuseOtelLogger INSTANCE is registered (the
    # "langfuse_otel" string form would rebuild its config from the process
    # env on first LLM call — i.e. the MAIN project's creds).
    assert len(fake_litellm.callbacks) == 1
    lg = fake_litellm.callbacks[0]
    assert isinstance(lg, fake_litellm.LangfuseOtelLogger)
    assert lg.config.exporter == "otlp_http"
    # Must NOT attach to the globally-registered tracer provider (llmio's,
    # main project) — cognee spans need their own isolated provider.
    assert lg.config.skip_set_global is True
    assert (
        lg.config.endpoint == "https://langfuse.robotsix.net/api/public/otel/v1/traces"
    )
    expected_auth = base64.b64encode(b"pk-lf-dedicated:sk-lf-dedicated").decode()
    assert lg.config.headers == f"Authorization=Basic {expected_auth}"
    # No string callbacks and no module-attr credential leakage.
    assert fake_litellm.success_callback == []
    assert fake_litellm.failure_callback == []
    assert fake_litellm.langfuse_public_key is None
    assert fake_litellm.langfuse_secret_key is None
    assert fake_litellm.langfuse_default_tags == ["component:cognee"]

    # Idempotent across repeated setup() calls.
    mem._setup_done = False
    await mem.setup()
    assert len(fake_litellm.callbacks) == 1


@pytest.mark.asyncio
async def test_litellm_langfuse_callback_prefers_base_url_env(
    cognee_memory_with_langfuse_creds: tuple[CogneeMemory, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LANGFUSE_BASE_URL (llmio's name) wins over LANGFUSE_HOST."""
    mem, _, fake_litellm, _ = cognee_memory_with_langfuse_creds
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://langfuse.robotsix.net")
    monkeypatch.setenv("LANGFUSE_HOST", "https://wrong.example.com")
    await mem.setup()

    assert (
        fake_litellm.callbacks[0].config.endpoint
        == "https://langfuse.robotsix.net/api/public/otel/v1/traces"
    )


@pytest.mark.asyncio
async def test_litellm_langfuse_callback_skipped_without_host(
    cognee_memory_with_langfuse_creds: tuple[CogneeMemory, Any, Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No base-URL env -> no callback (never default to Langfuse US cloud)."""
    mem, _, fake_litellm, _ = cognee_memory_with_langfuse_creds
    monkeypatch.delenv("LANGFUSE_BASE_URL", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    await mem.setup()

    assert fake_litellm.callbacks == []


@pytest.mark.asyncio
async def test_litellm_langfuse_callback_skipped_without_creds(
    cognee_memory: tuple[CogneeMemory, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When dedicated creds are absent, litellm's callback is not configured."""
    fake_litellm = _install_fake_litellm(monkeypatch)
    mem, _ = cognee_memory
    await mem.setup()

    # litellm should remain untouched — no callback configured.
    assert fake_litellm.callbacks == []
    assert fake_litellm.success_callback == []
    assert fake_litellm.failure_callback == []
    assert fake_litellm.langfuse_public_key is None
    assert fake_litellm.langfuse_secret_key is None
    assert fake_litellm.langfuse_default_tags is None


# ---------------------------------------------------------------------------
# LANGFUSE_* env-var hiding guard (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_langfuse_env_guard_regression(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Regression: LANGFUSE_* creds hidden for cognee import, restored after.

    Cognee's model validator force-selects Langfuse monitoring when
    ``LANGFUSE_*`` env vars are present, then ``from langfuse.decorators
    import observe`` crashes because the Docker image ships no langfuse SDK.
    The guard in ``_configure()`` hides those vars before ``import cognee``
    and restores them afterwards.

    This test verifies both halves of the guard so future refactors trigger
    a failing test before shipping.
    """
    import os

    _install_fake_cognee(monkeypatch)

    popped: list[str] = []
    _real_pop = os.environ.pop

    def _tracking_pop(key: str, *args: Any) -> Any:
        result = _real_pop(key, *args)
        popped.append(key)
        return result

    monkeypatch.setattr(os.environ, "pop", _tracking_pop)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-guard-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-guard-test")

    mem = CogneeMemory(_enabled_settings(str(tmp_path / "cognee")))
    await mem.setup()

    assert "LANGFUSE_PUBLIC_KEY" in popped, (
        "LANGFUSE_PUBLIC_KEY must be popped before import cognee — "
        "otherwise cognee's unconditional `from langfuse.decorators import "
        "observe` will crash"
    )
    assert "LANGFUSE_SECRET_KEY" in popped, (
        "LANGFUSE_SECRET_KEY must be popped before import cognee"
    )
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == "pk-guard-test"
    assert os.environ["LANGFUSE_SECRET_KEY"] == "sk-guard-test"
