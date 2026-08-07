# GitHub Actions

## PUT /chat/github/repos/{owner}/{repo}/actions/secrets/{secret_name}

Create or update a repository Actions secret. The secret value is encrypted with the repo's public
key before transmission — the server never retains the plaintext.

**This is a confirmation-gated mutation.** Before calling, confirm the exact repo name and secret
name with the user in-chat. Never create or update secrets without explicit user approval in the
conversation — the endpoint modifies live repository configuration.

### Request

- **Method:** `PUT`
- **Auth:** `X-API-Key` header (server-side `github_actions.deploy_api_key`)
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

| Status | Meaning                                                                |
| ------ | ---------------------------------------------------------------------- |
| 200    | Secret set successfully — body includes the repo and secret name       |
| 400    | Invalid body (missing `secret_value`) or missing path params           |
| 403    | Invalid or missing `X-API-Key` header                                  |
| 404    | Repository not in the GitHub App installation scope                    |
| 503    | `github_actions` not configured (disabled or missing `deploy_api_key`) |

## POST /chat/github/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches

Trigger a `workflow_dispatch` event on a repository workflow.

**This is a confirmation-gated mutation.** Before calling, confirm the exact repo, workflow, ref,
and inputs with the user in-chat.

### Request

- **Method:** `POST`
- **Auth:** `X-API-Key` header (server-side `github_actions.deploy_api_key`)
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

| Status | Meaning                                                                |
| ------ | ---------------------------------------------------------------------- |
| 200    | Workflow dispatched successfully                                       |
| 400    | Invalid body (missing `ref`) or missing path params                    |
| 403    | Invalid or missing `X-API-Key` header                                  |
| 404    | Repository not in the GitHub App installation scope                    |
| 503    | `github_actions` not configured (disabled or missing `deploy_api_key`) |

## Agent tool: `check_workflow_run`

Fetch recent workflow runs and diagnose common CI failure patterns. This is a read-only agent tool
(no HTTP endpoint) that inspects workflow runs and detects known failure signatures.

In particular, it detects **private-repo billing failures** — runs that complete with
`conclusion: "failure"` but have zero jobs, or runs that never started (`run_started_at` is null).
These signatures strongly indicate that GitHub Actions billing is not enabled for the repository.

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

## Agent tool: `fetch_workflow_job_log`

Fetch the raw console log for a specific job in a GitHub Actions workflow run. Looks up the job by
name within a workflow run and returns its full log — every line of output printed by each step,
including test failure messages, compiler errors, stack traces, and shell output.

Use this when annotations alone are insufficient to diagnose a failure (e.g. when you need to see
the exact pytest output, a build error in context, or the full shell trace of a failing step).

Takes a repository name, a workflow run id (the numeric id from the Actions tab URL), and the exact
job name (e.g. `"test (3.14)"`, `"lint"`, `"build"`). Returns the raw log text, or an error message
when the job is not found or the log cannot be retrieved.

**Read-only.** Does not modify any repository state. No confirmation gating — safe to call anytime
to investigate a CI failure.

**Cost note:** Job logs can be multi-megabyte. The tool truncates logs longer than 200 KB to the
trailing portion (which typically contains the failure output). When the log is truncated a header
notes the original size.
