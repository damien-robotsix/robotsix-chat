"""Tests for the CLI entry point and server launcher."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest
import structlog

from robotsix_chat.chat.server.cli import (
    _configure_logging,
    _setup_observability,
    run_server,
    run_server_from_config,
)
from robotsix_chat.config import Settings

# ---------------------------------------------------------------------------
# _configure_logging
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    """Unit tests for ``_configure_logging``."""

    @pytest.fixture(autouse=True)
    def _reset_structlog(self) -> None:
        """Reset structlog configuration between tests."""
        # structlog caches configuration; clear it so each test starts clean.
        structlog.reset_defaults()
        # Clear root logger handlers so each test starts from a known state.
        root = logging.getLogger()
        root.handlers.clear()
        root.setLevel(logging.WARNING)
        # Also clear uvicorn loggers.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lgr = logging.getLogger(name)
            lgr.handlers.clear()
            lgr.propagate = False

    def test_json_format_uses_json_renderer(self) -> None:
        """When ``log_json_format=True``, the formatter produces JSON output."""
        import json

        settings = Settings(log_json_format=True, log_level="DEBUG")
        _configure_logging(settings)

        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        formatter = handler.formatter
        assert formatter is not None

        # Verify by formatting a record: should be valid JSON.
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        formatted = formatter.format(record)
        data = json.loads(formatted)
        assert data["event"] == "hello"

    def test_no_json_format_uses_console_renderer(self) -> None:
        """When ``log_json_format=False``, the output is human-readable."""
        import json

        settings = Settings(log_json_format=False, log_level="INFO")
        _configure_logging(settings)

        root = logging.getLogger()
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        formatter = root.handlers[0].formatter

        # Console output contains the message and is NOT valid JSON.
        record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
        formatted = formatter.format(record)
        assert "hello" in formatted
        with pytest.raises(json.JSONDecodeError):
            json.loads(formatted)

    def test_uvicorn_loggers_propagate(self) -> None:
        """Uvicorn loggers have their handlers cleared and propagate=True."""
        # Set up fake handlers on uvicorn loggers first.
        uvicorn = logging.getLogger("uvicorn")
        uvicorn.handlers.clear()
        uvicorn.addHandler(logging.StreamHandler())
        uvicorn_access = logging.getLogger("uvicorn.access")
        uvicorn_access.handlers.clear()
        uvicorn_access.addHandler(logging.StreamHandler())

        settings = Settings()
        _configure_logging(settings)

        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            lgr = logging.getLogger(name)
            assert lgr.handlers == []
            assert lgr.propagate is True

    def test_foreign_pre_chain_includes_shared_processors(self) -> None:
        """The formatter's ``foreign_pre_chain`` includes merge_contextvars.

        So stdlib-logger calls (e.g. from libraries) also get structured.
        """
        settings = Settings()
        _configure_logging(settings)

        formatter = logging.getLogger().handlers[0].formatter
        # structlog < 25.0.0: foreign_pre_chain; >= 25.0.0: _foreign_pre_chain
        # (or processors when neither is available).
        chain = (
            getattr(formatter, "foreign_pre_chain", None)
            or getattr(formatter, "_foreign_pre_chain", None)
            or getattr(formatter, "processors", None)
        )
        assert isinstance(chain, (list, tuple))
        assert len(chain) > 0


# ---------------------------------------------------------------------------
# _setup_observability
# ---------------------------------------------------------------------------


class TestSetupObservability:
    """Unit tests for ``_setup_observability``."""

    def test_happy_path_calls_setup_logging_and_langfuse(self) -> None:
        """When the tracing imports succeed, both setup functions are called."""
        mock_setup_logging = MagicMock()
        mock_setup_langfuse = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "robotsix_llmio.core.tracing": MagicMock(
                    setup_langfuse_tracing=mock_setup_langfuse
                ),
                "robotsix_llmio.logging": MagicMock(setup_logging=mock_setup_logging),
            },
        ):
            _setup_observability()

        mock_setup_logging.assert_called_once()
        mock_setup_langfuse.assert_called_once()

    def test_importerror_fallback_does_not_raise(self) -> None:
        """When the tracing imports fail, the function returns without error."""
        # Set the target modules to None in sys.modules so the `from … import`
        # statements raise ModuleNotFoundError (a subclass of ImportError).
        with patch.dict(
            "sys.modules",
            {
                "robotsix_llmio.core.tracing": None,
                "robotsix_llmio.logging": None,
            },
        ):
            # Must not raise.
            _setup_observability()

    def test_importerror_fallback_logs_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When imports fail, a debug-level message is logged."""
        with (
            patch.dict(
                "sys.modules",
                {
                    "robotsix_llmio.core.tracing": None,
                    "robotsix_llmio.logging": None,
                },
            ),
            caplog.at_level(logging.DEBUG),
        ):
            _setup_observability()

        assert any(
            "tracing extras not installed" in record.message
            for record in caplog.records
        )


# ---------------------------------------------------------------------------
# run_server
# ---------------------------------------------------------------------------


class TestRunServer:
    """Unit tests for ``run_server``."""

    def test_calls_uvicorn_with_default_host_and_port(self) -> None:
        """``run_server`` passes ``host`` and ``port`` to ``uvicorn.run``."""
        agent = MagicMock()

        with (
            patch("robotsix_chat.chat.server.cli.create_app") as mock_create,
            patch("uvicorn.run") as mock_uvicorn_run,
        ):
            mock_app = MagicMock()
            mock_create.return_value = mock_app

            run_server(agent)

        mock_uvicorn_run.assert_called_once_with(mock_app, host="0.0.0.0", port=8000)

    def test_calls_uvicorn_with_custom_host_and_port(self) -> None:
        """``run_server`` forwards custom ``host`` and ``port``."""
        agent = MagicMock()

        with (
            patch("robotsix_chat.chat.server.cli.create_app") as mock_create,
            patch("uvicorn.run") as mock_uvicorn_run,
        ):
            mock_app = MagicMock()
            mock_create.return_value = mock_app

            run_server(agent, host="127.0.0.1", port=9999)

        mock_uvicorn_run.assert_called_once_with(mock_app, host="127.0.0.1", port=9999)

    def test_forwards_all_kwargs_to_create_app(self) -> None:
        """``run_server`` passes every kwarg through to ``create_app``."""
        agent = MagicMock()
        conv_store = MagicMock()
        event_bus = MagicMock()
        run_serializer = MagicMock()
        on_startup = MagicMock()
        sub_registry = MagicMock()
        sub_delivery = MagicMock()

        with (
            patch("robotsix_chat.chat.server.cli.create_app") as mock_create,
            patch("uvicorn.run"),
        ):
            mock_create.return_value = MagicMock()

            run_server(
                agent,
                serve_ui=False,
                idle_timeout_minutes=5,
                max_images_per_message=4,
                max_image_bytes=1_000_000,
                allowed_image_media_types=["image/png"],
                cors_allow_origins=["https://example.com"],
                correlation_id_header="X-Custom-ID",
                conversation_store=conv_store,
                event_bus=event_bus,
                run_serializer=run_serializer,
                subsession_registry=sub_registry,
                subsession_delivery=sub_delivery,
                on_startup=on_startup,
            )

        mock_create.assert_called_once_with(
            agent,
            summary_agent=None,
            serve_ui=False,
            idle_timeout_minutes=5,
            max_images_per_message=4,
            max_image_bytes=1_000_000,
            allowed_image_media_types=["image/png"],
            cors_allow_origins=["https://example.com"],
            correlation_id_header="X-Custom-ID",
            conversation_store=conv_store,
            event_bus=event_bus,
            run_serializer=run_serializer,
            subsession_registry=sub_registry,
            subsession_delivery=sub_delivery,
            on_startup=on_startup,
            on_startup_async=None,
            on_shutdown=None,
            direct_repo_settings=None,
            github_security_settings=None,
        )


# ---------------------------------------------------------------------------
# run_server_from_config
# ---------------------------------------------------------------------------


class TestRunServerFromConfig:
    """Unit tests for ``run_server_from_config``."""

    @pytest.fixture(autouse=True)
    def _reset_structlog(self) -> None:
        """Reset structlog between tests."""
        structlog.reset_defaults()
        root = logging.getLogger()
        root.handlers.clear()

    def test_passes_settings_values_to_run_server(self) -> None:
        """``run_server_from_config`` reads Settings and forwards them."""
        agent = MagicMock()

        with (
            patch(
                "robotsix_chat.chat.server.cli.Settings.load",
                return_value=Settings(
                    server_host="10.0.0.1",
                    server_port=9090,
                    idle_timeout_minutes=15,
                    max_images_per_message=4,
                    max_image_bytes=2_000_000,
                    allowed_image_media_types=["image/webp"],
                    cors_allow_origins=["http://local"],
                    correlation_id_header="X-Req",
                    log_json_format=False,
                    log_level="WARNING",
                ),
            ),
            patch(
                "robotsix_chat.chat.server.cli._configure_logging"
            ) as mock_cfg_logging,
            patch("robotsix_chat.chat.server.cli._export_langfuse_env") as mock_export,
            patch("robotsix_chat.chat.server.cli._setup_observability") as mock_obs,
            patch("robotsix_chat.subsessions.SubsessionRegistry") as mock_registry_cls,
            patch("robotsix_chat.subsessions.ParentDelivery") as mock_delivery_cls,
            patch("robotsix_chat.subsessions.resume_subsessions") as _,
            # The lazy ``from . import run_server`` inside run_server_from_config
            # resolves through the package re-export — patch that, not cli.run_server.
            patch("robotsix_chat.chat.server.run_server") as mock_run_server,
        ):
            mock_registry_cls.return_value = MagicMock()
            mock_delivery_cls.return_value = MagicMock()

            run_server_from_config(agent=agent)

        # Verify _configure_logging was called with the Settings object.
        mock_cfg_logging.assert_called_once()
        cfg_arg = mock_cfg_logging.call_args[0][0]
        assert isinstance(cfg_arg, Settings)
        assert cfg_arg.server_host == "10.0.0.1"
        assert cfg_arg.server_port == 9090

        # Verify observability setup.
        mock_export.assert_called_once()
        mock_obs.assert_called_once()

        # Verify run_server was called with the resolved values.
        mock_run_server.assert_called_once()
        kwargs = mock_run_server.call_args.kwargs
        assert mock_run_server.call_args.args[0] is agent
        assert kwargs["host"] == "10.0.0.1"
        assert kwargs["port"] == 9090
        assert kwargs["idle_timeout_minutes"] == 15
        assert kwargs["max_images_per_message"] == 4
        assert kwargs["max_image_bytes"] == 2_000_000
        assert kwargs["allowed_image_media_types"] == ["image/webp"]
        assert kwargs["cors_allow_origins"] == ["http://local"]
        assert kwargs["correlation_id_header"] == "X-Req"
        assert kwargs["conversation_store"] is not None
        assert kwargs["event_bus"] is not None
        assert kwargs["run_serializer"] is not None
        assert kwargs["subsession_registry"] is not None
        assert kwargs["subsession_delivery"] is not None
        assert kwargs["on_startup"] is not None

    def test_no_agent_creates_default(self) -> None:
        """When ``agent`` is ``None``, one is built from settings."""
        with (
            patch(
                "robotsix_chat.chat.server.cli.Settings.load",
                return_value=Settings(),
            ),
            patch("robotsix_chat.chat.server.cli._configure_logging"),
            patch("robotsix_chat.chat.server.cli._export_langfuse_env"),
            patch("robotsix_chat.chat.server.cli._setup_observability"),
            patch("robotsix_chat.subsessions.SubsessionRegistry") as mock_registry_cls,
            patch("robotsix_chat.subsessions.ParentDelivery") as mock_delivery_cls,
            patch("robotsix_chat.subsessions.resume_subsessions") as _,
            patch(
                "robotsix_chat.chat.server.cli.create_agent_from_settings"
            ) as mock_create_agent,
            patch("robotsix_chat.chat.server.run_server") as mock_run_server,
        ):
            mock_registry_cls.return_value = MagicMock()
            mock_delivery_cls.return_value = MagicMock()
            mock_agent = MagicMock()
            mock_create_agent.return_value = mock_agent

            run_server_from_config(agent=None)

        # create_agent_from_settings is called twice: once for the main agent
        # (because agent=None) and once for the summary_agent.
        assert mock_create_agent.call_count >= 2
        # The agent passed to run_server should be the one we created.
        mock_run_server.assert_called_once()
        assert mock_run_server.call_args.args[0] is mock_agent

    def test_on_startup_resume_calls_resume_subsessions(self) -> None:
        """The ``on_startup`` callback resumes persisted subsessions."""
        resume_calls = []

        with (
            patch(
                "robotsix_chat.chat.server.cli.Settings.load",
                return_value=Settings(),
            ),
            patch("robotsix_chat.chat.server.cli._configure_logging"),
            patch("robotsix_chat.chat.server.cli._export_langfuse_env"),
            patch("robotsix_chat.chat.server.cli._setup_observability"),
            patch(
                "robotsix_chat.subsessions.SubsessionRegistry",
                return_value=MagicMock(),
            ),
            patch(
                "robotsix_chat.subsessions.ParentDelivery",
                return_value=MagicMock(),
            ),
            patch(
                "robotsix_chat.subsessions.resume_subsessions",
                side_effect=lambda env: resume_calls.append(env),
            ),
            patch("robotsix_chat.chat.server.run_server") as mock_run_server,
        ):
            run_server_from_config(agent=MagicMock())

        mock_run_server.assert_called_once()
        on_startup = mock_run_server.call_args.kwargs["on_startup"]
        assert on_startup is not None
        assert callable(on_startup)

        # Call the startup callback and verify resume_subsessions is invoked.
        on_startup()
        assert len(resume_calls) == 1
