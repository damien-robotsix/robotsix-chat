# github skill

The GitHub component provides repository management through the GitHub REST API. All operations
authenticate via a scoped personal access token provisioned through the deploy EnvStore — the token
never appears in this container's environment.

## Allowed operations

| Tool                 | GitHub API                    | Description                            |
| -------------------- | ----------------------------- | -------------------------------------- |
| `create_github_repo` | `POST /user/repos`            | Create a new repository.               |
| `update_github_repo` | `PATCH /repos/{owner}/{repo}` | Update repo settings (description, …). |
| `get_github_repo`    | `GET /repos/{owner}/{repo}`   | Read repository details.               |

## Confirmation gate

`create_github_repo` is **confirmation-gated**: the tool requires `confirmed=True`. Call it first
with `confirmed=False` to preview what would be created, then ask the user for approval. Only call
it with `confirmed=True` after the user explicitly agrees. Never pre-confirm on the user's behalf,
even when the request seems unambiguous — the gate exists so the user can review the repo name,
visibility, and description before anything is created.

## Safety

- `create_github_repo` is the only destructive operation — it creates real repositories that persist
  and may incur billing.
- `update_github_repo` is low-risk (changes settings, not content) and does not require
  confirmation.
- `get_github_repo` is read-only and safe to call freely.
- The token has repo-admin scope — protect it. Never echo the token, its prefix, or any
  Authorization header value in conversation.
- Repositories are created under the authenticated user's account (not an org). For org repos the
  user must create them manually or configure a different token.
