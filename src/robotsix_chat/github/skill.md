# GitHub Repository Settings

## PATCH /chat/github/repos/{owner}/{repo}/settings

Toggle repository security-and-analysis features on repos under the
configured GitHub App installation.

**This is a confirmation-gated mutation.** Before calling, confirm the
exact repo name (`owner/repo`) and the specific change with the user
in-chat.  Never toggle settings without explicit user approval in the
conversation — the endpoint modifies live repository configuration.

### Request

- **Method:** `PATCH`
- **Auth:** `X-API-Key` header (server-side `github_security.deploy_api_key`)
- **Content-Type:** `application/json`

#### Path parameters

| Parameter | Description |
|-----------|-------------|
| `owner` | GitHub organisation or user name |
| `repo`  | Repository name (not `owner/repo`) |

#### Body (JSON)

All fields are optional — omitted fields are left unchanged.  Each
accepts `"enabled"` or `"disabled"`.

| Field | Description |
|-------|-------------|
| `dependency_graph` | Enable/disable the dependency graph |
| `advanced_security` | Enable/disable GitHub Advanced Security |
| `secret_scanning` | Enable/disable secret scanning |
| `secret_scanning_push_protection` | Enable/disable push protection for secret scanning |

At least one field must be specified.

#### Example

```json
{
  "dependency_graph": "enabled"
}
```

### Response

| Status | Meaning |
|--------|---------|
| 200 | Settings applied successfully — body includes the repo and result message |
| 400 | Invalid body (missing required field, invalid value) or missing path params |
| 403 | Invalid or missing `X-API-Key` header |
| 404 | Repository not in the GitHub App installation scope |
| 503 | `github_security` not configured (disabled or missing `deploy_api_key`) |
