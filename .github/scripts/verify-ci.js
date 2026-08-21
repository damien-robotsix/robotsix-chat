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
  const deadlineMs = 30 * 60 * 1000;
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
  } catch (err) {
    // If we can't determine our check suite (permissions, API error),
    // log the error and fall through to the name-based fallback.
    core.warning(
      `Could not determine check suite id: ${err.message}. ` +
        'Falling back to name-based self-exclusion.'
    );
  }

  // Two-layer self-exclusion:
  //   1. Check-suite match (precise, handles re-runs).
  //   2. Job display-name match (fallback).  context.job is the YAML key
  //      (e.g. "verify"), not the display name.  We hard-code the display
  //      names of all jobs in *this* workflow so the exclusion works even
  //      when getWorkflowRun fails or returns a null check_suite_id.
  const selfNames = new Set(['Verify CI is green']);
  const isSelf = (r) =>
    (currentSuiteId != null && r.check_suite?.id === currentSuiteId) ||
    selfNames.has(r.name);

  let others = [];
  while (true) {
    const runs = await retryWithBackoff(() =>
      github.paginate(github.rest.checks.listForRef, {
        ...context.repo,
        ref: context.sha,
        per_page: 100,
      })
    );

    // Deploy jobs (e.g. GitHub Pages, reusable-workflow nested deploy
    // steps) can fail for infrastructure reasons outside the codebase
    // (repo settings, environment config).  Exclude them from the CI gate
    // so they don't block releases.
    //
    // Two patterns: reusable-workflow nested jobs end with " / Deploy";
    // standalone deploy jobs (e.g. "Deploy to GitHub Pages") contain
    // "Deploy" as a whole word.
    const isDeploy = (r) => {
      const name = r.name || '';
      return name.endsWith(' / Deploy') || /\bDeploy\b/.test(name);
    };
    // The "All CI checks passed" aggregate check run is a GitHub-internal
    // summary check run whose conclusion is purely derivative of the
    // individual job check runs already monitored by the loop. Including it
    // causes confusing double-reporting (e.g. "Pre-commit hooks=failure,
    // All CI checks passed=failure") without adding any information.
    const isAggregate = (r) => (r.name || '').startsWith('All CI checks');
    // The "Chat microbenchmarks" job runs only on pushes to main and is
    // intentionally informational: it is not in the `needs:` list of the
    // "All CI checks passed" merge gate, and its only downstream consumer is
    // the benchmark-history artifact upload. Exclude it from the release gate
    // so a benchmark flake (or an LLM/hardware timing regression that does
    // not affect correctness) never blocks publishing an image.
    const isBenchmark = (r) => (r.name || '') === 'Chat microbenchmarks';
    // Release-health monitoring jobs (release-health.yml) verify
    // that release-please infrastructure is healthy (App permissions,
    // latest run status).  They can fail for transient runner-infra
    // reasons (e.g. a "Set up job" timeout during a release-please
    // run) — exclude them so a monitoring false-positive never blocks
    // publishing an image.
    const isReleaseHealth = (r) => {
      const name = r.name || '';
      return name === 'Verify release-please prerequisites' ||
             name === 'Monitor release-please status';
    };
    // Security scan (shared) runs trufflehog + pip-audit via the
    // python-security.yml reusable workflow.  Both checks are redundant
    // with this repo's own jobs: trufflehog is covered by pre-commit
    // (detect-secrets), and pip-audit by the lockfile job (uv audit).
    // When a transient infrastructure failure (e.g. astral-sh/setup-uv
    // manifest fetch timeout) causes the shared security workflow to
    // fail, treating it as blocking prevents releasing an otherwise
    // green commit.  Tolerate failure so the security job never blocks
    // the release gate.
    const nonBlocking = new Set([
      'Security scan (shared) / Security',
    ]);
    others = runs.filter(
      (r) =>
        !isSelf(r) &&
        !isDeploy(r) &&
        !isAggregate(r) &&
        !isBenchmark(r) &&
        !isReleaseHealth(r) &&
        !nonBlocking.has(r.name)
    );
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
    (r) => r.conclusion !== 'success' && r.conclusion !== 'skipped' && r.conclusion !== 'cancelled'
  );
  if (failed.length > 0) {
    core.setFailed(
      `CI checks have not passed for this commit: ${failed.map((r) => `${r.name}=${r.conclusion}`).join(', ')}`
    );
  }
};
