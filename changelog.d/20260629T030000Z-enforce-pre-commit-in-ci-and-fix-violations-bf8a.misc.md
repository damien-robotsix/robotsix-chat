Enforce pre-commit in CI (new `pre-commit` job in `ci.yml`) and clear the pre-existing violations it
surfaces: fix the mypy hook to run `mypy src/ --strict` (avoids the "source file found twice"
error), bump `ruff-pre-commit` to v0.15.17 to match the project ruff, regenerate
`.secrets.baseline`, and whitelist three in-progress `diagnostics` accessors for vulture.
