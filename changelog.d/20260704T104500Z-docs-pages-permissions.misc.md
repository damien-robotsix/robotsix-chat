Docs workflow: grant the `pages: write` + `id-token: write` permissions the
shared `python-docs.yml` now requires (it deploys via the GitHub Pages
actions), fixing the `startup_failure` on main; the repo's Pages site is
enabled with the `workflow` build type.
