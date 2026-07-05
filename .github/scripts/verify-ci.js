module.exports = async ({github, context, core}) => {
  const selfRun = `/runs/${context.runId}/`;
  const isSelf = (r) => (r.details_url || '').includes(selfRun);
  const deadlineMs = 20 * 60 * 1000;
  const start = Date.now();

  let others = [];
  while (true) {
    const runs = await github.paginate(github.rest.checks.listForRef, {
      ...context.repo,
      ref: context.sha,
      per_page: 100,
    });
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
