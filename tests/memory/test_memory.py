"""Tests for the memory layer.

Covers :func:`build_memory`, ``NullMemory``, and the cognee backend's
graceful-degradation contract (cognee mocked, never imported for real).
"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path
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
from robotsix_chat.memory.cognee import (
    CogneeMemory,
    _format_results,
    _is_healable_kuzu_error,
)


def _enabled_settings(data_dir: str = "/data/cognee") -> MemorySettings:
    """Return a valid enabled MemorySettings with key and endpoint present."""
    return MemorySettings(
        enabled=True,
        data_dir=data_dir,
        llm=MemoryLlmSettings(api_key=SecretStr("sk-or-x")),  # pragma: allowlist secret
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


# ---------------------------------------------------------------------------
# Session scoping — regression: concurrent windows must not share guidance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_id_forwarded_to_cognee_search(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """session_id passed to recall() is forwarded to cognee.search()."""
    mem, fake = cognee_memory
    await mem.recall("query", session_id="sess-42")
    fake.search.assert_awaited_once()
    kwargs = fake.search.call_args.kwargs
    assert kwargs["session_id"] == "sess-42"


@pytest.mark.asyncio
async def test_session_id_forwarded_to_cognee_add_and_cognify(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """session_id passed to remember() is forwarded to cognee.add() + cognify()."""
    mem, fake = cognee_memory
    await mem.remember("hello", "hi", session_id="sess-99")
    fake.add.assert_awaited_once()
    assert fake.add.call_args.kwargs["session_id"] == "sess-99"
    fake.cognify.assert_awaited_once()
    assert fake.cognify.call_args.kwargs["session_id"] == "sess-99"


@pytest.mark.asyncio
async def test_interleaved_conversations_scoped_independently(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Two concurrent conversations are scoped independently.

    Guidance written in one must not appear in the recall of the other.

    Regression: the query-rewrite LLM was resolving 'this session' against
    guidance contaminated by other concurrent windows because session-level
    memory was process-global.
    """
    mem, fake = cognee_memory

    # Simulate two interleaved conversations.
    await mem.remember(
        "make ticket X prioritized",
        "ticket X is now prioritized.",
        session_id="window-A",
    )
    await mem.remember(
        "monitor tickets Y, Z",
        "watching tickets Y, Z.",
        session_id="window-B",
    )

    # Verify each add was scoped to its own session.
    assert fake.add.call_count == 2
    assert fake.add.call_args_list[0].kwargs["session_id"] == "window-A"
    assert fake.add.call_args_list[1].kwargs["session_id"] == "window-B"

    # Recall from window-A: must only search window-A's session.
    fake.search.reset_mock()
    fake.search.return_value = ["ticket X prioritized"]
    recalled_a = await mem.recall("this session", session_id="window-A")
    assert fake.search.call_args.kwargs["session_id"] == "window-A"
    assert "ticket X prioritized" in recalled_a

    # Recall from window-B: must only search window-B's session.
    fake.search.reset_mock()
    fake.search.return_value = ["tickets Y, Z monitored"]
    recalled_b = await mem.recall("this session", session_id="window-B")
    assert fake.search.call_args.kwargs["session_id"] == "window-B"
    assert "tickets Y, Z monitored" in recalled_b


@pytest.mark.asyncio
async def test_null_session_id_still_works(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """When no session_id is given (legacy path), cognee still works."""
    mem, fake = cognee_memory
    await mem.recall("query")
    kwargs = fake.search.call_args.kwargs
    assert kwargs["session_id"] is None

    fake.add.reset_mock()
    fake.cognify.reset_mock()
    await mem.remember("u", "a")
    assert fake.add.call_args.kwargs["session_id"] is None
    assert fake.cognify.call_args.kwargs["session_id"] is None


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
    settings.langfuse.public_key = SecretStr("pk-lf-dedicated")
    settings.langfuse.secret_key = SecretStr("sk-lf-dedicated")
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
    assert os.environ["LANGFUSE_PUBLIC_KEY"] == (
        "pk-guard-test"  # pragma: allowlist secret
    )
    assert os.environ["LANGFUSE_SECRET_KEY"] == (
        "sk-guard-test"  # pragma: allowlist secret
    )


# ---------------------------------------------------------------------------
# Stale kuzu shadow-file self-heal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_stale_shadow_directory(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """A stale .shadow directory is removed during setup."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    shadow_dir = databases_dir / "cognee_graph_ladybug.shadow"
    shadow_dir.mkdir()
    (shadow_dir / "stale_file").write_text("leftover")

    await mem.setup()

    assert not shadow_dir.exists()


@pytest.mark.asyncio
async def test_remove_stale_shadow_file(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """A stale .shadow file is removed during setup."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    shadow_file = databases_dir / "other.shadow"
    shadow_file.write_text("stale")

    await mem.setup()

    assert not shadow_file.exists()


@pytest.mark.asyncio
async def test_setup_clean_with_no_shadow_entries(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Setup succeeds when no .shadow entries exist."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)
    # No .shadow entries — must not raise.
    await mem.setup()


@pytest.mark.asyncio
async def test_setup_clean_with_no_databases_dir(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Setup succeeds when the databases directory does not exist yet."""
    mem, _ = cognee_memory
    # system dir exists but databases dir doesn't — must not raise.
    await mem.setup()


@pytest.mark.asyncio
async def test_shadow_removal_failure_is_logged_not_raised(
    cognee_memory: tuple[CogneeMemory, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError during shadow removal is logged, not raised."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    shadow_dir = databases_dir / "cognee_graph_ladybug.shadow"
    shadow_dir.mkdir()

    import shutil as _shutil

    original_rmtree = _shutil.rmtree

    def _failing_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path).endswith(".shadow"):
            raise OSError("permission denied")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(_shutil, "rmtree", _failing_rmtree)

    # Must not raise despite the OSError.
    await mem.setup()

    # The shadow directory is still there (removal failed).
    assert shadow_dir.exists()


# ---------------------------------------------------------------------------
# Stale kuzu wal cleanup + database directory recreation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_stale_shadow_and_wal_together(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Stale .shadow and .wal entries are both removed, matching DB dir recreated."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    db_dir = databases_dir / "cognee_graph_ladybug"
    db_dir.mkdir()
    (db_dir / "data.kz").write_text("main db content")

    shadow_dir = databases_dir / "cognee_graph_ladybug.shadow"
    shadow_dir.mkdir()
    (shadow_dir / "stale_checkpoint").write_text("stale")

    wal_file = databases_dir / "cognee_graph_ladybug.wal"
    wal_file.write_text("stale wal")

    await mem.setup()

    assert not shadow_dir.exists()
    assert not wal_file.exists()
    assert not db_dir.exists()


@pytest.mark.asyncio
async def test_wal_cleaned_when_shadow_already_deleted(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """Orphaned .wal referencing deleted shadow is removed, DB directory recreated."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    db_dir = databases_dir / "cognee_graph_ladybug"
    db_dir.mkdir()
    (db_dir / "data.kz").write_text("main db content")

    # Shadow already deleted (the previous self-heal scenario).
    # Only the WAL remains.
    wal_file = databases_dir / "cognee_graph_ladybug.wal"
    wal_file.write_text("wal referencing deleted shadow")

    await mem.setup()

    assert not wal_file.exists()
    assert not db_dir.exists()


@pytest.mark.asyncio
async def test_setup_clean_with_wal_but_no_shadow_and_no_db_dir(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """An orphaned .wal with no matching DB directory is removed without error."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    # Only a stale WAL, no DB dir, no shadow.
    wal_file = databases_dir / "cognee_graph_ladybug.wal"
    wal_file.write_text("orphaned wal")

    await mem.setup()

    assert not wal_file.exists()


@pytest.mark.asyncio
async def test_db_recreation_failure_is_logged_not_raised(
    cognee_memory: tuple[CogneeMemory, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An OSError during database directory recreation is logged, not raised."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    shadow_dir = databases_dir / "cognee_graph_ladybug.shadow"
    shadow_dir.mkdir()

    db_dir = databases_dir / "cognee_graph_ladybug"
    db_dir.mkdir()

    import shutil as _shutil

    original_rmtree = _shutil.rmtree

    def _failing_rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path).endswith(".shadow"):
            # Shadow removal succeeds.
            original_rmtree(path, *args, **kwargs)
        else:
            # DB directory removal fails.
            raise OSError("permission denied")

    monkeypatch.setattr(_shutil, "rmtree", _failing_rmtree)

    # Must not raise despite the OSError on DB dir removal.
    await mem.setup()

    # Shadow was removed, but DB dir is still there (recreation failed).
    assert not shadow_dir.exists()
    assert db_dir.exists()


# ---------------------------------------------------------------------------
# Missing-shadow detection: DB entity without companion .shadow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_heals_db_missing_shadow(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """DB directory present with NO .shadow or .wal → removed during setup."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    db_dir = databases_dir / "cognee_graph_ladybug"
    db_dir.mkdir()
    (db_dir / "data.kz").write_text("db content")

    # No .shadow, no .wal — this is the current production failure state.
    assert not (databases_dir / "cognee_graph_ladybug.shadow").exists()
    assert not (databases_dir / "cognee_graph_ladybug.wal").exists()

    await mem.setup()

    assert not db_dir.exists()


@pytest.mark.asyncio
async def test_setup_heals_db_file_missing_shadow(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """DB file (single-file ladybug form) with NO .shadow → removed during setup."""
    mem, _ = cognee_memory
    system_root = Path(mem._settings.data_dir) / "system"
    databases_dir = system_root / "databases"
    databases_dir.mkdir(parents=True, exist_ok=True)

    db_file = databases_dir / "cognee_graph_ladybug"
    db_file.write_text("single-file db content")

    # No .shadow — inconsistent state.
    assert not (databases_dir / "cognee_graph_ladybug.shadow").exists()

    await mem.setup()

    assert not db_file.exists()


# ---------------------------------------------------------------------------
# Open-time retry: catch ENOENT, heal, retry once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recall_retry_on_shadow_missing(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """recall() catches shadow-missing RuntimeError, heals, and retries once."""
    mem, fake = cognee_memory

    call_count = 0

    async def _search(*args: Any, **kwargs: Any) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError(
                "IO exception: Cannot open file "
                "/data/cognee/system/databases/cognee_graph_ladybug.shadow: "
                "No such file or directory"
            )
        return ["recalled after heal"]

    fake.search = _search

    result = await mem.recall("query")
    assert result == "recalled after heal"
    assert call_count == 2


@pytest.mark.asyncio
async def test_recall_retry_on_db_id_mismatch(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """recall() catches DB-ID-mismatch RuntimeError, heals, and retries once."""
    mem, fake = cognee_memory

    call_count = 0

    async def _search(*args: Any, **kwargs: Any) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError(
                "Database ID 12345 does not match the current database ID 67890"
            )
        return ["recalled after heal"]

    fake.search = _search

    result = await mem.recall("query")
    assert result == "recalled after heal"
    assert call_count == 2


@pytest.mark.asyncio
async def test_recall_no_retry_on_unrelated_error(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """recall() does NOT retry on errors that are not healable."""
    mem, fake = cognee_memory

    call_count = 0

    async def _search(*args: Any, **kwargs: Any) -> list[str]:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("some unrelated database error")

    fake.search = _search

    result = await mem.recall("query")
    assert result == ""
    assert call_count == 1  # No retry attempted.


@pytest.mark.asyncio
async def test_remember_retry_on_shadow_missing(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """remember() catches shadow-missing RuntimeError, heals, and retries once."""
    mem, fake = cognee_memory

    call_count = 0

    async def _add(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError(
                "IO exception: Cannot open file "
                "/data/cognee/system/databases/cognee_graph_ladybug.shadow: "
                "No such file or directory"
            )

    fake.add = _add
    fake.cognify = AsyncMock(return_value=None)

    await mem.remember("hello", "hi")
    assert call_count == 2


@pytest.mark.asyncio
async def test_remember_no_retry_on_unrelated_error(
    cognee_memory: tuple[CogneeMemory, Any],
) -> None:
    """remember() does NOT retry on errors that are not healable."""
    mem, fake = cognee_memory

    call_count = 0

    async def _add(*args: Any, **kwargs: Any) -> None:
        nonlocal call_count
        call_count += 1
        raise RuntimeError("some other error")

    fake.add = _add
    fake.cognify = AsyncMock(return_value=None)

    await mem.remember("hello", "hi")  # must not raise
    assert call_count == 1  # No retry attempted.


# ---------------------------------------------------------------------------
# _is_healable_kuzu_error unit tests
# ---------------------------------------------------------------------------


def test_is_healable_shadow_missing() -> None:
    """Shadow-missing ENOENT is recognised as healable."""
    exc = RuntimeError(
        "IO exception: Cannot open file "
        "/data/cognee/system/databases/cognee_graph_ladybug.shadow: "
        "No such file or directory"
    )
    assert _is_healable_kuzu_error(exc) is True


def test_is_healable_db_id_mismatch() -> None:
    """Database ID mismatch is recognised as healable."""
    exc = RuntimeError("Database ID 12345 does not match the current database ID 67890")
    assert _is_healable_kuzu_error(exc) is True


def test_is_healable_unrelated_error() -> None:
    """Unrelated RuntimeError is NOT healable."""
    exc = RuntimeError("disk full")
    assert _is_healable_kuzu_error(exc) is False
