/**
 * Retry an async operation with exponential backoff on transient errors.
 *
 * Transient errors include network timeouts, DNS failures, and 5xx responses
 * from the GitHub API.  Non-retryable errors (4xx, permission denials) are
 * re-thrown immediately.
 *
 * @param {() => Promise<any>} fn — async operation to retry
 * @param {{maxRetries?: number, baseDelayMs?: number}} opts
 * @returns {Promise<any>} — resolved value of fn
 */
async function retryWithBackoff(fn, {maxRetries = 3, baseDelayMs = 2000} = {}) {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (err) {
      const isTransient =
        err.status >= 500 ||
        err.message?.includes('Connect Timeout') ||
        err.message?.includes('ETIMEDOUT') ||
        err.message?.includes('ENOTFOUND') ||
        err.message?.includes('socket hang up') ||
        err.message?.includes('ECONNRESET') ||
        (err.cause && (
          err.cause.code === 'UND_ERR_CONNECT_TIMEOUT' ||
          err.cause.code === 'ECONNRESET' ||
          err.cause.code === 'ETIMEDOUT'
        ));

      if (!isTransient || attempt === maxRetries) throw err;

      const delay = baseDelayMs * Math.pow(2, attempt);
      await new Promise((res) => setTimeout(res, delay));
    }
  }
}

module.exports = async ({github, context, core}) => {
  const deadlineMs = 20 * 60 * 1000;
  const start = Date.now();

  // Every GitHub Actions workflow run is backed by a single check suite.
  // We fetch our own check-suite id directly from the workflow-run API so we
  // can exclude our entire suite — this guarantees we never wait on ourselves
  // even with re-runs or timing issues.
  //
  // Prior approaches:
  //   1. Matching `/runs/<run_id>/` in `details_url` — broken because
  //      check-run URLs use the check-run id, not the workflow-run id.
  //   2. Scanning `checks.listForRef` for job names — ambiguous when
  //      multiple workflow runs exist for the same commit (re-runs), and
  //      fragile when a check run hasn't been indexed yet on first poll.
  let currentSuiteId;
  try {
    const {data: wfRun} = await retryWithBackoff(() =>
      github.rest.actions.getWorkflowRun({
        ...context.repo,
        run_id: context.runId,
      })
    );
    currentSuiteId = wfRun.check_suite_id;
  } catch {
    // If we can't determine our check suite (permissions, API error),
    // fall through — the loop will wait on itself and eventually time out
    // rather than silently passing. This is the safe default.
  }

  const isSelf = (r) =>
    currentSuiteId != null && r.check_suite?.id === currentSuiteId;

  let others = [];
  while (true) {
    const runs = await retryWithBackoff(() =>
      github.paginate(github.rest.checks.listForRef, {
        ...context.repo,
        ref: context.sha,
        per_page: 100,
      })
    );

    // Deploy jobs (e.g. GitHub Pages) can fail for infrastructure reasons
    // outside the codebase (repo settings, environment config).  Exclude them
    // from the CI gate so they don't block releases.
    const isDeploy = (r) => (r.name || '').endsWith(' / Deploy');
    others = runs.filter((r) => !isSelf(r) && !isDeploy(r));
    const pending = others.filter((r) => r.status !== 'completed');

    if (others.length === 0 || pending.length === 0) break;

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
