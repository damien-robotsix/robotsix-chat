Fix GHCR image publish: use the built-in `GITHUB_TOKEN` for registry login instead of unset
`GHCR_TOKEN`/`GHCR_USERNAME` secrets, unblocking the Release image workflow.
