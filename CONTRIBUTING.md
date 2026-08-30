# Contributing to robotsix-chat

## Prerequisites

- Python 3.14 or later
- [uv](https://docs.astral.sh/uv/) (package manager)

## Setup

```bash
git clone https://github.com/damien-robotsix/robotsix-chat.git
cd robotsix-chat
make install          # or: uv sync --all-extras
pre-commit install
```

`pre-commit install` activates the Git hooks that run on every commit: trailing-whitespace,
YAML/TOML checks, ruff (lint + format), mypy, uv audit, and detect-secrets.

## Running checks manually

The `Makefile` provides convenient shorthand targets for common operations. `make lint`,
`make typecheck`, `make test`, and `make all` (which runs lint + format-check + typecheck + test)
are the quickest way to validate a change. Developers who prefer raw `uv run` can continue using the
commands below; the Makefile targets are simple wrappers with no hidden logic.

| Tool                | `make` target                   | Raw command                                                                                       | What it checks                        |
| ------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------- |
| ruff (lint)         | `make lint`                     | `uv run ruff check src/robotsix_chat tests`                                                       | Code style, lint, and docstring rules |
| ruff (format check) | `make format-check`             | `uv run ruff format --check src/robotsix_chat tests && uv run ruff check src/robotsix_chat tests` | Code formatting                       |
| mypy                | `make typecheck`                | `uv run mypy src/robotsix_chat tests`                                                             | Static type checking (strict mode)    |
| uv audit            | *(no target — use raw command)* | `uv audit`                                                                                        | Known vulnerabilities in dependencies |
| pytest              | `make test`                     | `uv run pytest`                                                                                   | Test suite                            |
| all of the above    | `make all`                      | *(runs lint, format-check, typecheck, test)*                                                      | Pre-PR validation                     |

## Testing conventions

See [AGENT.md](AGENT.md) > Testing conventions for the canonical rules.

## Dependency auditing

`uv audit` checks installed packages against the
[PyPA Advisory Database](https://github.com/pypa/advisory-database). It runs automatically as a
pre-commit hook when `uv.lock` changes, and you can run it manually with `uv audit`.

If a vulnerability is flagged, see [`SECURITY.md`](SECURITY.md) for the reporting and response
process.

## Changelog

The changelog is generated automatically by **release-please** from
[conventional commits](https://www.conventionalcommits.org/). PR titles and commit subjects must use
the conventional format: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`, or `ci:`.

No manual changelog fragment files are needed — release-please creates and updates `CHANGELOG.md` as
part of its release workflow.

## Pre-commit hooks

After `pre-commit install`, the following hooks run on staged files:

1. **pre-commit-hooks** — trailing whitespace, file endings, YAML/TOML syntax
1. **ruff** — lint with auto-fix, then format
1. **mypy** — strict type checking
1. **uv audit** — dependency vulnerability scan (only when `uv.lock` changes)
1. **detect-secrets** — secret leakage prevention
1. **markdownlint-cli2** — structural Markdown linting (broken links, duplicate headings, missing
   alt text)
1. **mdformat** — consistent Markdown formatting (100-char wrap, 2-space indentation, numbered
   lists)

To run all hooks without committing: `pre-commit run --all-files`
