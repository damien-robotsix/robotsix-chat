"""Dedicated unit tests for :mod:`robotsix_chat.repo.direct.github_tools`.

These exercise the :func:`build_github_tools` factory and the decision
branches of individual tool closures directly, using lightweight fakes for
the ``DirectRepoClient`` / ``BoardClient`` and the precondition helpers,
rather than routing through the higher-level ``build_direct_repo_tools``
wrapper.  The point is to cover the pure request-shaping / result-parsing
logic (JSON validation, changelog-newline normalisation, default-PR-body,
mergeability summary formatting, precondition/scope pass-through) without
any network I/O.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from robotsix_chat.repo.direct.github_tools import (
    build_github_tools,
    check_simple_pr_file_safety,
)


class _FakeClient:
    """Minimal fake capturing kwargs and returning canned values."""

    def __init__(
        self,
        *,
        pr: dict[str, Any] | None = None,
        get_pr_exc: Exception | None = None,
        scope_error: str | None = None,
    ) -> None:
        """Store canned return values / side effects for the fake methods."""
        self._pr = pr
        self._get_pr_exc = get_pr_exc
        self._scope_error = scope_error
        self.pushed: dict[str, Any] | None = None
        self.created: dict[str, Any] | None = None
        self.updated: dict[str, Any] | None = None

    async def push_branch(self, **kwargs: Any) -> str:
        """Record the push kwargs and return a canned status."""
        self.pushed = kwargs
        return "pushed-ok"

    async def create_pr(self, **kwargs: Any) -> str:
        """Record the create-PR kwargs and return a canned status."""
        self.created = kwargs
        return "pr-created"

    async def update_pr_branch(self, **kwargs: Any) -> str:
        """Record the update kwargs and return a canned status."""
        self.updated = kwargs
        return "update-queued"

    async def get_pr(self, **kwargs: Any) -> dict[str, Any]:
        """Return the canned PR dict, or raise the configured exception."""
        if self._get_pr_exc is not None:
            raise self._get_pr_exc
        assert self._pr is not None
        return self._pr

    async def check_installation_scope(self, repo_full_name: str) -> str | None:
        """Return the configured scope error (or ``None`` to pass)."""
        return self._scope_error


def _build(
    *,
    client: _FakeClient | None = None,
    component_request: Any = None,
    blocked_error: str | None = None,
) -> dict[str, Any]:
    """Build the tools with fakes and return a name -> callable mapping."""

    async def _pass(*_a: Any, **_k: Any) -> str | None:
        return None

    async def _blocked(*_a: Any, **_k: Any) -> str | None:
        return blocked_error

    tools = build_github_tools(
        client=cast(Any, client or _FakeClient()),
        board=cast(Any, object()),
        settings=cast(Any, object()),
        component_request=component_request,
        assert_blocked_and_scoped=_blocked if blocked_error else _pass,
        assert_in_scope=_pass,
    )
    return {t.__name__: t for t in tools}


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_build_github_tools_returns_all_named_async_tools() -> None:
    """The factory returns the full set of uniquely-named callable tools."""

    async def _pass(*_a: Any, **_k: Any) -> str | None:
        return None

    tools = build_github_tools(
        client=cast(Any, _FakeClient()),
        board=cast(Any, object()),
        settings=cast(Any, object()),
        component_request=None,
        assert_blocked_and_scoped=_pass,
        assert_in_scope=_pass,
    )
    names = [t.__name__ for t in tools]
    assert len(tools) == 24
    # No duplicate registrations.
    assert len(names) == len(set(names))
    assert all(callable(t) for t in tools)
    for expected in (
        "push_direct_repo_branch",
        "open_direct_repo_pr",
        "open_simple_repo_pr",
        "check_pr_merge_conflict",
        "verify_pr_ci_status",
        "merge_direct_repo_pr",
    ):
        assert expected in names


# ---------------------------------------------------------------------------
# push_direct_repo_branch
# ---------------------------------------------------------------------------


class TestPushDirectRepoBranch:
    """Input-validation and normalisation branches of the push tool."""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self) -> None:
        """Non-JSON ``files_json`` yields a validation error, not a crash."""
        tools = _build()
        result = await tools["push_direct_repo_branch"](
            "id", "o/r", "branch", "not-json"
        )
        assert "valid JSON array" in result

    @pytest.mark.asyncio
    async def test_non_list_json_returns_error(self) -> None:
        """A JSON object (not array) is rejected with a clear message."""
        tools = _build()
        result = await tools["push_direct_repo_branch"](
            "id", "o/r", "branch", '{"path": "a"}'
        )
        assert "must be a JSON array" in result

    @pytest.mark.asyncio
    async def test_precondition_failure_short_circuits(self) -> None:
        """A failing BLOCKED/scope precondition is returned verbatim."""
        tools = _build(blocked_error="ticket not blocked")
        result = await tools["push_direct_repo_branch"]("id", "o/r", "branch", "[]")
        assert result == "ticket not blocked"

    @pytest.mark.asyncio
    async def test_changelog_fragment_gets_trailing_newline(self) -> None:
        """A ``changelog.d/*.md`` fragment without a newline gets one added."""
        client = _FakeClient()
        tools = _build(client=client)
        await tools["push_direct_repo_branch"](
            "id",
            "o/r",
            "branch",
            '[{"path": "changelog.d/1.misc.md", "content": "note"}]',
        )
        assert client.pushed is not None
        assert client.pushed["files"][0]["content"] == "note\n"

    @pytest.mark.asyncio
    async def test_non_changelog_file_is_not_modified(self) -> None:
        """Non-changelog file content is pushed unchanged."""
        client = _FakeClient()
        tools = _build(client=client)
        await tools["push_direct_repo_branch"](
            "id",
            "o/r",
            "branch",
            '[{"path": "src/a.py", "content": "code"}]',
        )
        assert client.pushed is not None
        assert client.pushed["files"][0]["content"] == "code"


# ---------------------------------------------------------------------------
# open_direct_repo_pr
# ---------------------------------------------------------------------------


class TestOpenDirectRepoPr:
    """Precondition and default-body branches of the open-PR tool."""

    @pytest.mark.asyncio
    async def test_precondition_failure_short_circuits(self) -> None:
        """A failing precondition is returned without calling create_pr."""
        tools = _build(blocked_error="nope")
        assert await tools["open_direct_repo_pr"]("id", "o/r", "b", "t") == "nope"

    @pytest.mark.asyncio
    async def test_default_body_notes_auto_merge_disabled(self) -> None:
        """An empty body is replaced with the ticket-referencing default."""
        client = _FakeClient()
        tools = _build(client=client)
        await tools["open_direct_repo_pr"]("id-123", "o/r", "b", "title")
        assert client.created is not None
        assert "Auto-merge is disabled" in client.created["body"]
        assert "id-123" in client.created["body"]

    @pytest.mark.asyncio
    async def test_explicit_body_is_preserved(self) -> None:
        """A caller-supplied body is passed through unchanged."""
        client = _FakeClient()
        tools = _build(client=client)
        await tools["open_direct_repo_pr"]("id", "o/r", "b", "title", "custom body")
        assert client.created is not None
        assert client.created["body"] == "custom body"


# ---------------------------------------------------------------------------
# check_pr_merge_conflict
# ---------------------------------------------------------------------------


class TestCheckPrMergeConflict:
    """Mergeability-summary formatting for the conflict-check tool."""

    @pytest.mark.asyncio
    async def test_mergeable_true_reports_no_conflict(self) -> None:
        """``mergeable=True`` renders the no-conflict line."""
        tools = _build(client=_FakeClient(pr={"mergeable": True}))
        result = await tools["check_pr_merge_conflict"]("id", "o/r", 7)
        assert "No merge conflicts detected" in result

    @pytest.mark.asyncio
    async def test_mergeable_false_reports_conflict_and_extra_fields(self) -> None:
        """``mergeable=False`` renders the conflict line plus extra fields."""
        tools = _build(
            client=_FakeClient(
                pr={
                    "mergeable": False,
                    "mergeable_state": "dirty",
                    "title": "My PR",
                    "html_url": "https://example/pr/7",
                    "draft": True,
                }
            )
        )
        result = await tools["check_pr_merge_conflict"]("id", "o/r", 7)
        assert "Merge conflicts detected" in result
        assert "Mergeable state: dirty" in result
        assert "draft: True" in result

    @pytest.mark.asyncio
    async def test_mergeable_none_reports_pending(self) -> None:
        """``mergeable=None`` renders the still-computing hint."""
        tools = _build(client=_FakeClient(pr={"mergeable": None}))
        result = await tools["check_pr_merge_conflict"]("id", "o/r", 7)
        assert "still being computed" in result

    @pytest.mark.asyncio
    async def test_get_pr_exception_is_reported(self) -> None:
        """A ``get_pr`` failure is caught and surfaced as an error string."""
        tools = _build(client=_FakeClient(get_pr_exc=RuntimeError("boom")))
        result = await tools["check_pr_merge_conflict"]("id", "o/r", 7)
        assert "Error fetching PR #7" in result
        assert "boom" in result

    @pytest.mark.asyncio
    async def test_precondition_failure_short_circuits(self) -> None:
        """A failing precondition is returned before any PR fetch."""
        tools = _build(blocked_error="blocked-msg")
        assert await tools["check_pr_merge_conflict"]("id", "o/r", 7) == "blocked-msg"


# ---------------------------------------------------------------------------
# verify_pr_ci_status (read-only: scope check, no BLOCKED requirement)
# ---------------------------------------------------------------------------


class TestVerifyPrCiStatus:
    """Scope-gate branch of the read-only CI-status tool."""

    @pytest.mark.asyncio
    async def test_scope_error_short_circuits_without_component_request(self) -> None:
        """Without a component credential, a scope error short-circuits."""
        tools = _build(
            client=_FakeClient(scope_error="out of scope"),
            component_request=None,
        )
        assert await tools["verify_pr_ci_status"]("o/r", 7) == "out of scope"


# ---------------------------------------------------------------------------
# check_simple_pr_file_safety (module-level guard)
# ---------------------------------------------------------------------------


class TestCheckSimplePrFileSafety:
    """The risky-file guard for the ungated simple-PR path."""

    def test_plain_content_edits_pass(self) -> None:
        """Ordinary content/doc/source edits are eligible."""
        files = [
            {"path": "content/projects.html", "content": "<div>THALAMUS</div>"},
            {"path": "docs/readme.md", "content": "hi"},
            {"path": "src/app.py", "content": "x = 1"},
            {"path": ".env.example", "content": "KEY="},
        ]
        assert check_simple_pr_file_safety(files) is None

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/ci.yml",
            "/.github/workflows/ci.yml",
            ".github/actions/build/action.yml",
        ],
    )
    def test_workflow_files_refused(self, path: str) -> None:
        """CI workflow / action files are refused on the ungated path."""
        result = check_simple_pr_file_safety([{"path": path, "content": "x"}])
        assert result is not None
        assert "workflow" in result.lower()

    @pytest.mark.parametrize(
        "path",
        ["deploy.pem", "secrets/server.key", ".env", ".env.local", "keys/id_rsa"],
    )
    def test_secret_files_refused(self, path: str) -> None:
        """Credential/secret-shaped files are refused on the ungated path."""
        result = check_simple_pr_file_safety([{"path": path, "content": "x"}])
        assert result is not None
        assert "secret" in result.lower() or "credential" in result.lower()


# ---------------------------------------------------------------------------
# open_simple_repo_pr (ungated: scope check, NO BLOCKED requirement)
# ---------------------------------------------------------------------------


class TestOpenSimpleRepoPr:
    """The lightweight ungated direct-PR tool."""

    @pytest.mark.asyncio
    async def test_happy_path_pushes_and_opens_pr(self) -> None:
        """A simple change pushes a branch and opens a PR without a ticket."""
        client = _FakeClient()
        tools = _build(client=client)
        result = await tools["open_simple_repo_pr"](
            "o/r",
            "chore/add-card",
            '[{"path": "content/x.html", "content": "hi"}]',
            "chore: add card",
        )
        assert "pushed-ok" in result
        assert "pr-created" in result
        # No ticket_id gate — a synthetic marker is used for traceability.
        assert client.pushed is not None
        assert client.pushed["ticket_id"] == "simple-pr"
        assert client.pushed["commit_message"] == "chore: add card"
        assert client.created is not None
        assert client.created["head_branch"] == "chore/add-card"

    @pytest.mark.asyncio
    async def test_invalid_json_returns_error(self) -> None:
        """Non-JSON ``files_json`` yields a validation error, not a crash."""
        tools = _build()
        result = await tools["open_simple_repo_pr"]("o/r", "b", "not-json", "title")
        assert "valid JSON array" in result

    @pytest.mark.asyncio
    async def test_workflow_file_refused(self) -> None:
        """A workflow-file change is refused before any push."""
        client = _FakeClient()
        tools = _build(client=client)
        result = await tools["open_simple_repo_pr"](
            "o/r",
            "b",
            '[{"path": ".github/workflows/ci.yml", "content": "x"}]',
            "ci: tweak",
        )
        assert "Refused" in result
        assert client.pushed is None

    @pytest.mark.asyncio
    async def test_scope_error_short_circuits(self) -> None:
        """A scope error short-circuits before any push."""

        async def _scope_fail(*_a: Any, **_k: Any) -> str | None:
            return "out of scope"

        async def _pass(*_a: Any, **_k: Any) -> str | None:
            return None

        client = _FakeClient()
        tools = {
            t.__name__: t
            for t in build_github_tools(
                client=cast(Any, client),
                board=cast(Any, object()),
                settings=cast(Any, object()),
                component_request=None,
                assert_blocked_and_scoped=_pass,
                assert_in_scope=_scope_fail,
            )
        }
        result = await tools["open_simple_repo_pr"](
            "o/r", "b", '[{"path": "x.md", "content": "hi"}]', "docs: x"
        )
        assert result == "out of scope"
        assert client.pushed is None

    @pytest.mark.asyncio
    async def test_push_failure_skips_pr(self) -> None:
        """A push failure returns early without opening a PR."""

        class _FailPush(_FakeClient):
            async def push_branch(self, **kwargs: Any) -> str:
                self.pushed = kwargs
                return "Error pushing branch: boom"

        client = _FailPush()
        tools = _build(client=client)
        result = await tools["open_simple_repo_pr"](
            "o/r", "b", '[{"path": "x.md", "content": "hi"}]', "docs: x"
        )
        assert result == "Error pushing branch: boom"
        assert client.created is None
