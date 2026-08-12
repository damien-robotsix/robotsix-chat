# Direct Repository Access

## Agent tool: `merge_direct_repo_pr`

Merge a pull request in a repository under the GitHub App installation scope. The merge is performed
using the GitHub App installation token — the credential never leaves the server.

**This is a confirmation-gated mutation.** Merge only when the operator has explicitly consented
in the conversation. The operator's own direct request (e.g. "merge PR #123") or a clear
affirmative answer to your proposal IS that consent — once given, state the exact repo, PR number,
PR title, and head/base branches and merge without re-asking. Ask only when consent has not yet
been clearly given. The endpoint modifies live repository state.

### Preconditions (enforced server-side)

- PR must be mergeable (no conflicts).
- Required status checks / CI must be green.
- PR must not be in draft state.
- Repository must be within the GitHub App installation scope.

### Merge methods

| Method   | Behaviour                                                         |
| -------- | ----------------------------------------------------------------- |
| `squash` | Squash all commits into one (default).                            |
| `merge`  | Create a merge commit preserving the branch history.              |
| `rebase` | Rebase the branch commits onto the base branch (no merge commit). |

### Error responses

| Condition                         | Message                                                         |
| --------------------------------- | --------------------------------------------------------------- |
| PR is a draft                     | `the PR is still in draft state`                                |
| Merge conflicts                   | `merge conflicts detected`                                      |
| Mergeability still being computed | `mergeability is still being computed by GitHub`                |
| CI checks failing/pending         | `the PR is not in a mergeable state` (HTTP 405)                 |
| Repo not in installation scope    | `The robotsix-mill GitHub App is not installed on 'owner/name'` |

______________________________________________________________________

## Agent tool: `check_direct_repo_auto_merge`

Check whether a repository has auto-merge enabled at the repository level. Reads the
`allow_auto_merge` setting from the GitHub repository metadata.

**Read-only.** This tool does not modify any state and does not require confirmation gating. Use it
proactively before filing or managing tickets that require automatic merging — if auto-merge is
disabled the operator should be informed that manual merging will be required and the workflow
should be adjusted (e.g., skip `waiting_auto_merge` or set expectations early).

### Preconditions

- Repository must be within the GitHub App installation scope.

### Response

| Result            | Message                                                                   |
| ----------------- | ------------------------------------------------------------------------- |
| Auto-merge on     | `Auto-merge is **enabled** on owner/name. PRs ... will be merged ...`     |
| Auto-merge off    | `Auto-merge is **disabled** on owner/name. ... all merges must be manual` |
| Repo not in scope | `The robotsix-mill GitHub App is not installed on 'owner/name'`           |

______________________________________________________________________

## Agent tool: `arm_direct_repo_auto_merge`

Enable GitHub native auto-merge on a pull request. Once armed, GitHub automatically merges the PR as
soon as all required conditions are met (status checks pass, required reviews are submitted, branch
protection rules are satisfied) — no further human intervention is needed.

**This is a confirmation-gated mutation.** Enable only when the operator has explicitly consented
in the conversation. The operator's own direct request (e.g. "enable auto-merge on PR #123") or a
clear affirmative answer to your proposal IS that consent — once given, state the exact repo, PR
number, PR title, and head/base branches and enable it without re-asking. Ask only when consent has
not yet been clearly given.

### Preconditions

- Repository must be within the GitHub App installation scope.
- PR must not be in draft state.
- PR must not already be merged.
- Repository must have auto-merge enabled (some repos disable it).

### Merge methods (applied when auto-merge fires)

| Method   | Behaviour                                                         |
| -------- | ----------------------------------------------------------------- |
| `squash` | Squash all commits into one (default).                            |
| `merge`  | Create a merge commit preserving the branch history.              |
| `rebase` | Rebase the branch commits onto the base branch (no merge commit). |

### Error responses

| Condition                        | Message                                                                                                                               |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| PR is a draft                    | `the PR is still in draft state`                                                                                                      |
| Auto-merge not available on repo | `the repository may not have auto-merge enabled` (HTTP 403/404) — use `check_direct_repo_auto_merge` first to detect this proactively |
| Repo not in installation scope   | `The robotsix-mill GitHub App is not installed on 'owner/name'`                                                                       |

______________________________________________________________________

## Other direct-repo tools (read-only or gated on BLOCKED state)

The following tools are available for push/PR operations. They require the ticket to be in BLOCKED
state and the repo to be in the installation scope:

- **`push_direct_repo_branch`** — Push a new branch with file changes.
- **`open_direct_repo_pr`** — Open a PR from an existing branch (no auto-merge — human review
  required).
- **`update_pr_branch`** — Rebase a PR branch onto the latest base branch.
- **`check_pr_merge_conflict`** — Check a PR's mergeability status (read-only, no confirmation
  gating).
- **`apply_patch_to_file`** — Push a file patched with a unified diff to a new branch.

These tools are read-only or gated on BLOCKED state — they do not modify live repository state
beyond what the agent has already been authorised to do via the BLOCKED ticket flow.
