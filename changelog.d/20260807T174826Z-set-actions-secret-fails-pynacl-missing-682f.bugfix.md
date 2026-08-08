Include the `github-actions` extra (PyNaCl) in the container image so the `set_actions_secret` tool can encrypt repository secrets at runtime instead of returning a "PyNaCl is required" error.
