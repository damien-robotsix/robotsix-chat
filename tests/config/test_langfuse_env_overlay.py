"""Deploy-dispatched Langfuse credentials → ``langfuse.projects`` overlay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from robotsix_chat.config import Settings
from robotsix_chat.config.langfuse_models import (
    LangfuseProjectCreds,
    LangfuseSettings,
    env_project_name,
    overlay_env_projects,
)

# Real Langfuse key shapes, as central-deploy injects them for a consumed
# ``langfuse:robotsix-mill`` scope.
MILL_PK = "pk-lf-1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d"
MILL_SK = "sk-lf-6d5c4b3a-2f1e-0d9c-8b7a-6f5e4d3c2b1a"  # pragma: allowlist secret


def test_env_project_name_maps_segment() -> None:
    assert env_project_name("ROBOTSIX_MILL") == "robotsix-mill"
    assert env_project_name("ROBOTSIX_CI_FIX") == "robotsix-ci-fix"


def test_overlay_materialises_project_from_env() -> None:
    base = LangfuseSettings(
        projects={
            "robotsix-chat": LangfuseProjectCreds(
                public_key=SecretStr("pk-lf-chat"),
                secret_key=SecretStr("sk-lf-chat"),  # pragma: allowlist secret
            )
        }
    )
    env = {
        "LANGFUSE_ROBOTSIX_MILL_PUBLIC_KEY": MILL_PK,
        "LANGFUSE_ROBOTSIX_MILL_SECRET_KEY": MILL_SK,
    }

    result = overlay_env_projects(base, env)

    mill = result.creds("robotsix-mill")
    assert mill.is_configured()
    assert mill.public_key.get_secret_value() == MILL_PK
    assert mill.secret_key.get_secret_value() == MILL_SK
    # The component's own project is untouched, and the input is not mutated.
    assert result.creds("robotsix-chat").public_key.get_secret_value() == "pk-lf-chat"
    assert "robotsix-mill" not in base.projects


def test_overlay_ignores_bare_and_half_filled_vars() -> None:
    env = {
        # llmio's own tracing wiring — no project segment.
        "LANGFUSE_PUBLIC_KEY": "pk-lf-own",
        "LANGFUSE_SECRET_KEY": "sk-lf-own",  # pragma: allowlist secret
        "LANGFUSE_HOST": "https://langfuse.example.com",
        # Only one half of the pair.
        "LANGFUSE_ROBOTSIX_CI_FIX_PUBLIC_KEY": MILL_PK,
        # Empty values.
        "LANGFUSE_ROBOTSIX_X_PUBLIC_KEY": "",
        "LANGFUSE_ROBOTSIX_X_SECRET_KEY": "",
    }
    base = LangfuseSettings()

    result = overlay_env_projects(base, env)

    assert result is base
    assert result.projects == {}


def test_env_overlays_config_and_keeps_project_id() -> None:
    base = LangfuseSettings(
        projects={
            "robotsix-mill": LangfuseProjectCreds(
                public_key=SecretStr("pk-lf-stale"),
                secret_key=SecretStr("sk-lf-stale"),  # pragma: allowlist secret
                project_id="proj-123",
            )
        }
    )
    env = {
        "LANGFUSE_ROBOTSIX_MILL_PUBLIC_KEY": MILL_PK,
        "LANGFUSE_ROBOTSIX_MILL_SECRET_KEY": MILL_SK,
    }

    mill = overlay_env_projects(base, env).creds("robotsix-mill")

    assert mill.public_key.get_secret_value() == MILL_PK
    assert mill.secret_key.get_secret_value() == MILL_SK
    assert mill.project_id == "proj-123"


def test_settings_load_applies_overlay_without_touching_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.json"
    original = {
        "langfuse": {
            "host": "https://langfuse.example.com",
            "projects": {
                "robotsix-chat": {
                    "public_key": "pk-lf-chat",
                    "secret_key": "sk-lf-chat",  # pragma: allowlist secret
                }
            },
        }
    }
    config_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setenv("ROBOTSIX_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("LANGFUSE_ROBOTSIX_MILL_PUBLIC_KEY", MILL_PK)
    monkeypatch.setenv("LANGFUSE_ROBOTSIX_MILL_SECRET_KEY", MILL_SK)

    settings = Settings.load()

    assert sorted(settings.langfuse.projects) == ["robotsix-chat", "robotsix-mill"]
    assert settings.langfuse.creds("robotsix-mill").is_configured()
    assert settings.langfuse.host == "https://langfuse.example.com"
    # The foreign key is never written into the config file.
    on_disk = config_path.read_text(encoding="utf-8")
    assert MILL_PK not in on_disk
    assert MILL_SK not in on_disk
    assert json.loads(on_disk) == original
