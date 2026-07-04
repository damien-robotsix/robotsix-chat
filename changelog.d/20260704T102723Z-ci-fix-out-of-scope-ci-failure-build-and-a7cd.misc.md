ci_fix: out-of-scope CI failure — Build and deploy docs / Deploy in Either configure the
`github-pages` environment in repo Settings to allow PR branch deployments, or add a conditional in
`.github/workflows/docs.yml` (or the shared `python-docs.yml`) to skip the deploy job on
pull_request events.
