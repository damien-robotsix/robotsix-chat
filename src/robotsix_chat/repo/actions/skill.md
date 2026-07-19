# GitHub Actions

## PUT /chat/github/repos/{owner}/{repo}/actions/secrets/{secret_name}

Create or update a repository Actions secret.  The secret value is encrypted with the repo's
public key before transmission — the server never retains the plaintext.

**This is a confirmation-gated mutation.** Before calling, confirm the exact repo name and
secret name with the user in-chat. Never create or update secrets without explicit user approval
in the conversation — the endpoint modifies live repository configuration.

### Request

- **Method:** `PUT`
- **Auth:** `X-API-Key` header (server-side `github_actions.deploy_api_key`)
- **Content-Type:** `application/json`

#### Path parameters

| Parameter     | Description                                       |
| ------------- | ------------------------------------------------- |
| `owner`       | GitHub organisation or user name                   |
| `repo`        | Repository name (not `owner/repo`)                 |
| `secret_name` | Actions secret name (e.g. `OVH_SFTP_HOST`)        |

#### Body (JSON)

| Field          | Type   | Description                    |
| -------------- | ------ | ------------------------------ |
| `secret_value` | string | The plaintext value to encrypt |

### Response

| Status | Meaning                                                                     |
| ------ | --------------------------------------------------------------------------- |
| 200    | Secret set successfully — body includes the repo and secret name             |
| 400    | Invalid body (missing `secret_value`) or missing path params                 |
| 403    | Invalid or missing `X-API-Key` header                                        |
| 404    | Repository not in the GitHub App installation scope                         |
| 503    | `github_actions` not configured (disabled or missing `deploy_api_key`)      |

## POST /chat/github/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches

Trigger a `workflow_dispatch` event on a repository workflow.

**This is a confirmation-gated mutation.** Before calling, confirm the exact repo, workflow,
ref, and inputs with the user in-chat.

### Request

- **Method:** `POST`
- **Auth:** `X-API-Key` header (server-side `github_actions.deploy_api_key`)
- **Content-Type:** `application/json`

#### Path parameters

| Parameter     | Description                                                       |
| ------------- | ----------------------------------------------------------------- |
| `owner`       | GitHub organisation or user name                                   |
| `repo`        | Repository name (not `owner/repo`)                                 |
| `workflow_id` | Workflow file name (e.g. `deploy.yml`) or numeric workflow ID     |

#### Body (JSON)

| Field    | Type   | Description                                              |
| -------- | ------ | -------------------------------------------------------- |
| `ref`    | string | Branch or tag to run the workflow on (default `"main"`)  |
| `inputs` | object | Optional key/value pairs of workflow inputs               |

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

| Status | Meaning                                                                     |
| ------ | --------------------------------------------------------------------------- |
| 200    | Workflow dispatched successfully                                             |
| 400    | Invalid body (missing `ref`) or missing path params                         |
| 403    | Invalid or missing `X-API-Key` header                                        |
| 404    | Repository not in the GitHub App installation scope                         |
| 503    | `github_actions` not configured (disabled or missing `deploy_api_key`)      |
