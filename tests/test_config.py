"""Tests for the environment-based configuration system."""

from __future__ import annotations

from pathlib import Path

import pytest

from robotsix_chat.config import Settings

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wipe_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all config-related env vars so tests start from a clean slate."""
    for name in (
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_BASE_URL",
        "SERVER_HOST",
        "SERVER_PORT",
        "LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional fields fall back to their documented defaults."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")

    settings = Settings.from_env()

    assert settings.llm_model == "gpt-4o-mini"
    assert settings.llm_base_url is None
    assert settings.server_host == "127.0.0.1"
    assert settings.server_port == 8000
    assert settings.log_level == "INFO"


def test_log_level_default() -> None:
    """Explicit check that ``log_level`` defaults to ``"INFO"``."""
    s = Settings(llm_api_key="sk-test")
    assert s.log_level == "INFO"


# ---------------------------------------------------------------------------
# API key validation
# ---------------------------------------------------------------------------


def test_missing_api_key_raises() -> None:
    """Constructing ``Settings`` with an empty ``llm_api_key`` raises ``ValueError``."""
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        Settings(llm_api_key="")


def test_missing_api_key_via_from_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``from_env()`` raises ``ValueError`` when ``LLM_API_KEY`` is not set."""
    _wipe_env_vars(monkeypatch)

    with pytest.raises(ValueError, match="LLM_API_KEY"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# Loading from environment variables
# ---------------------------------------------------------------------------


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``Settings.from_env()`` picks up values from ``os.environ``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-env-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-5")
    monkeypatch.setenv("LLM_BASE_URL", "https://custom.example.com")
    monkeypatch.setenv("SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("SERVER_PORT", "9090")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")

    settings = Settings.from_env()

    assert settings.llm_api_key == "sk-env-test"
    assert settings.llm_model == "gpt-5"
    assert settings.llm_base_url == "https://custom.example.com"
    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 9090
    assert settings.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# SERVER_PORT coercion
# ---------------------------------------------------------------------------


def test_server_port_coerced_to_int(monkeypatch: pytest.MonkeyPatch) -> None:
    """A string ``SERVER_PORT`` is coerced to ``int``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SERVER_PORT", "8080")

    settings = Settings.from_env()

    assert settings.server_port == 8080
    assert isinstance(settings.server_port, int)


def test_server_port_invalid_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric ``SERVER_PORT`` raises ``ValueError``."""
    _wipe_env_vars(monkeypatch)
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("SERVER_PORT", "twelve")

    with pytest.raises(ValueError, match="SERVER_PORT"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# .env file loading
# ---------------------------------------------------------------------------


def test_dotenv_file_loaded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Values from a ``.env`` file are picked up by ``from_env()``."""
    _wipe_env_vars(monkeypatch)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_API_KEY=sk-dotenv-test\n"
        "LLM_MODEL=gpt-4\n"
        "SERVER_HOST=0.0.0.0\n"
        "SERVER_PORT=3000\n"
    )

    # Load the explicit file before from_env() runs; from_env() will
    # call load_dotenv() again (harmless), but the env vars are
    # already present.
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=str(env_file))

    settings = Settings.from_env()

    assert settings.llm_api_key == "sk-dotenv-test"
    assert settings.llm_model == "gpt-4"
    assert settings.server_host == "0.0.0.0"
    assert settings.server_port == 3000
