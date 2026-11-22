"""Shared fixtures for tests/repo/direct/ test modules."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx

from robotsix_chat.config import DirectRepoSettings


def _prepopulate_installation_token(settings: DirectRepoSettings) -> None:
    """Compatibility no-op.

    The client owns no token cache — robotsix-github-auth's public
    ``mint_installation_token`` handles minting and caching, and the autouse
    ``_mock_github_auth`` fixture fakes it to return a constant token.  So
    tests no longer need to seed a local cache; this helper is retained so
    the call sites across the test modules keep compiling unchanged.
    """


def _settings(**kw: Any) -> DirectRepoSettings:
    base: dict[str, Any] = {
        "enabled": True,
        "github_app_id": "12345",
        "github_app_private_key": "fake-key",  # pragma: allowlist secret
        "github_app_installation_id": "67890",
        "board_api_base_url": "http://127.0.0.1:8077",
    }
    base.update(kw)
    return DirectRepoSettings(**base)


@pytest.fixture(autouse=True)
def _mock_github_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock robotsix-github-auth so the shared library is never imported."""
    import sys

    def _fake_mint(**kw: object) -> object:
        return SimpleNamespace(token="ghs_test_installation_token")

    fake = SimpleNamespace()
    fake.mint_installation_token = _fake_mint
    # The client delegates 401-refresh invalidation to the library's public
    # token-cache API.
    fake.clear_token_cache = lambda: None
    fake.invalidate_token_cache = lambda installation_id: None
    monkeypatch.setitem(sys.modules, "robotsix_github_auth", fake)


def _mock_installation_repos(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(
        url__startswith="https://api.github.com/installation/repositories"
    ).mock(
        return_value=httpx.Response(
            200,
            text=json.dumps(
                {
                    "repositories": [
                        {"full_name": "damien-robotsix/repo-a"},
                        {"full_name": "damien-robotsix/robotsix-central-deploy"},
                    ]
                }
            ),
        )
    )


async def _mock_component_request_blocked(
    _component_id: str,
    _method: str,
    _path: str,
    **_kw: Any,
) -> str:
    """Mock ``component_request`` that returns BLOCKED state with ≥3 cycles."""
    return "HTTP 200\n" + json.dumps(
        {
            "state": "blocked",
            "events": [
                {"type": "implement_start"},
                {"type": "implement_complete"},
                {"type": "implement_start"},
                {"type": "implement_complete"},
                {"type": "implement_start"},
                {"type": "implement_complete"},
            ],
        }
    )


async def _mock_component_request_error(
    _component_id: str,
    _method: str,
    _path: str,
    **_kw: Any,
) -> str:
    """Mock ``component_request`` that returns a transient 'Error:' response."""
    return "Error: connection refused"


# ---------------------------------------------------------------------------
# Shared patch-tool scaffolding (audit gap: repo_direct_test_scaffold_dup)
# ---------------------------------------------------------------------------

DEFAULT_INSTALLED_REPOS = ["org/repo"]

THREE_IMPLEMENT_CYCLES: list[dict[str, str]] = [
    {"type": "implement_start"},
    {"type": "implement_complete"},
    {"type": "implement_start"},
    {"type": "implement_complete"},
    {"type": "implement_start"},
    {"type": "implement_complete"},
]


def _board_ticket_payload(
    ticket_id: str,
    state: str,
    events: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build a minimal mill-board ticket payload for the patch preconditions."""
    payload: dict[str, Any] = {"id": ticket_id, "state": state}
    if events is not None:
        payload["events"] = events
    return payload


@pytest.fixture
def mock_installation_scope(
    respx_mock: respx.MockRouter,
) -> Callable[..., respx.Route]:
    """Mount ``GET /installation/repositories`` with the given installed repos.

    Defaults to ``["org/repo"]`` so in-scope tests can call it bare;
    out-of-scope refusal tests pass their own repo list; the
    component_request-bypass tests simply never call it.
    """

    def _mount(full_names: list[str] | None = None) -> respx.Route:
        repos = [
            {"full_name": name} for name in (full_names or DEFAULT_INSTALLED_REPOS)
        ]
        return respx_mock.get(
            url__startswith="https://api.github.com/installation/repositories"
        ).mock(
            return_value=httpx.Response(
                200,
                text=json.dumps({"repositories": repos}),
            )
        )

    return _mount


@pytest.fixture
def mock_board_ticket(
    respx_mock: respx.MockRouter,
) -> Callable[[str, dict[str, Any]], respx.Route]:
    """Mount ``GET /tickets/{ticket_id}`` returning the given payload."""

    def _mount(ticket_id: str, payload: dict[str, Any]) -> respx.Route:
        return respx_mock.get(f"http://127.0.0.1:8077/tickets/{ticket_id}").mock(
            return_value=httpx.Response(200, text=json.dumps(payload))
        )

    return _mount


@pytest.fixture
def mock_repo_catchalls(
    respx_mock: respx.MockRouter,
) -> Callable[[], None]:
    """Mount catch-all ``repos/org/repo`` GET/POST/PATCH routes.

    For tests whose tool invocation passes the guards and proceeds into the
    push internals, so the mock responses never hit the network.  Mounted on
    demand (call the returned function), which lets tests that register
    *specific* repo routes register them first and stay unshadowed.
    """

    def _mount() -> None:
        respx_mock.get(url__startswith="https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, text="{}")
        )
        respx_mock.post(url__startswith="https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, text="{}")
        )
        respx_mock.patch(url__startswith="https://api.github.com/repos/org/repo").mock(
            return_value=httpx.Response(200, text="{}")
        )

    return _mount


@dataclass(frozen=True)
class PatchScenario:
    """One ``patch_direct_repo_file`` precondition variant.

    *proceeds* is ``True`` when the guards should pass (no refusal); in that
    case the output must not contain ``"Refused"``.  Otherwise every *match*
    needle must appear in the output (matched case-insensitively, mirroring
    the mixed-case assertions of the original per-variant tests).
    """

    name: str
    ticket: dict[str, Any]
    scope: list[str] | None = None
    component_request: Callable[..., Any] | None = None
    proceeds: bool = False
    match: tuple[str, ...] = ()


# The precondition matrix for ``patch_direct_repo_file``: blocked-state,
# implement-cycle-count, installation-scope and component_request-fallback
# variants.  Single source of truth for the tool's refusal semantics — shared
# by every test module that exercises the tool.
PATCH_SCENARIOS: list[PatchScenario] = [
    PatchScenario(
        name="roster_fallback",
        ticket=_board_ticket_payload("t-pf-fb", "blocked", THREE_IMPLEMENT_CYCLES),
        component_request=_mock_component_request_error,
        proceeds=True,
    ),
    PatchScenario(
        name="rejects_non_blocked",
        ticket=_board_ticket_payload("t-pf1", "draft"),
        match=("refused", "blocked"),
    ),
    PatchScenario(
        name="rejects_few_cycles",
        ticket=_board_ticket_payload(
            "t-pf2", "blocked", [{"type": "implement_start", "timestamp": "..."}]
        ),
        match=("refused", "implement", "1"),
    ),
    PatchScenario(
        name="rejects_zero_cycles",
        ticket=_board_ticket_payload(
            "t-pf2z", "blocked", [{"type": "ticket_created", "timestamp": "..."}]
        ),
        match=("refused", "implement", "0"),
    ),
    PatchScenario(
        name="allows_enough_cycles",
        ticket=_board_ticket_payload("t-pf3", "blocked", THREE_IMPLEMENT_CYCLES),
        proceeds=True,
    ),
    PatchScenario(
        name="rejects_out_of_scope",
        ticket=_board_ticket_payload("t-pf4", "blocked", THREE_IMPLEMENT_CYCLES),
        scope=["org/other-repo"],
        match=("not installed", "install"),
    ),
]
