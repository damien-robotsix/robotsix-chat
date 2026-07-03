# Triage Boilerplate

Standard boilerplate responses for scope-triage decisions during the `draft → ready` transition.

## Changelog Update

**Boilerplate:**

```
scope-triage EXPAND: The CHANGELOG.md update is legitimate accompanying documentation that records the change made in the ticket. Keeping the changelog in sync with the work performed is not scope creep.
```

### When to apply

Apply this boilerplate during the `draft → ready` transition when the implement agent determines that a changelog entry is required. Common triggers:

- Any user-facing change (feature, bugfix, behavior change)
- Internal refactoring that affects public API or config surface
- Addition/removal of a periodic workflow or tool
- CI/CD workflow changes that affect the developer experience

### When NOT to apply

Do NOT apply when:

- The change is purely internal with zero user/operator impact (e.g., test-only changes, comment fixes)
- The changelog is already up-to-date from a prior ticket in the same batch
- The change is a revert of an unreleased change (roll forward, don't double-log)

### Additional files covered by the same pattern

- `docs/configuration.md` updates accompanying config changes
- `changelog.d/` fragment files (towncrier)

### Observed frequency

Appears on most non-trivial tickets (estimated 60-70% of tickets examined). The exact phrasing above is used verbatim across multiple tickets.
