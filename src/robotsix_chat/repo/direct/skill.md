# Direct Repository Access

## Agent tool: `merge_direct_repo_pr`

Merge a pull request in a repository under the GitHub App installation scope. The merge is performed
using the GitHub App installation token — the credential never leaves the server.

**This is a confirmation-gated mutation.** Before calling, state the exact repo, PR number, PR
title, and head/base branches in-chat and obtain explicit operator approval. Never merge a PR
without the operator's explicit consent in the conversation — the endpoint modifies live repository
state.

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

## Agent tool: `arm_direct_repo_auto_merge`

Enable GitHub native auto-merge on a pull request. Once armed, GitHub automatically merges the PR as
soon as all required conditions are met (status checks pass, required reviews are submitted, branch
protection rules are satisfied) — no further human intervention is needed.

**This is a confirmation-gated mutation.** Before calling, state the exact repo, PR number, PR
title, and head/base branches in-chat and obtain explicit operator approval. Never enable auto-merge
without the operator's explicit consent in the conversation.

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

| Condition                        | Message                                                         |
| -------------------------------- | --------------------------------------------------------------- |
| PR is a draft                    | `the PR is still in draft state`                                |
| Auto-merge not available on repo | `the repository may not have auto-merge enabled` (HTTP 403/404) |
| Repo not in installation scope   | `The robotsix-mill GitHub App is not installed on 'owner/name'` |

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

## Agent tool: `push_patch_to_pr_branch`

Push a patched file to an existing pull request's head branch. Fetches the file from the PR's head
branch, applies a unified diff, and pushes the result as a new commit on the same branch.

**Preconditions:**

- Ticket must be in BLOCKED state.
- PR must exist and its head branch must belong to the same repository.

### Patch format

Standard unified diff (as produced by `diff -u` or `git diff`):

```
--- a/path
+++ b/path
@@ -start,count +start,count @@
 context
-removed
+added
```

## Agent tool: `push_to_pr_branch`

Push multiple file changes (full content, not patches) to an existing pull request's head branch.
Fetches the PR to verify it exists and is open, resolves its head branch, and pushes the given files
as a new commit. This is the CI-fix iteration path — no new branch is created and no BLOCKED-state
or implement-cycle gate is enforced.

**Configuration gate:** This tool is only available when `allow_push_to_existing_pr` is `true` in
the config (default `false`).

### Preconditions (enforced server-side)

- PR must exist and be open (not merged or closed).
- PR must be associated with the ticket — the ticket id must appear in the PR title, body, or head
  branch name.
- PR's head branch must belong to the same repository (cross-repo PR updates are refused).

### Safeguards

| Safeguard          | Limit                                                    |
| ------------------ | -------------------------------------------------------- |
| Content size       | Maximum 200 KB total across all files.                   |
| Ticket association | Ticket id must appear in PR title, body, or head branch. |
| Audit log          | Every invocation logged at WARNING level.                |

### Error responses

| Condition                      | Message                                                            |
| ------------------------------ | ------------------------------------------------------------------ |
| PR is not open                 | `Refused: PR #N ... is '<state>', not 'open'`                      |
| PR not associated with ticket  | `Refused: PR #N ... is not associated with ticket <id>`            |
| Cross-repo PR                  | `Refused: PR #N head branch ... belongs to '<repo>', not '<repo>'` |
| Content too large              | `Refused: total file content size ... exceeds the 200 KB limit`    |
| Repo not in installation scope | `The robotsix-mill GitHub App is not installed on 'owner/name'`    |
