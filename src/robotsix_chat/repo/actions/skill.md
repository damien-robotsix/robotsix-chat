# GitHub Actions

## PUT /chat/github/repos/{owner}/{repo}/actions/secrets/{secret_name}

Create or update a repository Actions secret. The secret value is encrypted with the repo's public
key before transmission — the server never retains the plaintext.

**This is a confirmation-gated mutation.** Before calling, confirm the exact repo name and secret
name with the user in-chat. Never create or update secrets without explicit user approval in the
conversation — the endpoint modifies live repository configuration.

### Request

- **Method:** `PUT`
- **Auth:** `X-API-Key` header (server-side `central_deploy.deploy_api_key`)
- **Content-Type:** `application/json`

#### Path parameters

| Parameter     | Description                                |
| ------------- | ------------------------------------------ |
| `owner`       | GitHub organisation or user name           |
| `repo`        | Repository name (not `owner/repo`)         |
| `secret_name` | Actions secret name (e.g. `OVH_SFTP_HOST`) |

#### Body (JSON)

| Field          | Type   | Description                    |
| -------------- | ------ | ------------------------------ |
| `secret_value` | string | The plaintext value to encrypt |

### Response

| Status | Meaning                                                                               |
| ------ | ------------------------------------------------------------------------------------- |
| 200    | Secret set successfully — body includes the repo and secret name                      |
| 400    | Invalid body (missing `secret_value`) or missing path params                          |
| 403    | Invalid or missing `X-API-Key` header                                                 |
| 404    | Repository not in the GitHub App installation scope                                   |
| 503    | `github_actions` not configured (disabled or missing `central_deploy.deploy_api_key`) |

## POST /chat/github/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches

Trigger a `workflow_dispatch` event on a repository workflow.

**This is a confirmation-gated mutation.** Before calling, confirm the exact repo, workflow, ref,
and inputs with the user in-chat.

### Request

- **Method:** `POST`
- **Auth:** `X-API-Key` header (server-side `central_deploy.deploy_api_key`)
- **Content-Type:** `application/json`

#### Path parameters

| Parameter     | Description                                                   |
| ------------- | ------------------------------------------------------------- |
| `owner`       | GitHub organisation or user name                              |
| `repo`        | Repository name (not `owner/repo`)                            |
| `workflow_id` | Workflow file name (e.g. `deploy.yml`) or numeric workflow ID |

#### Body (JSON)

| Field    | Type   | Description                                             |
| -------- | ------ | ------------------------------------------------------- |
| `ref`    | string | Branch or tag to run the workflow on (default `"main"`) |
| `inputs` | object | Optional key/value pairs of workflow inputs             |

#### Example

```json
{
  "ref": "main",
  "inputs": {
    "environment": "production"
  }
}
```

### Response

| Status | Meaning                                                                               |
| ------ | ------------------------------------------------------------------------------------- |
| 200    | Workflow dispatched successfully                                                      |
| 400    | Invalid body (missing `ref`) or missing path params                                   |
| 403    | Invalid or missing `X-API-Key` header                                                 |
| 404    | Repository not in the GitHub App installation scope                                   |
| 503    | `github_actions` not configured (disabled or missing `central_deploy.deploy_api_key`) |

## Agent tool: `check_workflow_run`

Fetch recent workflow runs and diagnose common CI failure patterns. This is a read-only agent tool
(no HTTP endpoint) that inspects workflow runs and detects known failure signatures.

In particular, it detects **workflow infrastructure failures** — runs that complete with
`conclusion: "failure"` but have zero jobs, or runs that never started (`run_started_at` is null).

**Deterministic `startup_failure` classification (never guess billing vs. config).** A run whose
conclusion is `startup_failure` produced **zero jobs** — GitHub rejected the workflow file before
any job started, so there are no job logs. The tool classifies such runs by checking sibling
workflows on the **same commit** (`head_sha`):

- If **any sibling workflow on the same commit reached job execution** (terminal conclusion
  `success` / `failure` / `timed_out` / `action_required`), the account/runner plane is provably
  fine → the failure is a **per-workflow config issue** (trigger, `permissions:`, or a malformed
  reusable-workflow `uses:`). Fix the workflow file itself.
- If **no sibling reached job execution** (every workflow on the commit produced zero jobs, or there
  are no siblings at all), it is an **account/runner issue** — file an operator-action ticket. Do
  **not** edit workflow files for this classification.

Report the classification the tool returns verbatim. Do not speculate a billing diagnosis when the
classification says per-workflow config, and do not propose workflow-file edits when it says
account/runner.

The diagnosis is tailored to the repository's visibility:

- **Public repos:** billing is never the cause (GitHub Actions is free for public repos). The
  diagnosis focuses on trigger configuration mismatches, missing reusable workflow files, and
  input-contract mismatches (e.g. a required `workflow_call` input not provided).
- **Private repos:** billing may be the cause, but the tool cross-checks whether other workflow runs
  on the same commit completed successfully — if they did, the root cause is likely a trigger
  configuration mismatch rather than a billing issue (billing would block all workflows).

**Read-only.** Does not modify any repository state. No confirmation gating — safe to call anytime
to investigate a CI failure.

## Agent tool: `fetch_workflow_run_annotations`

Fetch annotations for all check runs in a GitHub Actions workflow run. Annotations are the inline
diagnostic messages that GitHub Actions surfaces on files — linter warnings, compiler errors, test
failure details — grouped by check run. Each annotation includes the file path, line range,
annotation level (`failure`, `warning`, `notice`), title, and the full message text.

Use this when a CI run fails and you need the exact annotation text to diagnose the root cause
(rather than rendering the entire GitHub Actions UI page, which can produce very large blobs).

Takes a repository name and a workflow run id (the numeric id from the Actions tab URL). Returns a
Markdown-formatted string with all annotations grouped by check run, or a diagnostic message when no
annotations are found.

### Permission fallback

When the GitHub App installation token lacks the `checks: read` permission, the Checks API returns
403 and annotations are unavailable. In that case the tool falls back to fetching raw job logs via
the Actions API (`/actions/jobs/{job_id}/logs`) for every failed job in the run. The raw logs are
returned as truncated Markdown code blocks — less structured than annotations, but often contain the
same diagnostic output (linter errors, test failures, stack traces).

If the Actions API also fails or the run has no failed jobs, the tool returns a diagnostic message
explaining the gap. When you receive raw logs instead of annotations, note the permission gap and
suggest the repo admin grant the `checks: read` permission on the GitHub App installation for richer
diagnostics in the future.

**Read-only.** Does not modify any repository state. No confirmation gating — safe to call anytime
to investigate a CI failure.

## Agent tool: `fetch_job_log`

Fetch the raw plain-text log for a specific GitHub Actions job. This is a lower-level tool that
retrieves the job's console output directly from the GitHub API (follows the 302 redirect to the
signed log URL server-side).

Use this as a **fallback** when `fetch_workflow_run_annotations` returns a permission error (403) or
when the check-run annotations endpoint is unavailable — job logs use a different API endpoint
(`/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`) that may still be accessible even when the
checks API is not.

Long logs are automatically truncated at 8000 characters to fit within the agent's context window.

**Read-only.** Does not modify any repository state. No confirmation gating — safe to call anytime
to investigate a CI failure.

### Fallback strategy for CI diagnosis

When a CI run fails and you need to diagnose the root cause:

1. **First**, call `fetch_workflow_run_annotations` to get inline annotations (linter errors, test
   failures, etc.).
1. **If that fails** (especially with a 403 permission error), `fetch_workflow_run_annotations`
   automatically falls back to raw job logs for failed jobs. The returned message will include
   whatever logs could be retrieved.
1. **For a specific job's log**, call `fetch_job_log` directly with the job ID (found via
   `check_workflow_run` or in the Actions tab URL).
1. **If everything fails**, the tool will clearly state that logs are inaccessible and suggest
   checking the GitHub Actions UI manually, including the direct URL to the workflow run.

**Limitation:** When the GitHub App installation lacks both `checks: read` and `actions: read`
permissions, neither annotations nor job logs are accessible. In this case, the user must check the
logs manually at `https://github.com/{owner}/{repo}/actions/runs/{run_id}`.
