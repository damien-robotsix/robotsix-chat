"""GitHub Actions tools for the chat agent.

Exposes :func:`build_github_actions_tools` — a factory returning LLM tools
for managing repository Actions secrets, dispatching workflows, and
diagnosing CI failures via the GitHub App installation.

Also exposes :func:`load_github_actions_skill` which returns the component
skill markdown — a description of the Actions endpoints, their auth requirements,
and their confirmation-gated mutation policy.

Returns no tools when the capability is disabled.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings, GitHubActionsSettings

__all__ = ["build_github_actions_tools", "load_github_actions_skill"]


def load_github_actions_skill() -> str:
    """Return the GitHub Actions component skill markdown.

    Reads ``skill.md`` (shipped next to this module) and returns it as a
    string suitable for appending to the agent's system prompt.  Returns
    an empty string when the file is missing, so a missing skill document
    never prevents the agent from starting.

    """
    skill_path = Path(__file__).parent / "skill.md"
    try:
        return skill_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def build_github_actions_tools(
    github_actions: GitHubActionsSettings,
    direct_repo: DirectRepoSettings,
) -> list[Callable[..., Any]]:
    """Return GitHub Actions tool(s) for the agent, or ``[]`` when disabled."""
    if not github_actions.enabled:
        return []

    from robotsix_chat.repo.direct.actions_client import (
        ActionsClient,
        StartupFailureClass,
    )

    actions = ActionsClient(direct_repo)
    client = actions._client  # DirectRepoClient for scope checks
    org = github_actions.github_org

    async def set_actions_secret(
        repo_name: str,
        secret_name: str,
        secret_value: str,
    ) -> str:
        """Set or update a repository Actions secret.

        **Confirmation-gated.** Before calling, confirm the repo and secret
        name with the user in-chat.  The secret value is encrypted with the
        repo's public key before transmission.

        **Scope:** Only repos within the GitHub App's current installation
        scope are modifiable — this is checked dynamically at call time.

        Args:
            repo_name: Repository name (not owner/name) — the org is
                configured server-side (default ``damien-robotsix``).
            secret_name: The name of the secret (e.g. ``"OVH_SFTP_HOST"``).
            secret_value: The plaintext value to store.  Neither the server
                nor the agent retains this after the API call.

        Returns:
            A success message on completion, or an error message describing
            why the request was refused or failed.

        """
        repo_full_name = f"{org}/{repo_name}"

        if scope_error := await client.check_installation_scope(repo_full_name):
            return scope_error

        return await actions.set_actions_secret(
            repo_full_name,
            secret_name=secret_name,
            secret_value=secret_value,
        )

    async def dispatch_workflow(
        repo_name: str,
        workflow_id: str,
        ref: str = "main",
        inputs: str | None = None,
    ) -> str:
        """Trigger a workflow_dispatch on a repository workflow.

        **Confirmation-gated.** Before calling, confirm the repo, workflow,
        ref, and inputs with the user in-chat.

        **Scope:** Only repos within the GitHub App's current installation
        scope are modifiable — this is checked dynamically at call time.

        Args:
            repo_name: Repository name (not owner/name) — the org is
                configured server-side (default ``damien-robotsix``).
            workflow_id: The workflow file name (e.g. ``"deploy.yml"``) or
                numeric workflow ID.
            ref: The branch or tag to run the workflow on (default ``"main"``).
            inputs: Optional JSON object string with workflow input keys/values
                (e.g. ``'{"environment": "production"}'``).

        Returns:
            A success message on completion, or an error message describing
            why the request was refused or failed.

        """
        repo_full_name = f"{org}/{repo_name}"

        if scope_error := await client.check_installation_scope(repo_full_name):
            return scope_error

        parsed_inputs: dict[str, str] | None = None
        if inputs:
            import json

            try:
                parsed = json.loads(inputs)
                if not isinstance(parsed, dict):
                    return (
                        f"Error: inputs must be a JSON object, "
                        f"got {type(parsed).__name__}"
                    )
                parsed_inputs = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError as exc:
                return f"Error parsing inputs JSON: {exc}"

        return await actions.dispatch_workflow(
            repo_full_name,
            workflow_id=workflow_id,
            ref=ref,
            inputs=parsed_inputs,
        )

    async def check_workflow_run(
        repo_name: str,
        *,
        branch: str | None = None,
        run_id: int | None = None,
    ) -> str:
        """Fetch recent workflow runs and diagnose common failure patterns.

        Use this to investigate a CI failure that has no obvious cause.
        The tool inspects recent workflow runs and detects known failure
        signatures — including zero-job runs and never-started runs.
        A ``startup_failure`` run (zero jobs, no logs) is classified
        deterministically by checking sibling workflows on the same commit
        (``head_sha``): if any sibling reached real job execution, the
        failure is a per-workflow config issue; only when every workflow
        on the commit produced zero jobs is it an account/runner/billing
        issue.  The diagnosis is visibility-aware: for public repos,
        billing is never suggested as a cause; guidance focuses on trigger
        misconfigurations, missing reusable workflow files, and
        input-contract mismatches instead.

        **Read-only.**  Does not modify any repository state.

        Args:
            repo_name: Repository name (not owner/name).
            branch: Optional branch filter (e.g. ``"main"``).
            run_id: Optional specific run ID to inspect in detail.
                When omitted the most recent runs are checked.

        Returns:
            A diagnostic summary: either a recognised failure pattern, a
            summary of recent runs, or an error message.

        """
        repo_full_name = f"{org}/{repo_name}"

        if scope_error := await client.check_installation_scope(repo_full_name):
            return scope_error

        if run_id is not None:
            # Single-run deep inspection
            jobs = await actions.get_workflow_run_jobs(repo_full_name, run_id)
            if not jobs:
                # -- deterministic startup_failure classification --
                run = await actions.get_workflow_run(repo_full_name, run_id)
                if run:
                    classification = await actions.classify_startup_failure_run(
                        repo_full_name, run
                    )
                else:
                    classification = None
                if classification is not None:
                    if classification.classification is StartupFailureClass.PER_WORKFLOW_CONFIG:
                        return (
                            f"Workflow run {run_id} on {repo_full_name} has no "
                            f"jobs (conclusion: {run.get('conclusion')}) — "
                            f"{classification.summary}.  The account/runner/"
                            f"billing plane is provably fine — the root cause "
                            f"is in this workflow's own file (trigger, "
                            f"permissions, or reusable-workflow ``uses:``)."
                        )
                    return (
                        f"Workflow run {run_id} on {repo_full_name} has no "
                        f"jobs (conclusion: {run.get('conclusion')}) — "
                        f"{classification.summary}.  Every workflow on this "
                        f"commit produced zero jobs, which points at the "
                        f"account/runner/billing plane (Actions disabled, "
                        f"billing lapse, or no available runners).  This is "
                        f"an operator-action ticket, NOT a workflow-file edit."
                    )
                # -- fallback: no run metadata or sibling listing failed --
                is_private = await actions.check_repo_visibility(repo_full_name)
                if is_private is True:
                    return (
                        f"Workflow run {run_id} on {repo_full_name} has no jobs — "
                        f"this may indicate that GitHub Actions billing "
                        f"is not enabled for this private repository, or that the "
                        f"workflow trigger is misconfigured (e.g. only triggers on "
                        f"``push`` to ``main``, not on the event that created this "
                        f"run).  Check the workflow's ``on:`` trigger in "
                        f"``.github/workflows/``, and verify billing at "
                        f"Settings > Actions > General."
                    )
                elif is_private is False:
                    return (
                        f"Workflow run {run_id} on {repo_full_name} has no jobs. "
                        f"Since {repo_full_name} is a public repository, billing "
                        f"is not the issue.  The likely causes are a missing "
                        f"reusable workflow file (``.github/workflows/``), an "
                        f"input-contract mismatch (e.g. a required "
                        f"``workflow_call`` input not provided), or a "
                        f"misconfigured trigger in the workflow's ``on:`` block."
                    )
                else:
                    return (
                        f"Workflow run {run_id} on {repo_full_name} has no jobs — "
                        f"this may indicate that GitHub Actions billing "
                        f"is not enabled for this private repository, or that the "
                        f"workflow trigger is misconfigured (e.g. only triggers on "
                        f"``push`` to ``main``, not on the event that created this "
                        f"run).  Check the workflow's ``on:`` trigger in "
                        f"``.github/workflows/``, and verify billing at "
                        f"Settings > Actions > General."
                    )
            lines: list[str] = [
                f"Workflow run {run_id} on {repo_full_name} — {len(jobs)} job(s):"
            ]
            for j in jobs:
                lines.append(
                    f"  - {j.get('name', '?')}: "
                    f"status={j.get('status')}, "
                    f"conclusion={j.get('conclusion')}"
                )
            return "\n".join(lines)

        # Broad scan of recent runs
        runs = await actions.list_workflow_runs(
            repo_full_name, branch=branch, per_page=5
        )
        if not runs:
            return (
                f"No recent workflow runs found for {repo_full_name}"
                + (f" on branch '{branch}'" if branch else "")
                + "."
            )

        # Check for billing-failure signature first
        billing_diag = await actions._diagnose_billing_failure(runs, repo_full_name)
        if billing_diag:
            return billing_diag

        # Otherwise summarise recent runs
        lines = [
            f"Recent workflow runs for {repo_full_name}"
            + (f" on '{branch}'" if branch else "")
            + ":"
        ]
        for r in runs[:5]:
            lines.append(
                f"  - {r.get('name', '?')} (id {r.get('id')}): "
                f"status={r.get('status')}, "
                f"conclusion={r.get('conclusion')}, "
                f"event={r.get('event')}"
            )
        return "\n".join(lines)

    async def fetch_workflow_run_annotations(
        repo_name: str,
        run_id: int,
    ) -> str:
        """Fetch annotations for all check runs in a GitHub Actions workflow run.

        Annotations are the inline diagnostic messages that GitHub Actions
        surfaces on files in the "Files changed" tab and the workflow run
        summary — linter warnings, compiler errors, test failure details,
        etc.  This tool returns them verbatim as Markdown, grouped by
        check run.

        Use this when a CI run fails and you need the exact annotation
        text to diagnose the root cause (rather than rendering the entire
        GitHub Actions UI page, which can produce very large blobs).

        **Read-only.**  Does not modify any repository state.

        Args:
            repo_name: Repository name (not owner/name) — the org is
                configured server-side (default ``damien-robotsix``).
            run_id: The workflow run id (the numeric id shown in the
                Actions tab URL and returned by ``check_workflow_run``).

        Returns:
            A Markdown-formatted string with all annotations grouped by
            check run, or an error/diagnostic message when none are found.

        """
        repo_full_name = f"{org}/{repo_name}"

        if scope_error := await client.check_installation_scope(repo_full_name):
            return scope_error

        return await actions.get_workflow_run_annotations(repo_full_name, run_id)

    async def fetch_job_log(
        repo_name: str,
        job_id: int,
    ) -> str:
        """Fetch the raw plain-text log for a GitHub Actions job.

        The GitHub API returns a 302 redirect to a signed URL; this tool
        follows it server-side and returns the log content directly.
        Long logs are truncated at 8000 characters.

        Use this as a fallback when ``fetch_workflow_run_annotations``
        returns a permission error (403) — job logs use a different API
        endpoint that may still be accessible.

        **Read-only.**  Does not modify any repository state.

        Args:
            repo_name: Repository name (not owner/name) — the org is
                configured server-side (default ``damien-robotsix``).
            job_id: The GitHub Actions job ID (integer, found in the
                Actions tab URL or from ``check_workflow_run`` output).

        Returns:
            The raw job log text (possibly truncated), or an error message.

        """
        repo_full_name = f"{org}/{repo_name}"

        if scope_error := await client.check_installation_scope(repo_full_name):
            return scope_error

        try:
            log = await actions.get_job_log(repo_full_name, job_id)
        except RuntimeError as exc:
            return (
                f"Error fetching job log for job {job_id} on "
                f"{repo_full_name}: {exc}\n\n"
                f"**Suggestion:** check the logs manually at "
                f"https://github.com/{repo_full_name}"
                f"/actions/runs?query=job_id%3A{job_id}"
            )

        if len(log) > 8000:
            log = log[:8000] + "\n\n... [log truncated at 8000 chars]"
        return log

    return [
        set_actions_secret,
        dispatch_workflow,
        check_workflow_run,
        fetch_workflow_run_annotations,
        fetch_job_log,
    ]
