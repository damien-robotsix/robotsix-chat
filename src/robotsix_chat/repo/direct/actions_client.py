"""GitHub Actions workflow management client.

Provides ``ActionsClient`` — a client for GitHub Actions API operations
(dispatch workflows, list runs, fetch job logs, manage secrets).

Shares the same GitHub App authentication plumbing as
:class:`DirectRepoClient` via composition.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Any

from robotsix_chat.common.http import safe_http_request

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings
    from robotsix_chat.repo.direct.client import DirectRepoClient

logger = logging.getLogger(__name__)


#: Reusable release workflow, pinned by SHA per fleet convention. Must stay in
#: step with what the other fleet repos call: the older `5fdc956e` revision
#: requires a `release-token` PAT instead of the GitHub App and fails at
#: startup with "Secret release-token is required, but not provided".
_REUSABLE_REF = (
    "0234f4b82365d776fc021c774dc104c5e7042c29"  # pragma: allowlist secret — git SHA
)
_REUSABLE_USES = (
    "damien-robotsix/robotsix-github-workflows/.github/workflows/auto-release.yml"
)

#: Content committed to a new fleet repo as
#: ``.github/workflows/auto-release.yml``. Kept as a literal rather than read
#: from disk so it ships with the package and cannot drift from the pin above.
#:
#: The `uses:` line is substituted rather than interpolated — an f-string or
#: ``.format()`` would mangle the ``${{ … }}`` GitHub expressions below.
AUTO_RELEASE_WORKFLOW = """\
name: Auto Release

# Shared towncrier-driven 0.x release workflow (repo baseline): when
# changelog.d/ has fragments, it derives the bump from fragment types, builds
# CHANGELOG.md, bumps pyproject.toml, and pushes the v* tag.
#
# Required by robotsix-standards changelog-driven-releases.md §4 ("Every repo
# wires the shared auto-release workflow"), which also specifies the weekly
# (and on-demand) cadence. A no-op when changelog.d/ is empty.
on:
  schedule:
    - cron: "0 9 * * 1"  # every Monday at 09:00 UTC
  workflow_dispatch:

permissions: {}

jobs:
  release:
    permissions:
      contents: write       # push release commit + v* tag (or release branch)
      pull-requests: write  # open the fallback release PR with auto-merge
    uses: __REUSABLE__  # main
    # Authenticates as the fleet GitHub App — the installation token is minted
    # inside the reusable workflow, so there is no PAT to rotate. A tag pushed
    # with GITHUB_TOKEN would not trigger downstream release workflows.
    with:
      app-id: ${{ vars.RELEASE_APP_ID }}
    secrets:
      app-private-key: ${{ secrets.RELEASE_APP_PRIVATE_KEY }}
""".replace("__REUSABLE__", f"{_REUSABLE_USES}@{_REUSABLE_REF}")


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

        self._s = settings
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
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """List recent workflow runs for a repository.

        Calls ``GET /repos/{owner}/{repo}/actions/runs``.

        Args:
            repo_full_name: ``"owner/name"``.
            branch: Optional branch filter.
            per_page: Results per page (default 10).

        Returns:
            A list of workflow run dicts (empty list on error).

        """
        params = f"?per_page={min(max(per_page, 1), 100)}"
        if branch:
            params += f"&branch={branch}"
        try:
            data = await self._client._get_json(
                f"/repos/{repo_full_name}/actions/runs{params}"
            )
            runs: list[dict[str, Any]] = data.get("workflow_runs", [])
            return runs
        except RuntimeError as exc:
            logger.warning(
                "Failed to list workflow runs for %s: %s",
                repo_full_name,
                exc,
            )
            return []

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
            return (
                f"Workflow run {run_id} on {repo_full_name} has no jobs — "
                f"this is a strong signal that GitHub Actions billing "
                f"is not enabled for this private repository. "
                f"Check the repo's Settings > Actions > General, "
                f"or verify billing at the organisation level."
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

    # -- Actions variables -------------------------------------------------

    async def set_actions_variable(
        self,
        repo_full_name: str,
        name: str,
        value: str,
    ) -> str:
        """Create or update a repository Actions variable.

        Variables are not secrets, so there is no encryption step — but the
        API is split: ``POST .../actions/variables`` creates and fails if the
        variable exists, while ``PATCH .../actions/variables/{name}`` updates
        and fails if it does not. Try the create first and fall back to the
        update, so the call is idempotent either way.

        Never raises — returns a success/error message string.
        """
        try:
            await self._client._request_json(
                "POST",
                f"/repos/{repo_full_name}/actions/variables",
                {"name": name, "value": value},
            )
            return f"Variable '{name}' created on {repo_full_name}."
        except Exception as exc:
            # Most likely already present; fall through to the update path.
            logger.debug("Variable create failed for %s, will PATCH: %s", name, exc)

        try:
            await self._client._request_json(
                "PATCH",
                f"/repos/{repo_full_name}/actions/variables/{name}",
                {"name": name, "value": value},
            )
            return f"Variable '{name}' updated on {repo_full_name}."
        except RuntimeError as exc:
            return f"Error setting variable: {exc}"
        except Exception as exc:
            return f"Error setting variable: {exc}"

    # -- release automation bootstrap --------------------------------------

    async def bootstrap_release_automation(self, repo_full_name: str) -> list[str]:
        """Provision the shared auto-release workflow on a new fleet repo.

        robotsix-standards ``changelog-driven-releases.md`` §4 requires every
        repo to wire the shared auto-release workflow. Doing it at creation
        time is the only way it actually happens — a sweep of the fleet found
        nine repos that had never wired it and therefore had never cut a
        single release tag, which is precisely the failure mode the standard
        describes for consumers pinning a git SHA.

        Three steps, all idempotent:

        1. ``RELEASE_APP_ID`` variable — the App this client already
           authenticates as, so a new repo needs no new credential.
        2. ``RELEASE_APP_PRIVATE_KEY`` secret — the same key, sealed-box
           encrypted for the target repo.
        3. ``.github/workflows/auto-release.yml`` — committed only when
           absent, so an existing (possibly customised) workflow is never
           overwritten.

        The App must be installed on the repo for the release run to mint a
        token; the installation is org-wide, so a repo created under the org
        is covered.

        Never raises — returns one status line per step for relaying to the
        caller.
        """
        results: list[str] = []

        results.append(
            await self.set_actions_variable(
                repo_full_name, "RELEASE_APP_ID", str(self._s.github_app_id)
            )
        )

        key = self._s.github_app_private_key
        key_value = key.get_secret_value() if hasattr(key, "get_secret_value") else key
        results.append(
            await self.set_actions_secret(
                repo_full_name, "RELEASE_APP_PRIVATE_KEY", str(key_value)
            )
        )

        results.append(await self._commit_auto_release_workflow(repo_full_name))
        return results

    async def _commit_auto_release_workflow(self, repo_full_name: str) -> str:
        """Commit ``auto-release.yml`` unless the repo already has one."""
        path = ".github/workflows/auto-release.yml"
        try:
            await self._client.get_file_content(repo_full_name, path)
            return f"Workflow '{path}' already present on {repo_full_name}, kept."
        except Exception as exc:
            # Absent (404) or unreadable — either way, attempt to create it.
            logger.debug(
                "No existing %s on %s (%s); creating.", path, repo_full_name, exc
            )

        # Padded base64 here, NOT the module's `_b64encode` — that helper
        # strips `=` padding for the secrets API, and the Contents API rejects
        # unpadded content.
        content_b64 = base64.b64encode(AUTO_RELEASE_WORKFLOW.encode("utf-8")).decode(
            "ascii"
        )

        try:
            await self._client._request_json(
                "PUT",
                f"/repos/{repo_full_name}/contents/{path}",
                {
                    "message": "ci: wire the shared auto-release workflow",
                    "content": content_b64,
                },
            )
            return f"Workflow '{path}' created on {repo_full_name}."
        except RuntimeError as exc:
            return f"Error creating {path}: {exc}"
        except Exception as exc:
            return f"Error creating {path}: {exc}"

    # -- zero-job workflow detection ---------------------------------------

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
        return (
            f"CI INFRASTRUCTURE FAILURE: workflow run '{run_name}' "
            f"(id {run_id}) on {repo_full_name} branch '{branch}' "
            f"has ZERO jobs — the CI workflow is not executing any jobs. "
            f"This typically indicates a workflow configuration error "
            f"(wrong trigger, invalid conditional) or a billing issue. "
            f"PRs on this branch are not receiving CI coverage."
        )

    # -- billing failure diagnosis -----------------------------------------

    def _diagnose_billing_failure(
        self,
        runs: list[dict[str, Any]],
    ) -> str | None:
        """Inspect recent workflow runs for a private-repo billing failure.

        Heuristic: a run whose ``run_started_at`` is ``null`` (never started)
        strongly suggests the repo has no GitHub Actions billing enabled.

        Note: zero-job detection is NOT attempted here because the
        ``/actions/runs`` endpoint does not include per-job run data.
        That signature is handled by the per-run inspection path in
        ``check_workflow_run`` (via ``get_workflow_run_jobs``).

        Returns a human-readable diagnostic string, or ``None`` when the
        signature is not detected.
        """
        for run in runs:
            conclusion = str(run.get("conclusion", "")).lower()
            if conclusion != "failure":
                continue
            run_id = run.get("id")
            run_name = run.get("name", str(run_id))
            # Runs that never started signal billing issues.
            if "run_started_at" in run and not run.get("run_started_at"):
                return (
                    f"Workflow run '{run_name}' (id {run_id}) for "
                    f"{run.get('head_branch', '?')} never started — "
                    f"this is typical of a private repository with no "
                    f"GitHub Actions billing. "
                    f"Enable Actions in the repo's "
                    f"Settings > Actions > General, "
                    f"or add billing at the organisation level."
                )
        return None
