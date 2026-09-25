"""CI-monitoring & troubleshooting tool closures for build_github_tools, split out.

The six tool closures below (``check_ci_health``, ``rerun_ci_workflow``,
``fetch_ci_job_logs``, ``fetch_trivy_findings``,
``file_ci_stabilization_ticket``, ``verify_pr_ci_status``) were extracted
verbatim from
:func:`robotsix_chat.repo.direct.github_tools.build_github_tools` to keep that
factory manageable.  ``build_github_tools`` calls :func:`build_github_tools_ci`
and splices the returned tools back into its result list at their original
positions, so the public tool set (names, count, order) is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from .actions_client import ActionsClient, StartupFailureClass

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

    from .board_client import BoardClient
    from .client import DirectRepoClient

logger = logging.getLogger(__name__)


def build_github_tools_ci(
    *,
    client: DirectRepoClient,
    board: BoardClient,
    settings: DirectRepoSettings,
    component_request: Callable[..., Any] | None,
    resolve_pr_ref: Callable[..., Awaitable[tuple[str, int] | str]],
) -> list[Callable[..., Any]]:
    """Build and return the 6 CI tool closures.

    These are the exact same tool objects ``build_github_tools`` used to define
    inline; only their captured names now come from this function's parameters
    (``ActionsClient`` / ``StartupFailureClass`` are imported at module scope).
    """
    async def check_ci_health(
        repo_full_name: str = "",
        branch: str = "",
        repo: str = "",
    ) -> str:
        """Check recent CI history for a repository branch and classify failures.

        Lists the most recent workflow runs on *branch* (default: the
        repository's default branch) and compares the latest run against the
        most recent green run.  Use this BEFORE asserting that a CI failure is
        pre-existing — never rely on cached or inferred status.  This is the
        first step when a deployment is blocked because a dependent PR cannot
        merge due to CI failures on the base branch.

        **Read-only.** Does not modify any state and does not require a ticket
        to be in BLOCKED state.

        Args:
            repo: Alias of ``repo_full_name`` (bare repo names are
                resolved via the board roster like the other GitHub tools);
                the claude_sdk schema check rejected ``repo=`` calls before
                the body ran (2026-09-14 drain runs).
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            branch: Branch to inspect.  Defaults to the repository's default
                branch when empty.

        Returns:
            A multi-line summary of recent runs plus a verdict: whether the
            latest failure is pre-existing (an earlier recent run was green)
            and whether a rerun or escalation is recommended.

        """
        repo_arg = repo.strip()
        alias_arg = repo_full_name.strip()
        if repo_arg and alias_arg and repo_arg != alias_arg:
            return (
                f"Error: repo={repo!r} and repo_full_name={repo_full_name!r} "
                "disagree; pass only one of them."
            )
        repo_full_name = alias_arg or repo_arg
        if not repo_full_name:
            return "Error: repo_full_name is required."
        if "/" not in repo_full_name:
            resolved = await board.resolve_repo_full_name(repo_full_name)
            if resolved is None:
                return (
                    f"Error: could not resolve repo {repo_full_name!r} via the "
                    "board roster; pass the full GitHub owner/name."
                )
            repo_full_name = resolved
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        actions_client = ActionsClient(settings)
        target_branch = branch or await actions_client.get_default_branch(
            repo_full_name
        )

        try:
            runs = await actions_client.list_workflow_runs(
                repo_full_name,
                branch=target_branch,
                per_page=20,
                raise_on_error=True,
            )
        except Exception as exc:
            return f"Error checking CI health for {repo_full_name}: {exc}"

        lines = [f"CI health for {repo_full_name} branch '{target_branch}':"]
        if not runs:
            lines.append("No recent workflow runs found.")
            lines.append(
                "Recommendation: escalate — there is no CI history available "
                "to compare against."
            )
            return "\n".join(lines)

        lines.append(f"{len(runs)} recent run(s):")
        for r in runs:
            lines.append(
                f"  - {r.get('name', '?')} (run {r.get('id')}): "
                f"status={r.get('status')}, conclusion={r.get('conclusion')}"
            )

        latest = runs[0]
        latest_conclusion = (latest.get("conclusion") or "").lower()

        if latest_conclusion == "startup_failure":
            # Zero-job run — classify deterministically via siblings on the
            # same commit instead of guessing between billing and config.
            lines.append(
                "Verdict: STARTUP FAILURE — the latest run produced zero "
                "jobs (GitHub rejected the workflow file before any job "
                "started)."
            )
            classification = await actions_client.classify_startup_failure_run(
                repo_full_name, latest
            )
            if classification is not None:
                lines.append(
                    f"Startup-failure classification: {classification.summary}"
                )
                if (
                    classification.classification
                    is StartupFailureClass.PER_WORKFLOW_CONFIG
                ):
                    lines.append(
                        "The account/runner plane is provably fine "
                        "— diagnose this workflow's own file (trigger, "
                        "permissions, reusable-workflow ``uses:``)."
                    )
                else:
                    lines.append(
                        "Every workflow on this commit produced zero jobs — "
                        "treat as an account/runner issue (operator "
                        "action), NOT a workflow-file edit."
                    )
            return "\n".join(lines)

        recent_green = next(
            (r for r in runs if (r.get("conclusion") or "").lower() == "success"),
            None,
        )

        failing = {"failure", "cancelled", "timed_out", "startup_failure"}
        if latest_conclusion in failing and recent_green is not None:
            lines.append(
                "Verdict: PRE-EXISTING failure — the latest run is failing but "
                f"an earlier recent run ({recent_green.get('name', '?')} run "
                f"{recent_green.get('id')}) was green, so branch "
                f"'{target_branch}' is red independently of any dependent PR."
            )
            lines.append(
                "Recommendation: rerun the failing run or file a CI "
                "stabilization ticket."
            )
        elif latest_conclusion == "success":
            lines.append("Verdict: GREEN — the latest run on this branch succeeded.")
        elif latest_conclusion in failing:
            lines.append(
                "Verdict: FAILING but no green run in the recent window — "
                "cannot confirm the failure is pre-existing from this history "
                "alone; widen the window or inspect the failing job logs."
            )
        else:
            lines.append(
                f"Verdict: latest run is '{latest.get('status')}' "
                f"(conclusion='{latest.get('conclusion')}') — no final verdict yet."
            )

        return "\n".join(lines)

    async def rerun_ci_workflow(
        repo_full_name: str,
        branch: str = "",
        run_id: int = 0,
    ) -> str:
        """Re-run a failed CI workflow run on a repository branch.

        When *run_id* is provided it re-runs that specific run; otherwise it
        re-runs the most recent failed run on *branch* (default: the
        repository's default branch).  Use this after ``check_ci_health``
        confirms a CI failure when a deployment is blocked by a dependent PR
        that cannot merge.

        **This is a confirmation-gated mutation.** Only re-run after the
        operator has explicitly consented in the conversation.  The endpoint
        triggers a new CI run (consuming Actions minutes); it does not modify
        repository source.

        **No BLOCKED-state requirement.** This is a follow-up remediation
        operation, like merge/auto-merge — it does not require a ticket to be
        in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name``.
            branch: Branch whose latest failed run should be re-run when
                *run_id* is not supplied.
            run_id: Specific workflow run id to re-run (optional).

        Returns:
            A status message, or an error describing why the re-run failed.

        """
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        actions_client = ActionsClient(settings)
        if not run_id:
            target_branch = branch or await actions_client.get_default_branch(
                repo_full_name
            )
            try:
                runs = await actions_client.list_workflow_runs(
                    repo_full_name,
                    branch=target_branch,
                    per_page=20,
                    raise_on_error=True,
                )
            except Exception as exc:
                return f"Error listing workflow runs for {repo_full_name}: {exc}"
            failed = next(
                (
                    r
                    for r in runs
                    if (r.get("conclusion") or "").lower()
                    in {"failure", "cancelled", "timed_out", "startup_failure"}
                ),
                None,
            )
            if failed is None:
                return (
                    f"No failed workflow run found on '{target_branch}' in "
                    f"{repo_full_name} — nothing to re-run."
                )
            resolved_id = failed.get("id")
            if not isinstance(resolved_id, int):
                return "Error: could not determine the failed run id."
            run_id = resolved_id

        return await actions_client.rerun_workflow_run(repo_full_name, run_id)

    async def fetch_ci_job_logs(
        repo_full_name: str = "",
        run_id: int = 0,
        branch: str = "",
        job_name: str = "",
        max_log_bytes: int = 16_000,
        repo: str = "",
    ) -> str:
        """Fetch and parse job logs from a CI workflow run.

        Retrieves detailed output from a specific workflow run, optionally
        filtering by job name. Useful for extracting specific error details
        from CI failures (e.g., Trivy scan output, test results, build errors).

        **Read-only.** Does not modify any repository state.
        **No BLOCKED-state requirement.** This is a pure diagnostic tool.

        When *run_id* is omitted, the most recent failed run on *branch*
        (default: the repository's default branch) is used.  When provided,
        *branch* is unused.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g. ``"org/repo"``).
            repo: Alias of ``repo_full_name``; the claude_sdk schema check
                rejected ``repo=`` calls before the body ran (2026-09-17,
                correlation ab33c37a…).
            run_id: Specific workflow run id (optional; defaults to latest failed).
            branch: Branch to search for failing runs when *run_id* is 0.
            job_name: Optional filter to only return logs from jobs whose name
                contains this substring (case-insensitive). When empty, returns
                all job logs.
            max_log_bytes: Maximum total bytes of log content to return.
                Defaults to 16 KB; truncated logs include an indication.

        Returns:
            A formatted log summary including run metadata, job names, and
            (when accessible) the raw log content from each matching job.

        """
        repo_arg = repo.strip()
        alias_arg = repo_full_name.strip()
        if repo_arg and alias_arg and repo_arg != alias_arg:
            return (
                f"Error: repo={repo!r} and repo_full_name={repo_full_name!r} "
                "disagree; pass only one of them."
            )
        repo_full_name = alias_arg or repo_arg
        if not repo_full_name:
            return "Error: repo_full_name is required."

        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        actions_client = ActionsClient(settings)

        # Resolve run_id when not provided.
        if not run_id:
            target_branch = branch or await actions_client.get_default_branch(
                repo_full_name
            )
            try:
                runs = await actions_client.list_workflow_runs(
                    repo_full_name,
                    branch=target_branch,
                    per_page=20,
                    raise_on_error=True,
                )
            except Exception as exc:
                return (
                    f"Error: could not list workflow runs for {repo_full_name}: {exc}"
                )
            if not runs:
                return (
                    f"No recent workflow runs found on '{target_branch}' in "
                    f"{repo_full_name}."
                )

            def _is_failure(result: str) -> bool:
                return result in {
                    "failure",
                    "timed_out",
                    "action_required",
                }

            failed_run = next(
                (r for r in runs if _is_failure((r.get("conclusion") or "").lower())),
                None,
            )
            if failed_run is None:
                # No suffering failure but return the latest run's info.
                latest = runs[0]
                resolved_id = latest.get("id")
                if not isinstance(resolved_id, int):
                    return (
                        f"No failed or actionable workflow run found "
                        f"on '{target_branch}' in {repo_full_name}."
                    )
                run_id = resolved_id
            else:
                resolved_id = failed_run.get("id")
                if not isinstance(resolved_id, int):
                    return (
                        f"Error: could not determine run id from the "
                        f"failing workflow run in {repo_full_name}."
                    )
                run_id = resolved_id

        # Fetch the workflow run metadata.
        run_data = await actions_client.get_workflow_run(repo_full_name, run_id)
        if run_data is None:
            return f"Error: workflow run {run_id} not found in {repo_full_name}."

        run_name = run_data.get("name", "(unknown)")
        run_status = run_data.get("status", "?")
        run_conclusion = run_data.get("conclusion", "?")
        run_branch = run_data.get("head_branch", "?")

        lines: list[str] = [
            f"## CI Job Logs — {run_name}",
            f"Workflow run: {run_id} in {repo_full_name}",
            f"Branch: {run_branch}",
            f"Status: {run_status} | Conclusion: {run_conclusion}",
            "",
        ]

        # Fetch all jobs for the run.
        jobs = await actions_client.get_workflow_run_jobs(repo_full_name, run_id)
        if not jobs:
            lines.append("No jobs found for this workflow run.")
            return "\n".join(lines)

        # Filter by job_name if provided.
        if job_name:
            name_lower = job_name.lower()
            matching_jobs = [j for j in jobs if name_lower in j.get("name", "").lower()]
            if not matching_jobs:
                available = ", ".join(j.get("name", "?") for j in jobs)
                lines.append(f"No job named '{job_name}' found in run {run_id}.")
                lines.append(f"Available jobs: {available}")
                return "\n".join(lines)
            jobs = matching_jobs

        lines.append(f"{len(jobs)} job(s) found")
        lines.append("")

        remaining_bytes = max_log_bytes

        for j in jobs:
            job_id = j.get("id")
            name = j.get("name", str(job_id))
            j_conclusion = j.get("conclusion", "?")

            lines.append(f"### Job: {name} (conclusion={j_conclusion})")

            if job_id is None:
                lines.append("_(no job ID — cannot fetch logs)_")
                lines.append("")
                continue

            if remaining_bytes <= 0:
                lines.append("_(log budget exhausted — increase max_log_bytes)_")
                lines.append("")
                continue

            try:
                log_text = await actions_client.get_job_log(repo_full_name, job_id)
            except RuntimeError as exc:
                lines.append(f"_Error fetching job log: {exc}_")
                lines.append("")
                continue

            if not log_text.strip():
                lines.append("_(empty log)_")
                lines.append("")
                continue

            original_len = len(log_text)
            log_text = log_text[-remaining_bytes:]
            remaining_bytes -= len(log_text)

            if len(log_text) < original_len:
                lines.append(
                    f"_(truncated to last {len(log_text)} bytes "
                    f"of {original_len} total)_"
                )
            lines.append("")
            lines.append("```")
            lines.append(log_text)
            lines.append("```")
            lines.append("")

        return "\n".join(lines)

    async def fetch_trivy_findings(
        repo_full_name: str,
        run_id: int = 0,
        branch: str = "",
        instance_url: str = "",
    ) -> str:
        """Fetch and parse Trivy vulnerability scan results from a CI run.

        Retrieves the Trivy scan job log from a CI workflow run and parses
        the table-formatted output into a structured vulnerability summary
        with CVE identifiers, affected packages, severity, and fixed
        versions.  Use this when ``fetch_ci_job_logs`` (or the board) shows
        a Trivy scan failure, so you can provide actionable remediation
        steps (upgrade package X to version Y) or justify a scanner-ignore
        rule.

        **Read-only.** Does not modify any repository state.
        **No BLOCKED-state requirement.** This is a pure diagnostic tool.

        When *run_id* is omitted, the most recent failed run on *branch*
        (default: the repository's default branch) is used.  When provided,
        *branch* is unused.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g. ``"org/repo"``).
            run_id: Specific workflow run id (optional; defaults to latest failed).
            branch: Branch to search for failing runs when *run_id* is 0.
            instance_url: Optional GitHub instance URL (for GitHub Enterprise).
                Defaults to ``https://github.com``.

        Returns:
            A formatted Markdown summary of all Trivy findings including a
            vulnerability table grouped by severity, plus remediation hints
            (fixed versions).  When no Trivy job is found or no
            vulnerabilities can be parsed, returns a diagnostic message
            with the raw log excerpt for manual review.

        """
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        from .actions_client import ActionsClient as _ActionsClient

        actions_client = _ActionsClient(settings)

        # Resolve run_id when not provided.
        if not run_id:
            target_branch = branch or await actions_client.get_default_branch(
                repo_full_name
            )
            try:
                runs = await actions_client.list_workflow_runs(
                    repo_full_name,
                    branch=target_branch,
                    per_page=20,
                    raise_on_error=True,
                )
            except Exception as exc:
                return (
                    f"Error: could not list workflow runs for {repo_full_name}: {exc}"
                )
            if not runs:
                return (
                    f"No recent workflow runs found on '{target_branch}' in "
                    f"{repo_full_name}."
                )

            failed_run = next(
                (
                    r
                    for r in runs
                    if (r.get("conclusion") or "").lower() in {"failure", "timed_out"}
                ),
                None,
            )
            if failed_run is None:
                latest = runs[0]
                resolved_id = latest.get("id")
                if not isinstance(resolved_id, int):
                    return (
                        f"No failed workflow run found on '{target_branch}' "
                        f"in {repo_full_name}."
                    )
                run_id = resolved_id
            else:
                resolved_id = failed_run.get("id")
                if not isinstance(resolved_id, int):
                    return (
                        f"Error: could not determine run id from the "
                        f"failing workflow run in {repo_full_name}."
                    )
                run_id = resolved_id

        # Fetch the workflow run metadata for context.
        run_data = await actions_client.get_workflow_run(repo_full_name, run_id)
        if run_data is None:
            return f"Error: workflow run {run_id} not found in {repo_full_name}."

        run_name = run_data.get("name", "(unknown)")
        run_branch = run_data.get("head_branch", "?")
        gh_instance = instance_url.rstrip("/") if instance_url else "https://github.com"
        run_url = f"{gh_instance}/{repo_full_name}/actions/runs/{run_id}"

        # Fetch jobs — look for Trivy-titled jobs first.
        jobs = await actions_client.get_workflow_run_jobs(repo_full_name, run_id)
        if not jobs:
            return (
                f"No jobs found in workflow run {run_id} ({repo_full_name}). "
                f"Check manually: {run_url}"
            )

        trivy_keywords = {"trivy", "vulnerability", "vuln", "scan-container"}
        trivy_jobs = [
            j
            for j in jobs
            if any(kw in (j.get("name") or "").lower() for kw in trivy_keywords)
        ]

        # Fall back to failed jobs if no trivy-named job
        candidate_jobs = trivy_jobs or [
            j
            for j in jobs
            if (j.get("conclusion") or "").lower()
            in ("failure", "timed_out", "cancelled")
        ]

        if not candidate_jobs:
            available = ", ".join(j.get("name", "?") for j in jobs)
            return (
                f"No Trivy-related or failed jobs in run {run_id} "
                f"({repo_full_name}).\n"
                f"Available jobs: {available}\n"
                f"Check manually: {run_url}"
            )

        # Fetch logs for each candidate job and attempt to parse Trivy output.
        parsed_results: list[str] = []
        total_findings = 0

        from .trivy_parser import format_findings_summary
        from .trivy_parser import parse_trivy_table as _parse_trivy

        for job in candidate_jobs:
            job_id = job.get("id")
            job_name = job.get("name", str(job_id))
            if job_id is None:
                continue

            try:
                log_text = await actions_client.get_job_log(repo_full_name, job_id)
            except RuntimeError as exc:
                parsed_results.append(
                    f"### Job: {job_name}\n\n_Log unavailable: {exc}_"
                )
                continue

            if not log_text.strip():
                parsed_results.append(f"### Job: {job_name}\n\n_(empty log)_")
                continue

            # Parse the log for Trivy table output.
            result = _parse_trivy(log_text)
            if result.findings:
                total_findings += len(result.findings)
                parsed_results.append(
                    f"### Job: {job_name}\n\n"
                    f"Run **{run_name}** on `{run_branch}` "
                    f"([view run]({run_url}))\n\n"
                    f"{format_findings_summary(result)}"
                )
            elif result.summary and result.summary.total > 0:
                # Summary line found but no individual findings could be parsed.
                s = result.summary
                parsed_results.append(
                    f"### Job: {job_name}\n\n"
                    f"Run **{run_name}** on `{run_branch}` "
                    f"([view run]({run_url}))\n\n"
                    f"**{s.total} vulnerabilities** "
                    f"(CRITICAL: {s.critical}, HIGH: {s.high}, "
                    f"MEDIUM: {s.medium}, LOW: {s.low}), "
                    f"but individual entries could not be parsed from "
                    f"the raw output. Review the full log manually.\n\n"
                    f"<details><summary>Raw log excerpt (last 2000 chars)</summary>\n\n"
                    f"```\n{log_text[-2000:]}\n```\n\n</details>"
                )
            else:
                # No Trivy output detected — include a snippet for manual review.
                snippet = log_text[-2000:] if len(log_text) > 2000 else log_text
                parsed_results.append(
                    f"### Job: {job_name}\n\n"
                    f"No Trivy vulnerability table found in this job's log.\n\n"
                    f"<details><summary>Raw log (last 2000 chars)</summary>\n\n"
                    f"```\n{snippet}\n```\n\n</details>"
                )

        header = (
            f"## Trivy Scan Results — {repo_full_name}\n"
            f"Workflow run: {run_id} | Branch: {run_branch}\n"
            f"Job(s) scanned: {len(candidate_jobs)}\n\n"
        )

        if total_findings == 0:
            no_findings_msg = (
                "No structured findings could be parsed. "
                "The scan may have used a different output format "
                "(e.g. SARIF instead of table). "
                "Check the full log:\n"
                f"- {run_url}\n\n"
            )
            header += no_findings_msg
        else:
            header += f"**{total_findings} vulnerability finding(s) extracted.**\n\n"

        return header + "\n\n".join(parsed_results)

    async def file_ci_stabilization_ticket(
        repo_full_name: str,
        branch: str = "",
        summary: str = "",
    ) -> str:
        """File a dedicated CI-stabilization ticket on the board.

        Escalates a CI failure to a human operator by creating a board ticket
        that flags the repository/branch for CI stabilization.  Use this when
        a deployment is blocked because a dependent PR cannot merge due to
        pre-existing CI failures and a simple re-run is not appropriate or
        has not resolved the problem.

        **No BLOCKED-state requirement.** Filing a ticket is an escalation
        action and does not require a ticket to already be in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name`` with the failing CI.
            branch: Branch with the failing CI (default: repository default).
            summary: Short description of the failure to include in the ticket
                body (optional).

        Returns:
            The created ticket id and title, or an error message.

        """
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        actions_client = ActionsClient(settings)
        target_branch = branch or await actions_client.get_default_branch(
            repo_full_name
        )

        title = f"CI stabilization needed: {repo_full_name} ({target_branch})"
        body_lines = [
            f"CI on `{repo_full_name}` branch `{target_branch}` is failing and "
            f"may be blocking dependent PRs from merging.",
        ]
        if summary:
            body_lines.append("")
            body_lines.append(f"Summary: {summary}")
        body_lines.append("")
        body_lines.append(
            "Filed by the robotsix-chat agent as a CI-stabilization "
            "escalation. Verify the failure, re-run CI if it looks transient, "
            "and remediate the root cause."
        )

        try:
            ticket_id = await board.create_ticket(
                title=title,
                description="\n".join(body_lines),
                kind="task",
                source="agent",
            )
        except Exception as exc:
            return f"Error filing CI stabilization ticket for {repo_full_name}: {exc}"

        if not ticket_id:
            return (
                f"Error filing CI stabilization ticket for {repo_full_name}: "
                f"board API did not return a ticket id."
            )
        return f"Filed CI stabilization ticket {ticket_id}: {title}"

    async def verify_pr_ci_status(
        repo_full_name: str | None = None,
        pr_number: int | None = None,
        repo: str | None = None,
        pr_url: str | None = None,
        ticket_id: str | None = None,
    ) -> str:
        """Fetch live CI run status and PR state from GitHub.

        Combines PR metadata (state, mergeability, draft status) with the
        latest CI workflow runs for the PR's head branch into a single
        human-readable summary.  Use this tool BEFORE asserting success or
        signalling the operator about CI/PR status — never rely on cached
        or inferred data.

        **Read-only.** Does not modify any repository state.
        **No BLOCKED-state requirement.** This is a pure diagnostic tool —
        it does not require a ticket to be in BLOCKED state.

        When GitHub is unreachable the tool returns an explicit error
        message rather than guessing.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``). ``repo`` is an accepted
                alias; a bare repo name is resolved via the board roster.
            pr_number: The PR number to inspect.
            repo: Alias of ``repo_full_name``.
            pr_url: Alternative to repo + number — the PR's GitHub url.
            ticket_id: Alternative — a mill ticket id; its open PR is used.

        Returns:
            A multi-line summary: PR state, mergeability, draft status,
            and the latest CI workflow runs for the PR's head branch.

        """
        ref = await resolve_pr_ref(
            board,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            repo=repo,
            pr_url=pr_url,
            ticket_id=ticket_id,
        )
        if isinstance(ref, str):
            return ref
        repo_full_name, pr_number = ref
        # Scope check (no BLOCKED-state requirement — this is read-only)
        if component_request is None and (
            scope_error := await client.check_installation_scope(repo_full_name)
        ):
            return scope_error

        try:
            pr = await client.get_pr(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
            )
        except Exception as exc:
            return f"Error fetching PR #{pr_number} in {repo_full_name}: {exc}"

        title = pr.get("title", "(no title)")
        state = pr.get("state", "unknown")
        html_url = pr.get("html_url", "")
        mergeable = pr.get("mergeable")
        mergeable_state = pr.get("mergeable_state", "unknown")
        draft = pr.get("draft", False)
        merged = pr.get("merged", False)
        head = pr.get("head", {})
        head_branch = head.get("ref", "")

        lines = [
            f"PR #{pr_number} in {repo_full_name}: {title}",
            f"URL: {html_url}",
            f"State: {state}",
            f"Draft: {draft}",
            f"Merged: {merged}",
            f"Mergeable state: {mergeable_state}",
        ]

        if mergeable is None:
            lines.append("Mergeability: still being computed by GitHub.")
        elif mergeable is True:
            lines.append("Mergeability: clean — no conflicts.")
        elif mergeable is False:
            lines.append("Mergeability: conflicts detected.")

        # --- CI workflow runs for the PR's head branch ---
        if head_branch:
            try:
                actions_client = ActionsClient(settings)
                runs = await actions_client.list_workflow_runs(
                    repo_full_name, branch=head_branch, per_page=5
                )
            except Exception as exc:
                lines.append(f"CI status: could not fetch workflow runs — {exc}")
                return "\n".join(lines)

            if not runs:
                lines.append(
                    f"CI status: no recent workflow runs found "
                    f"for branch '{head_branch}'."
                )
            else:
                lines.append(
                    f"CI status for branch '{head_branch}' ({len(runs)} recent run(s)):"
                )
                for r in runs[:5]:
                    lines.append(
                        f"  - {r.get('name', '?')} "
                        f"(run {r.get('id')}): "
                        f"status={r.get('status')}, "
                        f"conclusion={r.get('conclusion')}"
                    )
        else:
            lines.append("CI status: could not determine head branch from PR data.")

        return "\n".join(lines)

    return [
        check_ci_health,
        rerun_ci_workflow,
        fetch_ci_job_logs,
        fetch_trivy_findings,
        file_ci_stabilization_ticket,
        verify_pr_ci_status,
    ]
