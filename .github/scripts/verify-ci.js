module.exports = async ({github, context, core}) => {
  const deadlineMs = 20 * 60 * 1000;
  const start = Date.now();

  // The check-suite id is per-workflow-run: every job in this run shares the
  // same suite.  We locate it once from any check run belonging to this
  // workflow (jobs listed below), then exclude the whole suite so we never
  // wait on ourselves.
  //
  // Prior approach relied on check-run `details_url` containing the workflow
  // run id (`/runs/<run_id>/`), but GitHub Actions check-run URLs use the
  // check-run id, not the workflow-run id — so the self-filter silently
  // failed and the verify job waited on its own still-running check forever.
  const RELEASE_JOB_NAMES = [
    'Verify CI is green',
    'Build, scan, and publish image',
  ];

  let currentSuiteId = undefined;

  let others = [];
  while (true) {
    const runs = await github.paginate(github.rest.checks.listForRef, {
      ...context.repo,
      ref: context.sha,
      per_page: 100,
    });

    // Discover our own check-suite id on the first poll (or if it was
    // somehow missed earlier).
    if (currentSuiteId === undefined) {
      const ourRun = runs.find((r) => RELEASE_JOB_NAMES.includes(r.name));
      currentSuiteId = ourRun?.check_suite?.id;
    }

    const isSelf = (r) =>
      currentSuiteId != null && r.check_suite?.id === currentSuiteId;

    // Deploy jobs (e.g. GitHub Pages) can fail for infrastructure reasons
    // outside the codebase (repo settings, environment config).  Exclude them
    // from the CI gate so they don't block releases.
    const isDeploy = (r) => (r.name || '').endsWith(' / Deploy');
    others = runs.filter((r) => !isSelf(r) && !isDeploy(r));
    const pending = others.filter((r) => r.status !== 'completed');
    if (others.length > 0 && pending.length === 0) break;
    if (Date.now() - start > deadlineMs) {
      core.setFailed(
        `Timed out waiting for CI checks to complete: ${pending.map((r) => r.name).join(', ') || 'no checks found'}`
      );
      return;
    }
    await new Promise((res) => setTimeout(res, 15000));
  }

  const failed = others.filter(
    (r) => r.conclusion !== 'success' && r.conclusion !== 'skipped'
  );
  if (failed.length > 0) {
    core.setFailed(
      `CI checks have not passed for this commit: ${failed.map((r) => `${r.name}=${r.conclusion}`).join(', ')}`
    );
  }
};
