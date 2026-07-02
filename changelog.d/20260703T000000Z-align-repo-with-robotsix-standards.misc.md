Align the repo with robotsix-standards:

- Raise the CI coverage gate to 88 (was 60; the fleet floor is 80 and the gate ratchets — CI and
  pyproject `fail_under` now match).
- Drop the forbidden PyPI publish job from `release.yml` (the stack publishes to no package index).
- Migrate `release-image.yml` to the shared `docker-release.yml` reusable workflow (keeps the
  verify-CI gate; gains the publish-time Trivy gate).
- Add a PR-time container image build + Trivy scan job (blocks on fixable CRITICAL/HIGH findings
  only; GHA layer cache).
- Pin `robotsix-yaml-config` and `robotsix-agent-comm` git sources to commit SHAs (repo baseline: no
  branch refs).
- Add a `docs/modules.yaml` drift check to CI (`scripts/check_modules_registry.py`) and fix the two
  drift items it caught (unregistered `tests/common/subsession_fakes.py`, stale
  `tests/common/mock_helpers.py`).
- Rename `tests/board_reader/` to `tests/board/` (mirror the package) and move `tests/test_smoke.py`
  under `tests/common/`.
- Set ruff `target-version` to `py314` (matching `requires-python`; adopts PEP 758 unparenthesized
  `except` formatting), move per-file-ignores to table form with a justification comment per ignore,
  and add the standard `check-merge-conflict` / `check-added-large-files` pre-commit hooks.
