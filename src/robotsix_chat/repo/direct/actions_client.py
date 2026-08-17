"""GitHub Actions workflow management client.

Provides ``ActionsClient`` — a client for GitHub Actions API operations
(dispatch workflows, list runs, fetch job logs, manage secrets).

Shares the same GitHub App authentication plumbing as
:class:`DirectRepoClient` via composition.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings
    from robotsix_chat.repo.direct.client import DirectRepoClient

logger = logging.getLogger(__name__)

# Conclusions that prove a run reached real job execution — a terminal
# verdict on actual jobs — as opposed to ``startup_failure`` (rejected
# before any job), ``cancelled`` / ``stale``, or a pending status.
_JOB_EXECUTION_CONCLUSIONS: frozenset[str] = frozenset(
    {"success", "failure", "timed_out", "action_required"}
)


class StartupFailureClass(str, Enum):
    """Deterministic root-cause plane for a zero-job ``startup_failure`` run."""

    PER_WORKFLOW_CONFIG = "per_workflow_config"
    ACCOUNT_OR_RUNNER = "account_or_runner"


@dataclass(frozen=True)
class StartupFailureClassification:
    """Result of :func:`_classify_startup_failure`.

    ``classification`` picks the root-cause plane; ``summary`` is a
    one-line human-readable explanation.
    """

    classification: StartupFailureClass
    summary: str


def _classify_startup_failure(
    failing_run: dict[str, Any],
    latest_by_wf: Mapping[Any, dict[str, Any]],
) -> StartupFailureClassification:
    """Classify a ``startup_failure`` (zero-job) run deterministically — pure, no I/O.

    An account-level cause (billing lapse, org-wide Actions disablement,
    no available runners) zeroes out **every** workflow on a commit at once,
    whereas a per-workflow config cause leaves sibling workflows on the same
    commit running.  This helper turns that into a fact by checking
    *latest_by_wf* for sibling runs on the failing run's ``head_sha`` whose
    ``conclusion`` proves real job execution (``success`` / ``failure`` /
    ``timed_out`` / ``action_required`` — NOT ``startup_failure``,
    ``cancelled`` / ``stale``, or a pending status).

    Args:
        failing_run: The failing (zero-job) run dict.  Must carry
            ``head_sha`` (string) and ``id`` (to exclude itself).
        latest_by_wf: Mapping of ``workflow_id`` to the latest run dict
            per workflow (sibling candidates on any branch).

    Returns:
        ``PER_WORKFLOW_CONFIG`` when at least one sibling on the same
        commit reached job execution; ``ACCOUNT_OR_RUNNER`` when every
        workflow on the commit produced zero jobs, or there are no
        siblings at all.
    """
    head_sha = failing_run.get("head_sha")
    failing_id = failing_run.get("id")

    executed: list[dict[str, Any]] = []
    for sibling in latest_by_wf.values():
        if sibling.get("head_sha") != head_sha:
            continue
        if failing_id is not None and sibling.get("id") == failing_id:
            continue
        conclusion = str(sibling.get("conclusion") or "").lower()
        if conclusion in _JOB_EXECUTION_CONCLUSIONS:
            executed.append(sibling)

    if executed:
        names = ", ".join(
            str(sibling.get("name") or "").strip() or "?" for sibling in executed
        )
        return StartupFailureClassification(
            classification=StartupFailureClass.PER_WORKFLOW_CONFIG,
            summary=(
                f"{len(executed)} sibling workflow(s) ran jobs on "
                f"{head_sha} ({names}) → per-workflow config issue, "
                f"not account/billing"
            ),
        )
    return StartupFailureClassification(
        classification=StartupFailureClass.ACCOUNT_OR_RUNNER,
        summary=(
            f"no sibling workflow on {head_sha} reached job execution → "
            f"account/runner/billing plane implicated (operator action, "
            f"not a workflow-file edit)"
        ),
    )


class ActionsClient:
    """GitHub Actions workflow management client.

    Handles workflow dispatch, run listing/inspection, job log retrieval,
    and Actions secret management.  Composes a ``DirectRepoClient`` for
    shared GitHub App HTTP plumbing.
    """

    def __init__(self, settings: DirectRepoSettings) -> None:
        """Store settings; composes a ``DirectRepoClient`` for HTTP plumbing.

        Uses a local import to avoid circular dependency at module level.
        """
        from robotsix_chat.repo.direct.client import DirectRepoClient

        self._client: DirectRepoClient = DirectRepoClient(settings)

    # -- workflow dispatch -------------------------------------------------

    async def dispatch_workflow(
        self,
        repo_full_name: str,
        workflow_id: str,
        ref: str,
        inputs: dict[str, str] | None = None,
    ) -> str:
        """Trigger a workflow_dispatch event.

        Calls ``POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches``.

        Never raises — returns a success/error message string.
        """
        body: dict[str, Any] = {"ref": ref}
        if inputs:
            body["inputs"] = inputs

        try:
            await self._client._request_json(
                "POST",
                f"/repos/{repo_full_name}/actions/workflows/{workflow_id}/dispatches",
                body,
            )
            return (
                f"Workflow '{workflow_id}' dispatched successfully "
                f"on {repo_full_name} (ref: {ref})."
            )
        except RuntimeError as exc:
            return f"Error dispatching workflow: {exc}"
        except Exception as exc:
            return f"Error dispatching workflow: {exc}"

    # -- workflow run listing ----------------------------------------------

    async def list_workflow_runs(
        self,
        repo_full_name: str,
        *,
        branch: str | None = None,
        head_sha: str | None = None,
        per_page: int = 10,
        raise_on_error: bool = False,
    ) -> list[dict[str, Any]]:
        """List recent workflow runs for a repository.

        Calls ``GET /repos/{owner}/{repo}/actions/runs``.

        Args:
            repo_full_name: ``"owner/name"``.
            branch: Optional branch filter.
            head_sha: Optional commit SHA filter — only returns runs
                triggered by this commit.
            per_page: Results per page (default 10).
            raise_on_error: When ``True``, re-raise the underlying
                ``RuntimeError`` instead of returning an empty list.  This
                lets callers that need to distinguish "no runs exist" from
                "the GitHub Actions API is unreachable" surface the error.

        Returns:
            A list of workflow run dicts (empty list on error unless
            *raise_on_error* is set).

        """
        params = f"?per_page={min(max(per_page, 1), 100)}"
        if branch:
            params += f"&branch={branch}"
        if head_sha:
            params += f"&head_sha={head_sha}"
        try:
            data = await self._client._get_json(
                f"/repos/{repo_full_name}/actions/runs{params}"
            )
            runs: list[dict[str, Any]] = data.get("workflow_runs", [])
            return runs
        except RuntimeError as exc:
            if raise_on_error:
                raise
            logger.warning(
                "Failed to list workflow runs for %s: %s",
                repo_full_name,
                exc,
            )
            return []

    # -- default branch ----------------------------------------------------

    async def get_default_branch(self, repo_full_name: str) -> str:
        """Return the repository's default branch name.

        Calls ``GET /repos/{owner}/{repo}`` and reads ``default_branch``.
        Falls back to ``"main"`` when the metadata cannot be fetched.
        """
        try:
            repo = await self._client._get_json(f"/repos/{repo_full_name}")
        except Exception:
            logger.debug(
                "get_default_branch: could not fetch repo metadata for %s",
                repo_full_name,
            )
            return "main"
        return repo.get("default_branch") or "main"

    # -- workflow run re-run -----------------------------------------------

    async def rerun_workflow_run(
        self,
        repo_full_name: str,
        run_id: int,
    ) -> str:
        """Re-run all jobs in a completed workflow run.

        Calls ``POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun``.
        Never raises — returns a success/error message string.
        """
        url = (
            f"{self._client._base_url}/repos/{repo_full_name}"
            f"/actions/runs/{run_id}/rerun"
        )
        try:
            result = await self._client._http_with_retry(
                "POST",
                url,
                headers=await self._client._gh_headers(),
                timeout=self._client._s.timeout,
            )
        except Exception as exc:
            return f"Error rerunning workflow run {run_id}: {exc}"
        if result.error:
            return f"Error rerunning workflow run {run_id}: {result.error}"
        if result.status_code and result.status_code >= 400:
            body = (result.text or "").strip()[:200]
            suffix = f": {body}" if body else ""
            return (
                f"Error rerunning workflow run {run_id}: "
                f"HTTP {result.status_code}{suffix}"
            )
        return (
            f"Workflow run {run_id} on {repo_full_name} re-run triggered successfully."
        )

    # -- workflow run jobs -------------------------------------------------

    async def get_workflow_run_jobs(
        self,
        repo_full_name: str,
        run_id: int,
    ) -> list[dict[str, Any]]:
        """Return jobs for a specific workflow run.

        Calls ``GET /repos/{owner}/{repo}/actions/runs/{run_id}/jobs``.

        Returns an empty list on any error.
        """
        try:
            data = await self._client._get_json(
                f"/repos/{repo_full_name}/actions/runs/{run_id}/jobs"
            )
            jobs: list[dict[str, Any]] = data.get("jobs", [])
            return jobs
        except RuntimeError as exc:
            logger.warning(
                "Failed to get workflow run jobs for %s run %s: %s",
                repo_full_name,
                run_id,
                exc,
            )
            return []

    async def get_workflow_run(
        self,
        repo_full_name: str,
        run_id: int,
    ) -> dict[str, Any] | None:
        """Fetch a single workflow run's metadata.

        Calls ``GET /repos/{owner}/{repo}/actions/runs/{run_id}``.

        Returns:
            The run dict on success, ``None`` when the API call fails
            (logged at WARNING).
        """
        try:
            return await self._client._get_json(
                f"/repos/{repo_full_name}/actions/runs/{run_id}"
            )
        except RuntimeError as exc:
            logger.warning(
                "Failed to get workflow run %d on %s: %s",
                run_id,
                repo_full_name,
                exc,
            )
            return None

    # -- workflow run annotations ------------------------------------------

    @staticmethod
    def _is_checks_permission_error(exc: RuntimeError) -> bool:
        """Return ``True`` when *exc* is a 403 on the GitHub Checks API.

        The Checks API returns 403 when the installation token lacks the
        ``checks: read`` permission.  This is distinguishable from other
        403 errors (e.g. rate-limiting) by the path prefix.
        """
        msg = str(exc)
        return "error 403" in msg and ("/check-suites/" in msg or "/check-runs/" in msg)

    async def get_workflow_run_annotations(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        max_check_runs: int = 20,
    ) -> str:
        """Fetch annotations for all check runs in a workflow run.

        Orchestrates three GitHub API calls:
        1. ``GET /repos/{owner}/{repo}/actions/runs/{run_id}`` — get the
           ``check_suite_id`` for the run.
        2. ``GET /repos/{owner}/{repo}/check-suites/{check_suite_id}/check-runs``
           — list check runs belonging to the suite.
        3. For each check run with annotations,
           ``GET /repos/{owner}/{repo}/check-runs/{check_run_id}/annotations``.

        When the Checks API returns 403 (installation token lacks
        ``checks: read`` permission), the method falls back to fetching raw
        job logs via the Actions API instead.

        Returns a formatted Markdown string listing all annotations grouped
        by check run, or a diagnostic message when no annotations are found.
        """
        try:
            # 1. Get the workflow run to find the check_suite_id.
            run = await self._client._get_json(
                f"/repos/{repo_full_name}/actions/runs/{run_id}"
            )
            check_suite_id = run.get("check_suite_id")
            if check_suite_id is None:
                return (
                    f"Workflow run {run_id} on {repo_full_name} has no "
                    f"associated check suite — annotations are not available."
                )

            # 2. List check runs for the check suite.
            try:
                suite_data = await self._client._get_json(
                    f"/repos/{repo_full_name}/check-suites/{check_suite_id}"
                    f"/check-runs?per_page={min(max_check_runs, 100)}"
                    f"&filter=latest"
                )
            except RuntimeError as exc:
                if self._is_checks_permission_error(exc):
                    logger.info(
                        "Checks API returned 403 for run %d on %s — "
                        "falling back to raw job logs.",
                        run_id,
                        repo_full_name,
                    )
                    return await self._get_failed_job_logs(repo_full_name, run_id)
                raise

            check_runs: list[dict[str, Any]] = suite_data.get("check_runs", [])

            if not check_runs:
                return (
                    f"Workflow run {run_id} on {repo_full_name} has no "
                    f"check runs in its check suite."
                )

            # 3. Fetch annotations for each check run that has any.
            all_annotations: list[dict[str, Any]] = []
            check_run_summaries: list[str] = []

            _failed_conclusions = frozenset(
                {"failure", "timed_out", "cancelled", "action_required"}
            )

            for cr in check_runs:
                cr_id = cr.get("id")
                cr_name = cr.get("name", str(cr_id))
                cr_conclusion = cr.get("conclusion", "?")
                ann_count = cr.get("annotations_count", 0)

                if ann_count == 0 and cr_conclusion not in _failed_conclusions:
                    continue

                try:
                    annotations = await self._client._get_json(
                        f"/repos/{repo_full_name}/check-runs/{cr_id}"
                        f"/annotations?per_page=100"
                    )
                    if isinstance(annotations, list):
                        all_annotations.extend(annotations)
                        check_run_summaries.append(
                            f"{cr_name} (conclusion={cr_conclusion}, "
                            f"{len(annotations)} annotation(s))"
                        )
                except RuntimeError as exc:
                    if self._is_checks_permission_error(exc):
                        logger.info(
                            "Checks API returned 403 for check run %d — "
                            "falling back to raw job logs.",
                            cr_id,
                        )
                        return await self._get_failed_job_logs(repo_full_name, run_id)
                    logger.warning(
                        "Failed to fetch annotations for check run %d: %s",
                        cr_id,
                        exc,
                    )
                    continue

            if not all_annotations:
                return (
                    f"Workflow run {run_id} on {repo_full_name} has "
                    f"{len(check_runs)} check run(s) but none with annotations."
                )

            # 4. Format the output.
            lines: list[str] = [
                f"## Workflow run {run_id} annotations ({repo_full_name})",
                "",
                f"**{len(all_annotations)} annotation(s)** across "
                f"{len(check_run_summaries)} check run(s):",
                "",
            ]

            for summary in check_run_summaries:
                lines.append(f"- {summary}")

            lines.append("")
            lines.append("### Details")
            lines.append("")

            for i, ann in enumerate(all_annotations):
                level = ann.get("annotation_level", "?")
                path = ann.get("path", "")
                start_line = ann.get("start_line")
                end_line = ann.get("end_line")
                message = ann.get("message", "")
                title = ann.get("title", "")

                loc = path
                if start_line is not None:
                    loc += f":{start_line}"
                    if end_line is not None and end_line != start_line:
                        loc += f"-{end_line}"

                lines.append(
                    f"**{i + 1}.** `{level}` "
                    + (f"**{title}** — " if title else "")
                    + f"{message}"
                )
                if loc:
                    lines.append(f"  _Location: {loc}_")
                lines.append("")

        except RuntimeError as exc:
            annotation_error = str(exc)
        except Exception as exc:
            annotation_error = str(exc)
        else:
            return "\n".join(lines)

        # -- fallback: fetch raw job logs ----------------------------------
        return await self._fallback_job_logs(repo_full_name, run_id, annotation_error)

    async def _fallback_job_logs(
        self,
        repo_full_name: str,
        run_id: int,
        annotation_error: str,
    ) -> str:
        """Fallback for ``get_workflow_run_annotations``: fetch raw job logs.

        Called when the check-run / annotations API returns an error
        (typically 403 — insufficient permissions).  Attempts to list
        the workflow run's jobs and fetch raw logs for any that failed.

        Returns a Markdown string with whatever logs could be retrieved,
        plus explicit limitation messaging when everything is inaccessible.
        """
        run_url = f"https://github.com/{repo_full_name}/actions/runs/{run_id}"
        permission_note = (
            "The GitHub App installation may lack 'checks: read' permission "
            "for this repository — check-run annotations require that scope."
        )

        try:
            jobs_data = await self._client._get_json(
                f"/repos/{repo_full_name}/actions/runs/{run_id}/jobs"
            )
            jobs: list[dict[str, Any]] = jobs_data.get("jobs", [])
        except RuntimeError as exc:
            return (
                f"Unable to diagnose workflow run {run_id} on {repo_full_name}.\n\n"
                f"**Check-run annotations:** not accessible ({annotation_error})\n"
                f"**Job listing:** also failed ({exc})\n\n"
                f"{permission_note}\n\n"
                f"**Suggestion:** check the logs manually at {run_url}"
            )
        except Exception as exc:
            return (
                f"Unable to diagnose workflow run {run_id} on {repo_full_name}.\n\n"
                f"**Check-run annotations:** not accessible ({annotation_error})\n"
                f"**Job listing:** also failed ({exc})\n\n"
                f"**Suggestion:** check the logs manually at {run_url}"
            )

        if not jobs:
            is_private = await self.check_repo_visibility(repo_full_name)
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

        # Collect logs for failed / cancelled / timed-out jobs.
        log_results: list[str] = []
        failed_job_ids: list[str] = []
        success_count = 0
        failure_count = 0

        for job in jobs:
            job_id = job.get("id")
            if job_id is None:
                continue
            job_name = job.get("name", str(job_id))
            conclusion = job.get("conclusion", "")

            if conclusion in ("failure", "cancelled", "timed_out"):
                try:
                    log = await self.get_job_log(repo_full_name, job_id)
                    if len(log) > 8000:
                        log = log[:8000] + "\n\n... [log truncated at 8000 chars]"
                    log_results.append(
                        f"### Job: {job_name} (id {job_id}, conclusion={conclusion})\n"
                        f"\n```\n{log}\n```"
                    )
                    success_count += 1
                except RuntimeError as exc:
                    log_results.append(
                        f"### Job: {job_name} (id {job_id}, conclusion={conclusion})\n"
                        f"\n_Log unavailable: {exc}_"
                    )
                    failed_job_ids.append(str(job_id))
                    failure_count += 1
                except Exception as exc:
                    log_results.append(
                        f"### Job: {job_name} (id {job_id}, conclusion={conclusion})\n"
                        f"\n_Log unavailable: {exc}_"
                    )
                    failed_job_ids.append(str(job_id))
                    failure_count += 1

        if not log_results:
            return (
                f"Workflow run {run_id} on {repo_full_name} has "
                f"{len(jobs)} job(s) but none with a failed conclusion. "
                f"Check-run annotations were not accessible "
                f"({annotation_error})."
            )

        header = (
            f"## Workflow run {run_id} ({repo_full_name})\n\n"
            f"_Check-run annotations were not accessible — "
            f"falling back to raw job logs._\n\n"
            f"({success_count} log(s) retrieved"
            + (f", {failure_count} unavailable" if failure_count else "")
            + ")\n"
        )

        if failure_count > 0:
            header += (
                f"\n**Limitation:** could not retrieve logs for jobs "
                f"{', '.join(failed_job_ids)}. "
                f"{permission_note}\n"
            )

        header += f"\n**Manual check:** {run_url}\n"

        return header + "\n\n".join(log_results)

    # -- job log retrieval -------------------------------------------------

    async def _get_failed_job_logs(
        self,
        repo_full_name: str,
        run_id: int,
        *,
        max_log_bytes: int = 32_000,
    ) -> str:
        """Fallback: fetch raw logs for failed jobs in a workflow run.

        Used when the Checks API is inaccessible (e.g. the GitHub App token
        lacks ``checks: read`` permission).  The Actions API endpoints
        (``/actions/runs/{run_id}/jobs`` and ``/actions/jobs/{job_id}/logs``)
        require ``actions: read`` scope, which is often granted separately.

        Returns a formatted Markdown string with truncated log content for
        each failed job, or a diagnostic message when no failed jobs are
        found.
        """
        jobs = await self.get_workflow_run_jobs(repo_full_name, run_id)
        if not jobs:
            return (
                f"Workflow run {run_id} on {repo_full_name} has no jobs — "
                f"cannot fetch logs."
            )

        failed_jobs = [
            j
            for j in jobs
            if j.get("conclusion") in ("failure", "timed_out", "cancelled")
        ]
        if not failed_jobs:
            return (
                f"Workflow run {run_id} on {repo_full_name} has "
                f"{len(jobs)} job(s) but none with a failed conclusion — "
                f"no failed-job logs to fetch."
            )

        lines: list[str] = [
            f"## Workflow run {run_id} job logs ({repo_full_name})",
            "",
            f"_Annotations unavailable (Checks API returned 403 — the GitHub "
            f"App token likely lacks the `checks: read` permission). "
            f"Falling back to raw job logs for {len(failed_jobs)} failed "
            f"job(s)._",
            "",
        ]

        per_job_limit = max(max_log_bytes // max(len(failed_jobs), 1), 2000)

        for j in failed_jobs:
            job_id = j.get("id")
            job_name = j.get("name", str(job_id))
            job_conclusion = j.get("conclusion", "?")

            lines.append(f"### {job_name} (conclusion={job_conclusion})")
            lines.append("")

            if job_id is None:
                lines.append("_No job id available — cannot fetch log._")
                lines.append("")
                continue

            try:
                log_text = await self.get_job_log(repo_full_name, job_id)
            except RuntimeError as exc:
                lines.append(f"_Error fetching log: {exc}_")
                lines.append("")
                continue

            if not log_text.strip():
                lines.append("_(empty log)_")
                lines.append("")
                continue

            # Truncate long logs to keep the agent context manageable.
            if len(log_text) > per_job_limit:
                truncated = log_text[-per_job_limit:]
                lines.append(
                    f"_Log truncated to last {per_job_limit} bytes "
                    f"(full log is {len(log_text)} bytes)._"
                )
                lines.append("")
                lines.append("```")
                lines.append(truncated)
                lines.append("```")
            else:
                lines.append("```")
                lines.append(log_text)
                lines.append("```")
            lines.append("")

        return "\n".join(lines)

    async def get_job_log(self, repo_full_name: str, job_id: int) -> str:
        """Fetch the plain-text log for a GitHub Actions job.

        Calls ``GET /repos/{owner}/{repo}/actions/jobs/{job_id}/logs`` which
        returns a 302 redirect to a signed URL containing the raw log text.
        The redirect is followed server-side so the caller receives the log
        content directly.

        Raises ``RuntimeError`` on any failure (auth, not-found, network).
        """
        path = f"/repos/{repo_full_name}/actions/jobs/{job_id}/logs"
        url = f"{self._client._base_url}{path}"
        result = await safe_http_request(
            "GET",
            url,
            headers=await self._client._gh_headers(),
            timeout=self._client._s.timeout,
            follow_redirects=True,
            label="GitHub Actions log",
        )
        if result.error:
            raise RuntimeError(f"GitHub Actions log GET {path}: {result.error}")
        return result.text or ""

    # -- Actions secrets ---------------------------------------------------

    async def _get_repo_public_key(self, repo_full_name: str) -> tuple[str, str]:
        """Return ``(key_id, public_key_b64)`` for Actions secret encryption.

        Calls ``GET /repos/{owner}/{repo}/actions/secrets/public-key``.
        """
        data = await self._client._get_json(
            f"/repos/{repo_full_name}/actions/secrets/public-key"
        )
        return str(data["key_id"]), str(data["key"])

    async def set_actions_secret(
        self,
        repo_full_name: str,
        secret_name: str,
        secret_value: str,
    ) -> str:
        """Create or update a repository Actions secret.

        Encrypts *secret_value* with the repo's public key using libsodium
        sealed-box encryption (requires ``pynacl``), then sends it via
        ``PUT /repos/{owner}/{repo}/actions/secrets/{secret_name}``.

        Never raises — returns a success/error message string.
        """
        try:
            from nacl.public import (  # type: ignore[import-not-found]
                PublicKey,
                SealedBox,
            )
        except ImportError:
            return (
                "Error: PyNaCl is required for Actions secret encryption. "
                "Install it with: uv sync --extra github-actions  or  "
                "pip install pynacl"
            )

        try:
            key_id, public_key_b64 = await self._get_repo_public_key(repo_full_name)
        except RuntimeError as exc:
            return f"Error fetching repo public key: {exc}"
        except Exception as exc:
            return f"Error fetching repo public key: {exc}"

        from robotsix_chat.repo.direct.client import _b64decode, _b64encode

        try:
            public_key_bytes = _b64decode(public_key_b64)
            sealed_box = SealedBox(PublicKey(public_key_bytes))
            encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
            encrypted_b64 = _b64encode(encrypted)
        except Exception as exc:
            return f"Error encrypting secret: {exc}"

        try:
            await self._client._request_json(
                "PUT",
                f"/repos/{repo_full_name}/actions/secrets/{secret_name}",
                {
                    "encrypted_value": encrypted_b64,
                    "key_id": key_id,
                },
            )
            return f"Secret '{secret_name}' set successfully on {repo_full_name}."
        except RuntimeError as exc:
            return f"Error setting secret: {exc}"
        except Exception as exc:
            return f"Error setting secret: {exc}"

    # -- repo visibility ---------------------------------------------------

    async def check_repo_visibility(self, repo_full_name: str) -> bool | None:
        """Return ``True`` when *repo_full_name* is private, ``False`` when public.

        Calls ``GET /repos/{owner}/{repo}`` and reads the ``private`` field.
        Returns ``None`` on any error (logged at DEBUG level).
        """
        try:
            repo = await self._client._get_json(f"/repos/{repo_full_name}")
        except Exception:
            logger.debug(
                "check_repo_visibility: could not fetch repo metadata for %s",
                repo_full_name,
            )
            return None
        return bool(repo.get("private", False))

    # -- zero-job workflow detection ---------------------------------------

    async def classify_startup_failure_run(
        self,
        repo_full_name: str,
        failing_run: dict[str, Any],
    ) -> StartupFailureClassification | None:
        """Fetch sibling runs on the failing run's commit and classify it.

        Calls :func:`_classify_startup_failure` after building the
        per-workflow latest-run mapping from real sibling data.

        Returns ``None`` when the run carries no ``head_sha`` or the
        sibling listing fails (logged at DEBUG) — callers then fall
        back to visibility-based wording.
        """
        head_sha = failing_run.get("head_sha")
        if not isinstance(head_sha, str) or not head_sha:
            return None
        try:
            sibling_runs = await self.list_workflow_runs(
                repo_full_name,
                head_sha=head_sha,
                per_page=30,
                raise_on_error=True,
            )
        except Exception:
            logger.debug(
                "classify_startup_failure_run: could not list sibling runs "
                "for head_sha %s on %s",
                head_sha,
                repo_full_name,
            )
            return None

        latest_by_wf: dict[Any, dict[str, Any]] = {}
        for run in sibling_runs:
            workflow_id = run.get("workflow_id")
            if workflow_id is not None:
                latest_by_wf.setdefault(workflow_id, run)
        return _classify_startup_failure(failing_run, latest_by_wf)

    async def check_latest_run_for_zero_jobs(
        self,
        repo_full_name: str,
        branch: str,
    ) -> str | None:
        """Check if the latest workflow run on *branch* has zero jobs.

        Returns a diagnostic string when zero jobs are detected — a CI
        infrastructure failure where the workflow file parses correctly
        but produces no job definitions (typically a misconfigured trigger,
        invalid conditional, or billing issue).  Returns ``None`` when the
        check passes, no runs exist, or an error occurs (errors are logged
        at DEBUG level).
        """
        try:
            runs = await self.list_workflow_runs(
                repo_full_name, branch=branch, per_page=1
            )
        except Exception:
            logger.debug(
                "check_latest_run_for_zero_jobs: could not list runs for %s/%s",
                repo_full_name,
                branch,
            )
            return None

        if not runs:
            return None

        latest = runs[0]
        run_id = latest.get("id")
        if not isinstance(run_id, int):
            return None

        try:
            jobs = await self.get_workflow_run_jobs(repo_full_name, run_id)
        except Exception:
            logger.debug(
                "check_latest_run_for_zero_jobs: could not fetch jobs for run %d on %s",
                run_id,
                repo_full_name,
            )
            return None

        if jobs:
            return None

        run_name = latest.get("name", str(run_id))

        # -- deterministic classification via sibling check --
        classification = await self.classify_startup_failure_run(
            repo_full_name, latest
        )
        if classification is not None:
            conclusion = str(latest.get("conclusion") or "").lower()
            conclusion_note = (
                f" (conclusion: {conclusion})" if conclusion else ""
            )
            if classification.classification is StartupFailureClass.PER_WORKFLOW_CONFIG:
                return (
                    f"CI STARTUP FAILURE (per-workflow config): workflow run "
                    f"'{run_name}' (id {run_id}) on {repo_full_name} branch "
                    f"'{branch}' has ZERO jobs{conclusion_note} — the CI "
                    f"workflow is not executing any jobs.  "
                    f"{classification.summary} — the account/runner/billing "
                    f"plane is provably fine, so the root cause is in this "
                    f"workflow's own file (trigger, permissions, or "
                    f"reusable-workflow ``uses:``).  "
                    f"PRs on this branch are not receiving CI coverage."
                )
            return (
                f"CI STARTUP FAILURE (account/runner): workflow run "
                f"'{run_name}' (id {run_id}) on {repo_full_name} branch "
                f"'{branch}' has ZERO jobs{conclusion_note} — the CI "
                f"workflow is not executing any jobs.  "
                f"{classification.summary} — every workflow on this commit "
                f"produced zero jobs, which points at the account/runner/"
                f"billing plane (Actions disabled, billing lapse, or no "
                f"available runners).  This is an operator-action ticket, "
                f"NOT a workflow-file edit.  "
                f"PRs on this branch are not receiving CI coverage."
            )

        # -- fallback: no head_sha or sibling listing unavailable --
        is_private = await self.check_repo_visibility(repo_full_name)
        if is_private is True:
            return (
                f"CI INFRASTRUCTURE FAILURE: workflow run '{run_name}' "
                f"(id {run_id}) on {repo_full_name} branch '{branch}' "
                f"has ZERO jobs — the CI workflow is not executing any jobs. "
                f"This typically indicates a workflow configuration error "
                f"(wrong trigger, invalid conditional), a billing issue, "
                f"or a missing reusable workflow file. "
                f"PRs on this branch are not receiving CI coverage."
            )
        elif is_private is False:
            return (
                f"CI INFRASTRUCTURE FAILURE: workflow run '{run_name}' "
                f"(id {run_id}) on {repo_full_name} branch '{branch}' "
                f"has ZERO jobs — the CI workflow is not executing any jobs. "
                f"Since {repo_full_name} is a public repository, billing "
                f"is not the issue.  The likely causes are a workflow "
                f"configuration error (wrong trigger, invalid conditional), "
                f"a missing reusable workflow file, or an input-contract "
                f"mismatch (e.g. a required ``workflow_call`` input not "
                f"provided by the caller). "
                f"PRs on this branch are not receiving CI coverage."
            )
        else:
            return (
                f"CI INFRASTRUCTURE FAILURE: workflow run '{run_name}' "
                f"(id {run_id}) on {repo_full_name} branch '{branch}' "
                f"has ZERO jobs — the CI workflow is not executing any jobs. "
                f"This may indicate a workflow configuration error "
                f"(wrong trigger, invalid conditional), a billing issue, "
                f"a missing reusable workflow file, or an input-contract "
                f"mismatch.  PRs on this branch are not receiving CI "
                f"coverage."
            )

    # -- billing failure diagnosis -----------------------------------------

    async def _other_workflows_succeeded_on_commit(
        self,
        repo_full_name: str,
        head_sha: str,
        exclude_run_id: int | None = None,
    ) -> bool:
        """Check whether any workflow run on *head_sha* completed successfully.

        Excludes *exclude_run_id* (the run we are currently diagnosing).
        Returns ``True`` when at least one other workflow run on the same
        commit has ``conclusion="success"`` — this rules out a billing
        issue because billing would block **all** workflows, not just one.

        Returns ``False`` when the check cannot confirm a successful
        sibling run (no other runs, all failed, or an API error).
        """
        try:
            sibling_runs = await self.list_workflow_runs(
                repo_full_name, head_sha=head_sha, per_page=30
            )
        except Exception:
            logger.debug(
                "Could not list sibling runs for head_sha %s on %s",
                head_sha,
                repo_full_name,
            )
            return False

        for sibling in sibling_runs:
            if exclude_run_id is not None and sibling.get("id") == exclude_run_id:
                continue
            if str(sibling.get("conclusion", "")).lower() == "success":
                return True

        return False

    async def _diagnose_billing_failure(
        self,
        runs: list[dict[str, Any]],
        repo_full_name: str,
    ) -> str | None:
        """Inspect recent workflow runs for a workflow infrastructure failure.

        Heuristic: a run whose ``run_started_at`` is ``null`` (never started)
        strongly suggests the repo has a workflow configuration or
        infrastructure problem (billing, trigger config, missing reusable
        workflow, etc.).  The diagnosis is tailored to whether the repo is
        public or private.

        **Cross-check:** before returning a diagnosis, this method checks
        whether *other* workflow runs on the same commit completed
        successfully.  If they did, the root cause is likely a trigger
        configuration mismatch (e.g. a workflow that only triggers on
        ``push`` to ``main``, not on ``pull_request``) rather than a
        billing issue — billing would block all workflows, not just one.

        **Public-repo awareness:** for public repositories, billing is
        never the cause (GitHub Actions is free for public repos).  The
        diagnosis focuses on trigger misconfigurations, missing reusable
        workflow files, and input-contract mismatches instead.

        Note: zero-job detection is NOT attempted here because the
        ``/actions/runs`` endpoint does not include per-job run data.
        That signature is handled by the per-run inspection path in
        ``check_workflow_run`` (via ``get_workflow_run_jobs``).

        Returns a human-readable diagnostic string, or ``None`` when the
        signature is not detected.
        """
        for run in runs:
            run_id = run.get("id")
            run_name = run.get("name", str(run_id))
            conclusion = str(run.get("conclusion", "")).lower()

            # startup_failure runs are always zero-job — classify by siblings.
            if conclusion == "startup_failure":
                classification_result = await self.classify_startup_failure_run(
                    repo_full_name, run
                )
                if classification_result is not None:
                    if classification_result.classification is StartupFailureClass.PER_WORKFLOW_CONFIG:
                        return (
                            f"Workflow run '{run_name}' (id {run_id}) failed "
                            f"at startup (zero jobs) on "
                            f"{run.get('head_branch', '?')} — "
                            f"{classification_result.summary}.  "
                            f"The account/runner/billing plane is provably fine "
                            f"— the root cause is in this workflow's own file "
                            f"(trigger, permissions, or reusable-workflow "
                            f"``uses:``)."
                        )
                    return (
                        f"Workflow run '{run_name}' (id {run_id}) failed "
                        f"at startup (zero jobs) on "
                        f"{run.get('head_branch', '?')} — "
                        f"{classification_result.summary}.  "
                        f"Every workflow on this commit produced zero jobs, "
                        f"which points at the account/runner/billing plane "
                        f"(Actions disabled, billing lapse, or no available "
                        f"runners).  This is an operator-action ticket, "
                        f"NOT a workflow-file edit."
                    )
                # head_sha unavailable or listing failed — skip this run
                # without emitting any speculative billing diagnosis.
                continue

            if conclusion != "failure":
                continue

            # Runs that never started signal a configuration or billing issue.
            if "run_started_at" in run and not run.get("run_started_at"):
                head_sha = run.get("head_sha")
                if isinstance(head_sha, str) and head_sha:
                    other_succeeded = await self._other_workflows_succeeded_on_commit(
                        repo_full_name, head_sha, exclude_run_id=run_id
                    )
                else:
                    other_succeeded = False

                is_private = await self.check_repo_visibility(repo_full_name)

                if other_succeeded:
                    return (
                        f"Workflow run '{run_name}' (id {run_id}) for "
                        f"{run.get('head_branch', '?')} never started, "
                        f"but other workflows on the same commit ran "
                        f"successfully — this rules out a billing issue. "
                        f"The likely root cause is a trigger configuration "
                        f"mismatch: the workflow may only trigger on "
                        f"``push`` to ``main`` (or another branch), not on "
                        f"the ``pull_request`` event that created this run. "
                        f"Check the workflow's ``on:`` trigger in the "
                        f"``.github/workflows/`` directory."
                    )

                if is_private is True:
                    return (
                        f"Workflow run '{run_name}' (id {run_id}) for "
                        f"{run.get('head_branch', '?')} never started — "
                        f"this is typical of a private repository with no "
                        f"GitHub Actions billing. "
                        f"Enable Actions in the repo's "
                        f"Settings > Actions > General, "
                        f"or add billing at the organisation level."
                    )
                elif is_private is False:
                    return (
                        f"Workflow run '{run_name}' (id {run_id}) for "
                        f"{run.get('head_branch', '?')} never started. "
                        f"{repo_full_name} is a public repository, so "
                        f"billing is not the issue.  The likely causes "
                        f"are a missing reusable workflow file "
                        f"(``.github/workflows/``), an input-contract "
                        f"mismatch (e.g. a required ``workflow_call`` "
                        f"input not provided), or an invalid conditional "
                        f"in the workflow's ``on:`` trigger."
                    )
                else:
                    return (
                        f"Workflow run '{run_name}' (id {run_id}) for "
                        f"{run.get('head_branch', '?')} never started — "
                        f"this may indicate that GitHub Actions billing "
                        f"is not enabled for this private repository, or "
                        f"a trigger configuration mismatch, or a missing "
                        f"reusable workflow file.  Check the workflow's "
                        f"``on:`` trigger in ``.github/workflows/`` and "
                        f"verify billing at Settings > Actions > General."
                    )
        return None
