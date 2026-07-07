# github-repo-admin skill

The github component provides **scoped repository administration** on GitHub: create new
repositories, set repository metadata (description, visibility), and register new repos with the
mill board. A server-side GitHub token with `repo-admin` scope powers these operations — the token
is never exposed in the chat container's environment or the roster payload.

Access this component through the generic `component_request` tool:
`component_request(component_id="github", method=..., path=..., json_body=...)`

## Allowed operations

| Operation               | Method | Path                     | Description                                          |
| ----------------------- | ------ | ------------------------ | ---------------------------------------------------- |
| Create repository       | `POST` | `/repos`                 | Create a new GitHub repository.                      |
| Set repo metadata       | `PATCH`| `/repos/{owner}/{repo}`  | Update description, visibility, topics, homepage.    |
| Register with mill      | `POST` | `/repos/{owner}/{repo}/mill` | Register the repo on the mill board.            |

### POST /repos — create repository

```json
{
  "name": "repo-name",
  "description": "optional description",
  "private": false,
  "topics": ["robotsix"],
  "homepage": ""
}
```

Returns the created repository's full metadata including `html_url` and `clone_url`.

### PATCH /repos/{owner}/{repo} — set repo metadata

```json
{
  "description": "updated description",
  "private": false,
  "topics": ["robotsix", "chat"],
  "homepage": ""
}
```

All fields are optional — only the fields present in the body are updated.

### POST /repos/{owner}/{repo}/mill — register with mill

```json
{
  "board": "robotsix",
  "component_name": "robotsix-chat-mobile"
}
```

Registers the repository on the mill board so it appears in the component roster and can be managed
through the standard deploy lifecycle.

## Safety

🛑 **Confirmation gate:** Every write or create operation on this component (`POST`, `PATCH`)
requires explicit in-conversation user confirmation before calling. Read the proposed operation
back to the user and wait for their approval — never proceed without it.

- The GitHub token is server-side only: it never appears in the chat container environment, the
  roster payload, or any tool response.
- Repository creation is scoped to a single GitHub organization or user account configured
  server-side — the agent cannot create repos under arbitrary owners.
- Mill registration is also server-side authenticated; the mill API token is never exposed.
