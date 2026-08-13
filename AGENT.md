# robotsix-chat — agent-oriented reference

This repo follows the
[robotsix stack standards](https://github.com/damien-robotsix/robotsix-standards); read those first
— this file carries only repo-specific knowledge.

## Repo overview

robotsix-chat is a **deployable component** (per the standards' distribution tiers): a browser + SSE
chat server for an LLM agent. It drives an LLM through `robotsix-llmio` (pick a `model_level`, never
a concrete provider) and serves it over HTTP:

- `GET /` — self-contained browser chat UI (single HTML file, no build step)
- `POST /chat` — accepts `{"message": "..."}`, returns the agent reply as SSE (`text/event-stream`)
  frames
- `GET /health` — liveness probe, returns `200 {"status": "ok"}`

Key stack: **Python ≥3.14**, **Starlette** (ASGI), `robotsix-llmio`, `pydantic`, `uvicorn`.
Entrypoint: `robotsix-chat` (console script installed by the package).

## Configuration (config standard)

One JSON file, loaded by `robotsix_config.load_config` into the pydantic `Settings` model
(`src/robotsix_chat/config/settings.py`). No env overlay, no CLI merge — the file is the only source
of values; model field defaults fill the gaps.

- `config/config.json` — **committed defaults template** (what central-deploy merges operator edits
  into). Never put real credentials in it.
- `config/config.schema.json` — committed typed JSON Schema, generated from `Settings`; the CI
  `check-config-schema` job fails when it drifts from the model.
- `ROBOTSIX_CONFIG_FILE` — the one env var, and it only *locates* the file. For local runs with real
  credentials, copy the template to the gitignored `config/config.local.json` and point
  `ROBOTSIX_CONFIG_FILE` at it.
- The server binds `server_host:server_port` from the config file (template default
  `127.0.0.1:8000`; containers need `0.0.0.0:8080` in their mounted config).

**Rule:** All new Pydantic config sub-models must include
`model_config = ConfigDict(extra="forbid")` to reject unknown JSON keys at load time rather than
silently ignoring them.

**Rule:** When modifying `src/robotsix_chat/config/settings.py`, regenerate
`config/config.schema.json` by running `uv run scripts/regenerate_schema.py` before committing. The
CI `check-config-schema` job will catch drift, but regenerating before commit avoids a wasteful CI
rebuild cycle.

**Rule:** When removing a field from a Pydantic `BaseModel` with
`model_config = ConfigDict(extra="forbid")`, add a `model_validator(mode="before")` that strips the
removed key from the input dict before validation. This prevents production startup crashes when
deployed config files still carry the legacy key (serialized before the removal).

**Rule:** Every settings model added to `src/robotsix_chat/config/models.py` must have a matching
`###` section in `docs/configuration.md` (under `## Settings reference`) documenting its JSON keys,
types, defaults, and descriptions — mirror an existing section (e.g. `### SFTP`) and keep it in the
same PR. A settings model without a config-doc section is incomplete.

**Rule:** Every field added to a Pydantic config model in `src/robotsix_chat/config/models.py` must
appear as a row in the model's `###` table in `docs/configuration.md` — even when the field is
appended to an existing model. A field declared and consumed but missing from its config-doc table
is an incomplete change.

**Rationale:** The existing rule covers new settings models requiring a `###` section, but not
fields added to existing models. The completeness_check epic found six separate fields that shipped
added to existing models with no matching table row (feedback.deploy_api_key, memory
recovery/subsession/autonomous-cluster fields, subsessions.user_chat_max_retries,
autonomous.max_idle_auto_turns, render_url.fleet_auth, http_probe.fleet_auth; the trigger ticket
fixed feedback.deploy_api_key in PR #1100).

## Deploy stack structure

Two compose files with different jobs (component standard):

### Root `docker-compose.yml` — local dev

Builds the multi-stage `Dockerfile`, tags `robotsix-chat:local`. Mounts `./config/config.local.json`
read-only at `/home/app/config/config.json` and the host `~/.claude` at `/home/app/.claude` (run
`claude login` once beforehand).

### `deploy/docker-compose.yml` — production (central-deploy contract)

Consumed by [robotsix-central-deploy](https://github.com/damien-robotsix/robotsix-central-deploy)
(first line: `# central-deploy-contract-version: 1`); central-deploy pulls
`ghcr.io/damien-robotsix/robotsix-chat:main`, applies its own lifecycle (restart, networking,
gateway routing), and redeploys on operator demand — no Watchtower, no `restart:`, no host binds.

- **Service**: single `robotsix-chat` service (implicitly primary).
- **Port**: `8088:8080` — the primary port is gateway-routed (`deploy.robotsix.net/<component>/*`).
- **Config**: label `robotsix.deploy.config-target: "/home/app/config/config.json"` + the
  `chat-config` volume mounted at `/home/app/config`; central-deploy writes the merged config there
  before every start. `ROBOTSIX_CONFIG_FILE` in `environment:` is wiring only.
- **State**: named volume `chat-data` → `/data` (knowledge store, cognee memory, HF cache); starts
  empty on first onboard.
- **Claude credentials**: label `robotsix.deploy.claude-mount: "true"` — central-deploy mounts its
  managed `claude-auth` named volume at `/home/app/.claude` (levels 3-4 claude-sdk transport).
  Authenticate via central-deploy's dashboard login flow, never by preparing host files.

### Container layout

Standardized robotsix layout (docker standard): non-root user `app`, uid/gid **1000**,
`WORKDIR /home/app`; the container listens on **8080** (from the mounted config), `EXPOSE 8080`,
stdlib-only `HEALTHCHECK` on `/health`, exec-form `ENTRYPOINT ["robotsix-chat"]` (no entrypoint.sh).

## Long-term memory (cognee)

The agent is stateless by default. The optional `memory` extra adds persistent, cross-conversation
memory via embedded **cognee**: before each reply the agent `recall`s relevant memory and folds it
into the system prompt; after replying it persists the exchange (`add` + `cognify`) in a
**background task** so consolidation never adds latency. Disabled by default; a `NullMemory` no-op
is used when off or when the extra is absent — the agent then behaves exactly as before.

- **Selection**: `memory.enabled` (config). `build_memory()` returns `CogneeMemory` only when
  enabled *and* cognee is importable, else `NullMemory`.
- **Backends** (cognee runs embedded; the heavy inference is offloaded):
  - *Extraction LLM* — OpenRouter via litellm's `custom` provider
    (`openrouter/deepseek/deepseek-v4-flash`); needs `memory.llm.api_key`.
  - *Embeddings* — a remote **OpenAI-compatible** server (self-hosted Ollama / `bge-m3`, 1024-dim);
    provider must be `openai_compatible`, needs `memory.embedding.endpoint` (e.g.
    `http://host:11434/v1`). Embeddings are **not** run on the chat host.
- **Storage**: cognee's stores live under `memory.data_dir` (default `/data/cognee`) — keep it on
  the persistent `/data` volume so memory survives redeploys.
- **Tracing**: cognee traffic uses its **own** Langfuse project — named by `memory.langfuse_project`
  (default `robotsix-chat-cognee`) and resolved against the canonical top-level `langfuse.projects`
  block — never the main `robotsix-chat` project's credentials (component standard: one Langfuse
  project per LLM-generating function).
- **Safety**: `recall`/`remember` never raise into the chat path (errors are logged; the reply
  proceeds without memory).
- **Resilience caveat**: memory depends on the embedding server being reachable; while it's down,
  recall/consolidation silently no-op.

Config keys: `memory.enabled`, `memory.data_dir`, `memory.recall_search_type`,
`memory.llm.{provider,model,endpoint,api_key}`,
`memory.embedding.{provider,model,endpoint,dimensions,api_key,huggingface_tokenizer}` — see
`config/config.json` for defaults.

## Key file map

- `docker-compose.yml` — local dev compose (builds from Dockerfile, tag `robotsix-chat:local`)
- `deploy/docker-compose.yml` — production deploy contract (central-deploy; GHCR image)
- `Dockerfile` — multi-stage build (`python:3.14-slim`, Node.js + `claude` CLI, non-root `app`,
  `EXPOSE 8080`)
- `config/config.json` — committed JSON config defaults template
- `config/config.schema.json` — committed typed schema (CI-checked against `Settings`)
- `src/robotsix_chat/config/settings.py` — `Settings` (pydantic) + `robotsix_config.load_config`
- `src/robotsix_chat/memory/` — optional long-term memory: `base.py` (`ChatMemory` protocol +
  `NullMemory`), `cognee.py` (`CogneeMemory`), `__init__.py` (`build_memory()`)
- `src/robotsix_chat/chat/server/` — Starlette ASGI app (`app.py`, `cli.py`) and `routes/` package
  (`__init__.py`, `chat.py`, `sessions.py`, `subsessions.py`, `events.py`, `errors.py`,
  `constants.py`, `_shared.py`); `GET /`, `POST /chat`, `GET /health`
- `.github/workflows/release-image.yml` — GHCR publish caller (shared `docker-release.yml`)

## Formatting conventions

**Rule:** When adding or editing ordered lists in `docs/`, use `1.` for every item (CommonMark renders them sequentially regardless). Do not use sequential numbering (`2.`, `3.`, …) — the `mdformat --number` pre-commit hook flattens them back to `1.`, creating a wasted CI auto-fix cycle. This applies to all Markdown files under `docs/`.

**Rationale:** PRs #1297 and #1298 both hit this cycle; every PR touching the three ordered lists in `docs/configuration.md` will trigger it until the hook is changed.

## Python conventions

**Rule:** Always write exception-tuple handlers in the parenthesized form
`except (TypeA, TypeB, TypeC):`. Never use the bare comma form `except TypeA, TypeB, TypeC:` — it
has different meaning in Python 2, is a SyntaxError on Python 3.0-3.13, and only parses as a tuple
from Python 3.14 (PEP 758), so it is a cross-version portability and readability hazard. This
applies to both `except` and `except*` clauses.

**Rationale:** 17+ comma-form handlers still shipped across `src/`; new code repeatedly copies the
in-file comma-form pattern even while sweep/guard tickets standardize on the tuple form. A concrete
rule prevents re-introduction in fresh error handling.

## CI workflow conventions

**Rule:** All third-party GitHub Actions must be pinned by immutable 40-character commit SHA with
the semantic version as a trailing comment (e.g.
`actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683  # v4.2.2`). Do not use mutable tags
(`@v4`, `@v3`) without a SHA. Dependabot's `package-ecosystem: github-actions` will auto-update SHAs
on new releases.

**Rule:** All reusable workflow references (distinguishable by the `.github/workflows/` path
component in the `uses:` value) must use the full 40-character commit SHA of the target repo's
current HEAD on its default branch. Never use mutable refs (`@main`, `@master`, `@v1`, `@latest`).
Add a trailing version comment for readability (e.g. `# v0.2.0` or `# main`).

**Tool:** Use `scripts/resolve_github_sha.py` to resolve a GitHub `owner/repo` + ref to a 40-char
commit SHA via `git ls-remote`:

```bash
uv run scripts/resolve_github_sha.py damien-robotsix/robotsix-github-workflows main
uv run scripts/resolve_github_sha.py actions/checkout v4.2.2
```

The script handles annotated tags (peeled to the underlying commit), branches, and bare refs.

**Rule:** The `image-scan` job in `ci.yml` is deliberately hand-rolled (not the shared
`docker-pr-scan.yml`): the shared workflow uses the GHA layer cache, which was measured at 45-55 min
per run on this multi-GB image vs ~4 min cold. Do not switch back without timing both paths (docker
standard, "CI-time image scan").

**Rule:** The dependency CVE audit runs in the `lockfile` job (with the curated
`--ignore-until-fixed` list), and `run-audit: false` is passed to the shared `python-ci.yml`. The
shared audit step has no ignore mechanism, so enabling both hard-blocks CI on CVEs that have no
released fix.

**Rule:** Never commit pre-compiled tool binaries (e.g. hadolint, actionlint, shellcheck) to the
repository — the pre-commit hooks fetch their own binaries via their hook installers. If a tool
binary appears on your branch, remove it from tracking and add it to the `.gitignore` "Pre-commit
tool binaries accidentally committed during local testing" block instead of registering it as a
module path.

## Testing conventions

Tests for module `robotsix_chat.<module>` live under `tests/<module>/`, mirroring the per-module
source layout (e.g. `tests/chat/` for `robotsix_chat.chat`, `tests/config/` for
`robotsix_chat.config`). Do not place tests directly in the `tests/` root.

**Rule:** When a `ChatAgent` protocol parameter is added or changed, update ALL mock classes that
implement the protocol (`_MockAgent`, `MockAgent`, and any other test-local mocks) in the same PR.
Run `mypy` on the full test suite to verify protocol conformance — a mock that lacks a keyword
argument silently passes structural subtyping at runtime but fails static `mypy --strict` checks.

**Rule:** Use class-level monkeypatch-based fixtures (not instance-level MagicMock assignments) to
isolate tests from internal I/O in `AutonomousRunner` test fixtures — match the pattern in
`tests/autonomous/test_runner.py`'s `_mock_persistence` class fixture.

**Rationale:** Instance-level MagicMock assignments on the `autonomous_runner` fixture in
`tests/chat/server/test_autonomous_endpoints.py` have generated 5+ CI-fix tickets in ~24 hours
(spanning PRs #784, #820, #823, #824, #828), each patching one more leaky abstraction. The
class-level `monkeypatch` approach in `tests/autonomous/test_runner.py` has proven stable under
xdist without follow-up patches.

**Rule:** Any change that adds an HTTP authorization header, relaxes an SSRF/allowlist check, or
opens a new network-access path must ship with tests covering the newly-opened branches (e.g. header
injection, allowlist/SSRF bypass, redirect hops). Security-sensitive request paths live under
`src/robotsix_chat/public_fetch/`, `http_probe`, and `render_url` and may not ship without branch
coverage.

**Rationale:** PR #993 (ticket 20260729T105017Z) added fleet_auth to public_fetch — Basic-Auth
injection and two SSRF-bypass branches — with zero new tests in
tests/public_fetch/test_public_fetch.py; the security-critical code merged untested.

**Rule:** Test files that assert on secret-shaped placeholder values (e.g.
`github_app_private_key='k'`, `'cached-token'`, `'fresh-token'`) must append
`# pragma: allowlist secret` to those lines so `detect-secrets`' KeywordDetector does not fail
pre-commit. This is the established convention in `tests/refdocs/test_refdocs.py` and
`tests/version_check/test_version_check.py`.

**Rationale:** PR #1068 (ticket 20260730T051112Z) had to add 7 such suppressions in the fixing_ci
cycle after pre-commit detect-secrets flagged secret-looking test fixtures; adding the pragma up
front avoids a CI rebuild cycle.

**Rule:** Any change that adds a new SSE/event frame emission path (e.g. subsession
auto-pause/resume `notification` frames, `SSE_NOTIFICATION_TYPE`) must ship with a frame-assertion
test that builds the env with a `RecordingSink` and asserts the emitted frame's type/title/fields.
An emission path is only "covered" when the test env is wired with an `event_sink`; a
`build_env(...)` call without one cannot capture a notification frame.

**Rationale:** PR #1095 (ticket 20260731T235525Z) added auto-pause (worker.py:1040) and resume
(watcher.py:109/:152) notification frames with zero tests — `git show c18e4c1 --stat` touched no
test files, tests/subsessions/test_watcher.py has no event_sink references, and test_worker.py:410
builds env without an event_sink. PR #1066 (failing-CI resume branch) shipped untested the same way.
Both new event-emission paths merged uncovered.

## Feature flags and activation

**Rule:** Any feature gated behind a runtime flag (`enabled: false`, a feature toggle, or a config
key that must be set to opt in) must include an activation step in its definition of done. A ticket
that ships a new feature without documenting how to activate it (or without verifying it runs
post-activation) is incomplete.

Every ticket for a flag-gated feature must cover:

1. **Activation config** — the exact config keys and target values needed to turn the feature on
   (e.g. `feedback.enabled: true`, `feedback.board_url: "<url>"`). The committed
   `config/config.json` template may carry `"enabled": false` as a safe default; the ticket must
   still specify what an operator changes to activate.
2. **Live-proof step** — a concrete verification that the feature fires after activation (e.g.
   "verify FeedbackRunner fires after idle compaction", "check the log for
   `FeedbackRunner started`").
3. **Post-deploy follow-up** — a task or checklist item to revisit the config after the deploy
   settles (closed-loop: did the feature actually turn on in production?).

The implementing agent is responsible for including these in the ticket's acceptance criteria or in
a follow-up task filed under `tasks/`. A feature that ships default-off with no documented
activation path is a process bug — treat it the same as a feature that ships broken.

## Module registration

**Rule:** Every new file created under `src/robotsix_chat/<module>/` or `tests/<module>/` must be
registered in `docs/modules.yaml` under the corresponding module's `paths:` list. The
`modules-registration` pre-commit hook catches unregistered files at commit time so CI does not
waste a cycle on the drift. Run
`uv run robotsix-modules check-registration docs/modules.yaml --root .` to verify locally before
committing.

## Enum / UI surface sync

**Rule:** When adding a new `SubsessionKind` member in `src/robotsix_chat/subsessions/models.py`,
add a matching `kind === "<value>"` case to the `subsKindLabel` function in
`src/robotsix_chat/ui/static/chat.js` in the same commit. The CI `SubsessionKind audit` job
(`scripts/check_subsession_kinds.py`) scans `chat.js`/`index.html` for these string-literal
comparisons and fails CI when a canonical kind value lacks a frontend case.

**Rationale:** PR #1138 (ticket 20260802T101159Z, `SubsessionKind.ON_CLOSE`) tripped the audit in a
`fixing_ci` cycle because the enum member was added in `models.py` without the corresponding
`kind === "on_close"` label in `subsKindLabel`; the fix then shipped as a separate CI-fix commit
(b0f0f87).

## Direct repo tooling

**Rule:** When adding a tool to `src/robotsix_chat/repo/direct/`, document it in
`src/robotsix_chat/repo/direct/skill.md` (purpose, preconditions, error/status responses, and any
confirmation-gate contract). The skill file is read at call time by `load_direct_repo_skill()` and
injected into the agent system prompt via the `_inject_skills` mapping in `app.py` — a tool or skill
that is not registered there is silently undiscoverable to the chat agent with no compile-time or CI
signal.

**Rationale:** PR #1033 / ticket 20260730T141753Z delivered two confirmation-gated mutation tools
together with their skill.md docs and the app.py:605 injection in one change. The pattern is uniform
across ~10 components (public_fetch, render_url, github_security/actions, ticket_poll, etc.), each
with a `load_<component>_skill()` returning the dynamic skill doc and an `(enabled, name, load_...)`
entry in `_inject_skills`.

**Rule:** When agent-facing docs (system prompt, config docstrings, skill.md) reference a
direct-repo tool, use the exported callable name from `build_direct_repo_tools` (e.g.
`merge_direct_repo_pr`), never the internal client method name (e.g. `merge_pr`).

**Rationale:** PR #1089 / ticket 20260731T063148Z updated two doc sites (models.py:330,
settings.py:597-599) to reference `merge_pr`, which exists only as an internal client method; the
exported agent tool is `merge_direct_repo_pr`.

## Prompt engineering conventions

**Rule:** When constructing prompts that include internal metadata, scaffolding, or subsession
summaries (e.g. in `feedback/runner.py:_build_feedback_prompt`), fence the metadata section with
clear boundary markers like `=== INTERNAL METADATA — NOT part of the conversation ===` and append an
explicit rule telling the downstream model the block was never shown to the user. Never concatenate
metadata directly after the transcript without a boundary.

**Rationale:** Ticket 20260809T061049Z identified that the feedback analyzer's prompt builder in
`_build_feedback_prompt` concatenated subsession summary rows directly after the conversation
transcript with only a blank line separator. The downstream LLM read the block as trailing assistant
output, generating 12 false-positive tickets. The fix (fenced metadata section + explicit rule) is
drafted, but the convention should be documented in AGENT.md so future prompt builders follow the
same pattern.

## Prompt governance

**Rule:** Every edit to the `build_autonomous_instruction()` return text in
`src/robotsix_chat/autonomous/prompts.py` MUST bump `AUTONOMOUS_PROMPT_VERSION` in the same module,
add a new `## AUTONOMOUS v<N> — <YYYY-MM-DD> — <ticket-id>` entry at the top of the
`## Autonomous Prompt Changelog` section in `docs/system_prompt_changelog.md`, and record the SHA256
of the live output (`sha256(build_autonomous_instruction(Settings()).encode())`).
`tests/config/test_system_prompt_governance.py` enforces this, so skipping the bump wastes a CI
cycle.

**Rationale:** Ticket 20260804T004152Z-e556 (PR #1169) extended the existing SYSTEM_PROMPT
governance to the autonomous appendix after two prompt edits (#1157, #1165) shipped silent.
Documenting the convention in AGENT.md prevents future contributors from wasting a fixing_ci cycle
discovering the governance test, matching the style of the CI-enforced rules already in the
config/module-registration sections.

## Standards compliance verification

**Rule:** When making claims about whether a repo or component complies with a robotsix-standards
requirement (especially packaging, dependency, or distribution standards), consult the live
[robotsix-standards](https://github.com/damien-robotsix/robotsix-standards) repo rather than relying
on recalled memory. For packaging and dependency compliance, consult
`docs/distribution-packaging.md` in that repo. This applies especially during fleet-wide assessments
— standards evolve, and recalled memory can be stale.

**Rationale:** An agent (session 138ddb0d) claimed a repo was compliant with the robotsix-ui
distribution standard based on recalled memory, but it was non-compliant (used a public npm semver
range instead of a git-pinned URL). Consulting the live standards repo would have caught this.

## Task tracking

Persistent, human-readable task tracking lives under `tasks/` at the repo root:

- `tasks/TASKS.md` — active tasks (pending, in-progress, blocked).
- `tasks/ARCHIVE.md` — completed tasks (history preserved).
- `tasks/README.md` — documents the format and the read/add/update/archive workflow.

At the start of every conversation, read `tasks/TASKS.md` to pick up any pending work from prior
conversations. When work is done, archive the task by moving its section from `TASKS.md` into
`ARCHIVE.md`. The format is structured Markdown — a person can inspect or edit the files by hand.
