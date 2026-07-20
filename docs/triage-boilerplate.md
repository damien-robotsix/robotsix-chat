# Triage Boilerplate

Standard boilerplate responses for scope-triage decisions during the `draft → ready` transition.

## Changelog Update

**Boilerplate:**

```text
scope-triage EXPAND: The CHANGELOG.md update is legitimate accompanying documentation that records the change made in the ticket. Keeping the changelog in sync with the work performed is not scope creep.
```

### When to apply

Apply this boilerplate during the `draft → ready` transition when the implement agent determines
that a changelog entry is required. Common triggers:

- Any user-facing change (feature, bugfix, behavior change)
- Internal refactoring that affects public API or config surface
- Addition/removal of a periodic workflow or tool
- CI/CD workflow changes that affect the developer experience

### When NOT to apply

Do NOT apply when:

- The change is purely internal with zero user/operator impact (e.g., test-only changes, comment
  fixes)
- The changelog is already up-to-date from a prior ticket in the same batch
- The change is a revert of an unreleased change (roll forward, don't double-log)

### Additional files covered by the same pattern

- `docs/configuration.md` updates accompanying config changes
- `changelog.d/` fragment files (towncrier)

### Observed frequency

Appears on most non-trivial tickets (estimated 60-70% of tickets examined). The exact phrasing above
is used verbatim across multiple tickets.

## Out-of-Scope CI Failure

**Boilerplate:**

```text
scope-triage OUT-OF-SCOPE: CI failure in <TOOL> is unrelated to this change —
the failure exists on main and is not caused by the code under review. The
failure is tracked separately (see <TRACKING_TICKET>). Proceed without blocking
on this CI check.
```

### When to apply

Apply during the `draft → ready` transition when a CI check fails but the failure is determined to
be:

- Pre-existing on the main branch (not introduced by this change)
- In unrelated infrastructure (e.g., a flaky linter, a broken external service)
- Caused by a known issue already tracked in a separate ticket
- A transient/network hiccup that re-running resolves

### When NOT to apply

Do NOT apply when:

- The CI failure is in a check that the change affects (e.g., a lint rule violation introduced by
  new code)
- The failure has not been independently reproduced on main
- The failure blocks a required check (merge protection) — in that case, fix or escalate; don't
  out-of-scope it

### Observed frequency

9 resolved tickets (2026-06-27 through 2026-07-04) across tools: pre-commit (3), zizmor (2),
lint-workflow (2), hadolint (1), build-and-push (1). Naming pattern:
`ci-fix: out-of-scope CI failure — <tool>`.

## CI Failure on Main

**Boilerplate:**

```text
scope-triage ACKNOWLEDGE: CI failure in <TOOL> on the main branch is an
infrastructure-level issue. The failure is not caused by any specific change
and does not block ongoing work. Tracked for resolution (see <TRACKING_TICKET>);
fix when resources allow.
```

### When to apply

- A CI check fails on the main branch (not on a PR).
- The failure is an infrastructure / tooling issue (flaky runner, expired credential, broken
  external service, pre-existing configuration gap).
- A tracking ticket already exists for the failure.

### When NOT to apply

- The CI failure was introduced by a specific, recent merge — in that case, revert or fix, don't
  just acknowledge.
- The failure blocks a required check on every PR (e.g., a merge-protection gate that is now red on
  all branches) — escalate.

### Relationship to other boilerplate

| Aspect | Out-of-Scope CI Failure (above) | CI Failure on Main |
|---|---|---|
| Trigger | CI fails on a PR | CI fails on main branch |
| Decision | OUT-OF-SCOPE — proceed without blocking | ACKNOWLEDGE — track, don't block |
| Context | Has a "this change" to reference | No specific change; infrastructure-level |

### Observed frequency

17 resolved tickets across 4 weeks (June 22 – July 20, 2026), spanning 6+ distinct tools. Naming
pattern: `ci-failure: <tool> on main`.
