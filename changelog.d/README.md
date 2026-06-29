# changelog.d

Each non-trivial PR must add a file to this directory. The file name follows the pattern
`<id>.<type>.md` where:

- `<id>` is the issue number, PR number, or ticket identifier
- `<type>` is one of: `feature`, `bugfix`, `doc`, `removal`, `misc`

Examples:

- `42.feature.md` — a new feature
- `17.bugfix.md` — a bug fix
- `3.doc.md` — documentation update
- `99.removal.md` — deprecation or removal
- `7.misc.md` — minor change

CI enforces via `towncrier check` that a fragment was added (or modified) on each PR. To skip this
requirement for trivial PRs, apply the `skip-changelog` label.

At release time, `towncrier build --version X.Y.Z --yes` collects these fragments and writes them
into `CHANGELOG.md`, then deletes the consumed files.
