# Contributing to robotsix-chat

## Prerequisites

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/) (package manager)

## Setup

```bash
git clone https://github.com/robotsix/robotsix-chat.git
cd robotsix-chat
uv sync
pre-commit install
```

`pre-commit install` activates the Git hooks that run on every commit:
trailing-whitespace, YAML/TOML checks, ruff (lint + format), mypy,
bandit, pip-audit, and detect-secrets.

## Running checks manually

| Tool | Command | What it checks |
|---|---|---|
| ruff (lint) | `uv run ruff check .` | Code style, lint, and docstring rules |
| ruff (format) | `uv run ruff format --check .` | Code formatting |
| mypy | `uv run mypy .` | Static type checking (strict mode) |
| bandit | `uv run bandit -c pyproject.toml -r src/` | Security linting |
| pip-audit | `uv run pip-audit` | Known vulnerabilities in dependencies |
| pytest | `uv run pytest` | Test suite |

## Testing conventions

Tests for module `robotsix_chat.<module>` live under `tests/<module>/`,
mirroring the per-module source layout (e.g. `tests/chat/` for
`robotsix_chat.chat`, `tests/config/` for `robotsix_chat.config`).
Do not place tests directly in the `tests/` root.

## Dependency auditing

`pip-audit` checks installed packages against the [PyPA Advisory
Database](https://github.com/pypa/advisory-database). It runs
automatically as a pre-commit hook when `uv.lock` changes, and you can
run it manually with `uv run pip-audit`.

If a vulnerability is flagged, see [`SECURITY.md`](SECURITY.md) for
the reporting and response process.

## Pre-commit hooks

After `pre-commit install`, the following hooks run on staged files:

1. **pre-commit-hooks** — trailing whitespace, file endings, YAML/TOML syntax
2. **ruff** — lint with auto-fix, then format
3. **mypy** — strict type checking
4. **bandit** — security-focused AST scanner
5. **pip-audit** — dependency vulnerability scan (only when `uv.lock` changes)
6. **detect-secrets** — secret leakage prevention

To run all hooks without committing: `pre-commit run --all-files`
