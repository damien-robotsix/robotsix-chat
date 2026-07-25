Fixed an intermittent CI failure where the `check` aggregator job in
`.github/workflows/ci.yml` was cancelled even though every dependency job
succeeded.  The root cause is a GitHub Actions edge case: when a reusable
workflow call (e.g. `python-ci.yml`) contains conditionally-skipped nested
jobs, the caller's `needs` resolution can treat the reusable call as
skipped/cancelled instead of successful.  The `check` job now uses
`if: !cancelled()` with explicit `needs.<id>.result` checks, accepting
`skipped` only for the reusable-workflow `ci` / `security` jobs (where
conditionally-skipped nested jobs are the norm).
