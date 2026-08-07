## 0.0.0 (unreleased)

- `fetch_workflow_run_annotations`: when the GitHub Checks API returns 403
  (token lacks `checks: read` permission), the tool now falls back to
  fetching raw job logs via the Actions API instead of returning a generic
  error.  This lets the agent self-diagnose CI failures even when the
  GitHub App installation doesn't have the `checks: read` permission.
- Added dedicated unit test coverage for `build_sftp_tools` factory (`tests/sftp/test_init.py`): disabled gating, tool wiring, SftpError/SftpPathError translation, ImportError fallback, and empty-directory special-casing.
- Added per-monitor long-poll wake-up mechanism for paused periodic subsessions. Each paused monitor now polls the mill API directly at `paused_monitor_long_poll_interval_seconds` (default 15s) instead of relying solely on the centralized 60s watcher poll, reducing wake-up latency from up to 60s to ~15s. The background watcher remains as a safety-net backup.
- Added `low_risk_actions` configuration option: a list of action names or
  descriptions that the agent may perform without requesting human confirmation.
  When non-empty, the system prompt instructs the agent that these actions are
  pre-authorized, lifting the default ask-before-acting gate for them.
  Default `[]` (no actions are pre-authorized beyond the prompt's built-in
  low-risk heuristics).
- Removed stale "Deprecated wrapper methods remain on this class" sentence from `DirectRepoClient` module docstring — the 14 referenced methods were already removed.
- Upgraded aiohttp from 3.14.1 to 3.14.3 and cryptography from 49.0.0 to 50.0.0 to resolve CVE advisories flagged by `uv audit`.
- Pre-authorized ticket monitors now receive the standing-authority directive from the first run (via `dedup_key` fallback), so they no longer surface `human_issue_approval` gates that the operator already authorized. The PRE-AUTHORIZED instruction is also ordered before the decision-blocked paragraph to give it priority.
- Added guidance to the agent system prompt: when the user asks to prioritize or surface "associated tickets," the assistant now proactively queries the full board (GET /tickets) and filters by subject/repo/ticket-id prefix before reporting, ensuring the user gets a complete picture without needing to nudge for a re-check.
- Periodic subsessions gain sibling spawn capability: a periodic/monitor can now spawn `task` (remediation) and `user_chat` (escalation) subsessions as siblings attached to the holding parent conversation rather than as nested children. Nested `periodic` and `on_close` spawns remain forbidden and are silently rejected with audit logging. New self-adjustment tools (`update_periodic_instructions`, `adjust_periodic_interval`, `adjust_periodic_budget`) let a periodic monitor revise its own purpose within operator-configured bounds (`periodic_max_interval_seconds`, `periodic_max_total_runs`).
- Agent instruction: when a tool call returns an HTTP error, consult
  knowledge notes and reference docs for the correct endpoint before
  guessing alternate routes.  Discovered routes are persisted to the
  ``topic: endpoints`` knowledge note for future sessions.
- Enable `pin_bump` periodic workflow for automated fleet dependency pin bumps.
- Watcher now detects PRs closed without merging when the owning ticket
  is not yet terminal — publishes a high-urgency SSE notification and
  attempts to create a follow-up ticket on the board so the operator
  sees it immediately rather than only at monitor-report time.
  Merge conflicts on tracked PRs are also flagged with a high-urgency
  notification as soon as the watcher detects them, instead of waiting
  for the monitor to report.
- Add PRE-SPAWN GUARD directive to the agent system prompt: before spawning any
  subsession (task, user_chat, periodic), the agent MUST call list_subsessions
  and check for an existing OPEN subsession with the same purpose or dedup_key,
  reusing it instead of creating a duplicate. Includes specific guidance for
  user_chat subsessions where a single decision queue should have exactly one
  subsession.
- System prompt v83: added directive to preserve factual fidelity when reporting subsession outcomes — when the summary states a specific cause, reason, or actor (e.g. "ticket closed by operator"), echo that exact claim rather than substituting a vague paraphrase like "closed itself cleanly."
- Document `autonomous.max_idle_auto_turns` in the Autonomous configuration table (was declared in the model and consumed at runtime but missing from the docs).
- System prompt: add directive to avoid redundant ticket-history repetition when the user re-asks for monitoring status. The agent now directly states current state and next action without re-listing full ticket history or echoing already-seen subsession summaries. (v82)
- Added `### Docker Digest` documentation section to `docs/configuration.md` covering `docker_digest.enabled`, `docker_digest.timeout`, `docker_digest.registry_host`, and `docker_digest.auth_url` — the last wired settings group that was missing its config-doc table.
- Added `on_close` as a new `SubsessionKind`: subsessions of this kind wait until the parent session closes, then execute as a one-shot task — use `spawn_subsession(kind="on_close", ...)` to schedule work that fires when a conversation ends.
- Added comprehensive test coverage for ``ActionsClient`` (`tests/repo/direct/test_actions_client.py`, 26 tests) — covering `dispatch_workflow`, `list_workflow_runs`, `get_workflow_run_jobs`, `get_job_log`, `set_actions_secret`, `get_workflow_run_annotations`, `_diagnose_billing_failure`, and auth-failure paths. Extracted shared fixtures to `tests/repo/direct/conftest.py`.
- Document 5 missing `memory.*` fields in the Memory (cognee) settings table in `docs/configuration.md`: `background_recall_enabled`, `deep_recall_search_type`, `deep_recall_timeout_seconds`, `remember_max_attempts`, and `remember_retry_backoff_seconds`. Also fix stale defaults for `recall_search_type`, `remember_timeout_seconds`, and `memory.llm.model` that had drifted from the model.
- Document `direct_repo.direct_fix_enabled` in the Direct Repo (GitHub App) config table (`docs/configuration.md`). (mill: direct_repo.direct_fix_enabled missing from ### Direct Repo table in docs/configuration.md — sibling-pattern doc gap (20260802T052249Z-direct-repo-direct-fix-enabled-missing-f-86ee))
- Added unit tests for `robotsix_chat.common.unified_diff.apply_patch` (22 tests covering single/multiple hunks, edge cases, and error paths).
- Wire ticket-ID resolution into ``merge_pull_request`` and direct-repo tools (``reset_implement_spawn_counter``, ``_assert_blocked_and_scoped``, ``_check_blocked_exhausted``) so paraphrased / abbreviated ticket IDs are resolved against the live board before constructing board API URLs, preventing 404 failures.
- Periodic monitors now enter a true `PAUSED` state (not `CLOSED`) when auto-paused by the idle-guard. The worker stays alive and blocks on an event-driven inbox signal; the watcher sends an immediate wake message when the tracked ticket's state changes, so auto-resume actually works end-to-end. The pause notice wording now accurately reflects the real behavior.
- Split `tests/repo/direct/test_direct_repo.py` (4052 lines, 110 tests) into 9
  per-tool test modules with shared fixtures in `conftest.py`.
- Add dedicated unit tests for `BoardClient` (`tests/repo/direct/test_board_client.py`) covering `get_ticket_state`, `resume_blocked_ticket`, `get_ticket_data`, and `count_implement_cycles` (17 tests, respx-mocked HTTP).
- Removed 14 deprecated wrapper methods from `DirectRepoClient` that had zero
  production callers (all delegated to `BoardClient`, `ActionsClient`, or
  `unified_diff.apply_patch`): `_fetch_ticket_field`, `get_ticket_state`,
  `resume_blocked_ticket`, `get_ticket_data`, `count_implement_cycles`,
  `get_job_log`, `_get_repo_public_key`, `set_actions_secret`,
  `dispatch_workflow`, `list_workflow_runs`, `get_workflow_run_jobs`,
  `get_workflow_run_annotations`, `apply_patch`, `_diagnose_billing_failure`.
- Removed dead `_strip_legacy_timeout` model validators from `GitHubSecuritySettings` and `GitHubActionsSettings` — the legacy `timeout` key was fully removed from both config sections on 2026-07-28.
- `http_probe.fleet_auth` now documented in the HTTP Probe settings reference table (`docs/configuration.md`), matching the Render URL and Public Fetch sections.
- Surface human-approval tickets with a concrete recommendation (approve/close + specific reason) and set expectations about the auto-escalation timeout window, preventing silent stalls in autonomous workflows.
- Settings UI: render `agent_instruction` as a multi-line textarea instead of a single-line text input, so long instruction strings wrap and are comfortably readable and editable.
- Autonomous session presets now support self-refinement (`self_refine: true`): after each run completes, an LLM step analyses the outcome and proposes an updated "lessons learned" addendum for the next run.  Configurable per-preset; optionally requires operator approval before the refinement takes effect (`self_refine_require_approval: true`).  Refinements are bounded (capped addendum length, max history entries) and auditable (logged old → new with triggering feedback).  New API endpoints: `GET /autonomous/definitions/{name}/refinements`, `POST .../{id}/accept`, `POST .../{id}/reject`, `POST .../reset`.
- Document `render_url.fleet_auth` in the Render URL configuration table, matching the `public_fetch.fleet_auth` pattern.
- Document 5 missing `memory.*` config fields in the Memory (cognee) table: `subsession_enabled`, `autonomous_enabled`, `auto_recovery_enabled`, `frozen_store_recovery_minutes`, and `recovery_cooldown_minutes`.
- Enable `mypy_baseline` periodic workflow to track mypy baseline entry counts and catch type regressions.
- Add Python conventions section to `AGENT.md` with rule requiring parenthesized exception-tuple handlers (`except (A, B):` over `except A, B:`).
- Add `explore`-first guidance to implement agent system prompt: for architectural questions (locating state-field handlers, feedback-injection points, orchestration functions), use `explore` before falling back to raw `run_command` grep/sed — avoids duplicate workspace/site-packages searches that inflated a recent trace to 165 run_command calls.
- Agent instruction: require explicit credential values in ticket descriptions
  for credential-bearing tickets, and add credential-verification guidance
  before merging PRs that modify stored credentials or password hashes.
- Autonomous sessions: generalize from a single hard-coded session to configurable named session definitions.  Each definition (`autonomous.sessions[]`) carries its own prompt, trigger type (`"periodic"` or `"on_close"` for continuous chaining), and enabled flag.  When no definitions are configured, a single backward-compatible default preset is synthesized at runtime — existing behavior is unchanged out of the box.  New API endpoints: `GET /autonomous/definitions` (list definitions with active sessions) and `POST /autonomous/definitions/{name}/run` (manual one-shot trigger).
- Persist the operator's reply-style directive as a durable, versioned artifact
  (`docs/prompt-style.md`) so it is automatically injected into every system prompt
  build.  The style file is the single source of truth for reply formatting;
  a governance test fails if the file is deleted or unreferenced.
- Document `feedback.deploy_api_key` in the Feedback settings table in `docs/configuration.md`.
- Update direct-repo guardrail docstrings and system prompt to acknowledge the ``merge_pr`` tool (BLOCKED-only) instead of claiming no merge capability exists on the direct-repo path.
- Add single-repo PR verification to the retrospect stage: before closing a ticket, confirm that the PR recorded in ``merge.md`` is still present and merged on the forge. If the PR has been deleted or is no longer merged, the ticket is blocked instead of closed, preventing fixes from being silently dropped.
- Fix `max_idle_runs` docstring default from 5 to 3 (matching shipped behaviour), and correct the paused-monitor config key reference in `docs/periodic-checks.md` from `auto_stop_no_change_runs` to `max_idle_runs`.
- Diagnosed misrouted ticket 20260731T174638Z: mail account-add crash targets
  functionality (``default_account_id``, account CRUD handler) not present in
  robotsix-chat; likely belongs in robotsix-mill or the auto-mail board server
- Subsession auto-pause and resume now emit browser notification events
  (SSE ``notification`` type) so the operator can see which monitor paused
  or resumed, including the tracked ticket id and checkpoint state.
- Remove redundant `system_prompt` from `.robotsix-mill/periodic/monitor_9d6a.yaml` — the `prompt_overlay` already covers the role, and the mill's periodic loader treats the two fields as mutually exclusive (having both raises a `ValueError` at load time). (mill: monitor_9d6a.yaml: anomalous `system_prompt` field duplicates `prompt_overlay` content (20260731T122421Z-monitor-9d6a-yaml-anomalous-system-promp-f3d4))
- Add "Direct repo tooling" section to AGENT.md: when adding a tool to
  `src/robotsix_chat/repo/direct/`, document it in `skill.md` so the skill
  is discoverable via `load_direct_repo_skill()` and `_inject_skills`.)
- Monitors: watcher no longer resumes paused monitors when CI is
  failing on the tracked PR — the auto-pause delivery already
  escalated to the operator, and resuming would only create a
  wasteful pause-resume-pause loop since the monitor's agent cannot
  fix source-code or dependency issues on its own. (mill: Pause auto-retry monitors that block on substantive CI failures (20260731T200830Z-pause-auto-retry-monitors-that-block-on-c6b8))
- Route `ticket_poll` direct (non-component) single and batch ticket-fetch
  paths through `BoardClient` (`get_ticket_data`), deleting the duplicated
  `_fetch_one_direct` body that re-implemented `GET /tickets/{id}` with
  bearer auth, retry, and JSON parsing.  `BoardClient._fetch_ticket_field`
  now uses `RetryClient` so both `repo/direct` and `ticket_poll` benefit
  from the same retry policy (max_retries=2). (mill: Migrate ticket_poll's direct board-API fetch to the new BoardClient in repo/direct/board_client.py (20260801T000333Z-migrate-ticket-poll-s-direct-board-api-f-f73e))
- Split `DirectRepoClient` (1407→~1189 lines) into three focused modules:
  - `ActionsClient` (`repo/direct/actions_client.py`) — GitHub Actions workflow management (dispatch, runs, jobs, annotations, secrets, billing diagnosis).
  - `BoardClient` (`repo/direct/board_client.py`) — mill board API for ticket-state verification and lifecycle ops.
  - `common/unified_diff.py` — pure unified-diff applicator (no HTTP/I/O).
  `DirectRepoClient` is now a single-responsibility GitHub repo client (push, PR, merge, auto-merge, file content, repo creation, installation scope, security settings).
  Deprecated wrapper methods remain on `DirectRepoClient` for backward compatibility.
- Add "one decision at a time" rule to the user_chat subsession instructions: when multiple independent decisions are pending, present them sequentially with explicit confirmation after each answer before moving to the next.
- Suppress auto-pause / auto-stop summaries for monitor subsessions when the watched ticket is already in a terminal state ("closed" or "done"). This prevents stale "no change" chatter from monitors tracking long-closed tickets, while still delivering summaries for active tickets.
- Removed misleading instruction from monitor_9d6a periodic prompt overlay that asked agents to manually resolve abbreviated ticket IDs before calling `ticket_poll()` (the tool already handles this automatically).
- Extract `_board_connection` helper in `ticket_poll` to eliminate duplicated board-connection setup boilerplate across `build_merge_pull_request_tool` and `build_ticket_poll_tools`.
- Auto-continue prompts are now suppressed while any subsession is active (including periodic monitors sleeping between ticks). Previously, "Continue." prompts fired even when background work was in progress, producing spurious popups in the chat UI.
- Fix `patch_direct_repo_file` / `direct_fix` connectivity and cycle-count gate: the roster-based board-API path now uses retries with exponential backoff and falls back to the direct API; resume/unblock events are counted as evidence of prior exhaustion so the ≥3-cycle gate survives a resume-blocked reset.
- Deduplicate auto-pause notices in periodic subsessions: when a second monitor for the same ticket auto-pauses or auto-stops after the first already reported the no-change/terminal outcome, the second notice is suppressed before reaching the LLM, eliminating redundant "no change" reaction turns.
- Add "Tool failure interpretation" guidance to the implement agent system prompt and `ticket_poll` skill: teach the assistant to distinguish transient I/O failures (timeout, HTTP 5xx, unreachable) from facts about the queried resource's state, preventing conflation like "the board API returned an error, so the ticket must be closed."
- Add CI workflow convention rule: never commit pre-compiled tool binaries to the repository; add them to `.gitignore` instead of registering them as module paths.
- Added a contributor-side convention: every settings model in `src/robotsix_chat/config/models.py` must have a matching `###` section in `docs/configuration.md` (under `## Settings reference`), kept in the same PR.
- Add "Halt and Re-scope" workflow to system prompt: when the agent detects a policy violation, it now immediately halts and presents structured compliant alternatives with one-click actions to close superseded work, condensing a 4–5 turn resolution into 1–2 turns.
- Monitor subsessions tracking PRs now check CI status before staying paused. When a paused monitor's tracked PR has a failing CI workflow run on its head branch, the watcher resumes the monitor so the failure is surfaced rather than hidden. Previously, monitors would auto-pause after consecutive no-change runs even when the PR's CI was actively failing.
- Fix stale 401/403 error message in `fetch_public_url`: fleet-auth hosts now report "credentials may need updating" instead of "Only public, unauthenticated URLs are supported." Added test coverage for Basic-Auth header injection, allowlist bypass, and SSRF bypass for fleet-auth hosts. (mill: Fix stale 'only public, unauthenticated URLs' 401/403 message and add fleet_auth test coverage in public_fetch (20260731T024622Z-fix-stale-only-public-unauthenticated-ur-12ef))
- Component request client now follows HTTP redirects (``follow_redirects=True``) — fixes log-fetch returning HTTP 303 instead of the raw log body when a component endpoint redirects to a signed URL.
  - ``fetch_workflow_run_annotations`` no longer skips check runs with a failed conclusion that show ``annotations_count == 0``, avoiding silently-empty annotation results for failed CI runs.
- Fix: correct system-prompt version reference in CHANGELOG.md from v73 to v74 for the ticket-ID-fidelity entry, matching the version documented in `docs/system_prompt_changelog.md`.
- Add test coverage for the four error handlers in ``routes/errors.py`` (``http_exception_handler``, ``not_found_handler``, ``server_error_handler``, ``unhandled_exception_handler``)
- Refactor: extract shared `_check_preconditions` helper in `repo.direct`
  to eliminate ~15-line duplicated precondition guard (BLOCKED state +
  ≥3 implement cycles) that was copy-pasted between `direct_fix` and
  `patch_direct_repo_file`.
- Fix misleading error message in `direct_fix`/`patch_direct_repo_file` precondition check: when the board API returns a response without a `state` field, the error no longer incorrectly suggests checking API connectivity. Add regression tests for blocked tickets with 0 completed implement cycles.
- **System prompt v77** — added guidance to the Efficiency section directing the
  assistant to avoid dumping long sorted lists (20+ PR links, ticket enumerations,
  file inventories) inline in a single chat message. Long lists should use compact
  summaries with the full list as a separate artifact (knowledge note, split across
  replies, or narrowed query).
- Add consent-scoping and human_issue_approval safety rules to the autonomous MUTATION AUTHORIZATION prompt: auto-approval now only applies to tickets explicitly consented to (by ID, queue, or gate name), and non-consented human_issue_approval tickets require operator confirmation before transitioning.
- Fix SFTP private-key authentication: encode PEM string as bytes so asyncssh
  treats it as inline key data rather than a file path.
- Fix ``SftpClient.file_exists``: only return ``False`` for ``FX_NO_SUCH_FILE``
  (code 2); re-raise all other SFTP and connection errors as ``SftpError``
  instead of silently masking them.

- `apply_patch_to_file`: add optional `target_branch` parameter so BLOCKED
  tickets (regardless of implement-cycle count) can push a patched file
  directly to an existing branch.  `direct_fix` and `patch_direct_repo_file`
  error messages now mention this escape hatch alongside the ≥3-cycle
  precondition.
- Add retry (3 attempts with exponential backoff) and fallback (roster→direct board API) to ``direct_fix`` and ``patch_direct_repo_file`` board-API ticket fetches, so transient connectivity issues no longer hard-fail the tools.
- Add anti-re-emission guidance to the active-plan reaction prompt template (`_REACT_PROMPT_ACTIVE_PLAN_TEMPLATE`) in `delivery.py`, instructing the agent to reply with only a delta or one-sentence synthesis when subsession outcomes were already presented earlier — never re-emit a full table/rollup/enumeration verbatim.
- Subsession agents now have access to the ``notify_user`` tool (when
  ``notification.enabled`` is true).  Notifications are published on the
  owner's session so they reach the user's connected browser even when
  triggered from a background subsession worker.
- `ticket_poll` / `ticket_poll_batch`: resolve paraphrased ticket IDs (hash suffix or slug match) against the live board before making per-ticket requests, preventing 404s when IDs are derived from narrative text.
- Add `knowledge_store` to `SHARED_PARAMS` frozenset in `app.py`, matching the parameter already accepted by `create_app()` and `run_server()`.
- Fix `reset_implement_spawn_counter`: replace broken `DELETE /tickets/{id}/artifacts/implement_spawn_count`
  (HTTP 405 — board API has no artifact delete endpoint) with
  `POST /tickets/{id}/resume-blocked` using a spawn-counter-specific
  justification.  Removed dead `_delete_artifact_via_component` (never
  called) and `DirectRepoClient.delete_ticket_artifact` (orphaned).
- Wire `merge_pull_request` tool into the agent's tool suite so it can merge
  approved PRs for tickets in `waiting_auto_merge` / `human_mr_approval` state
  via the mill board's merge-now endpoint.  Update `skill.md` to document the
  tool and differentiate read-only ticket-poll tools from the mutating merge
  tool.
- Added `recover_auto_merge` direct-repo tool: recovers a PR that has bounced
  from auto-merge by calling GitHub's update-branch API to rebase the head
  branch, without requiring the owning ticket to be in BLOCKED state.  Designed
  for green, review-approved PRs stalled behind the base branch.
- Autonomous prompt: add "Stall guard response" guidance so the agent proactively suggests re-scoping or splitting when a periodic monitor auto-stops after consecutive no-change cycles. This reduces operator cognitive load when a monitored ticket stalls.
- Strengthen the implement agent's `no_change_needed=true` gate: before
  declaring a ticket already satisfied, the agent must now independently
  verify every acceptance criterion against the files on disk — an empty
  `git diff` alone is no longer sufficient evidence.
- Extract shared SFTP connection context manager (`_sftp_connection`) in `SftpClient`, eliminating duplicated
  connection-lifecycle boilerplate across `read_file`, `write_file`, `list_directory`, and `file_exists`.
- Lower ``subsessions.max_idle_runs`` default from 5 to 3 so periodic monitors auto-pause sooner (before auto-stop), and improve the auto-pause summary text to include what to do next.
- Extract shared `_retry_with_kuzu_heal` helper in `CogneeMemory`, deduplicating the 13-line retry-with-self-heal pattern from `_recall_core` and `_remember_core`.
- Add `merge_pr` tool to `DirectRepoClient` and the direct-repo agent tools,
  allowing the agent to merge PRs via the GitHub API (PUT merge endpoint).
  The tool is gated on the same BLOCKED-ticket precondition as other
  direct-repo tools and supports merge, squash, and rebase methods.
- Document `PublicFetchSettings` in `docs/configuration.md` — a new `### Public Fetch` section under Settings reference covering all 7 JSON keys: `enabled`, `timeout`, `max_body_bytes`, `max_redirects`, `domain_allowlist`, `rate_limit_requests`, and `rate_limit_window_seconds`.
- Extract shared `_format_entries` helper from duplicated entry-formatting
  loops in `list_knowledge_notes` and `search_knowledge_notes`.
- Wire `load_render_url_skill()` into the agent instruction pipeline so the
  render_url skill markdown is injected alongside other component skills.
  Previously `skill.md` existed but was never loaded — the LLM had no
  guidance on when or how to use the render_url tool.
- Improved subsession outcome formatting: added FILTERING RULE to reaction prompt templates instructing the agent to strip internal technical details (block IDs, event numbers, state machine transitions, spawn counters) from subsession outcomes before presenting to the user. Added user-facing summary formatting guidance to periodic monitor prompts and the `complete_subsession` tool docstring. (mill: Extract shared _format_entries helper from duplicated formatting loop in list_knowledge_notes and search_knowledge_notes (20260729T165917Z-extract-shared-format-entries-helper-fro-3daa))
- Document `### SFTP` settings section in `docs/configuration.md` covering all 9 fields: `enabled`, `host`, `port`, `username`, `password`, `private_key`, `private_key_passphrase`, `known_hosts`, `remote_root`.
- Dockerfile: fix hadolint warnings — use numeric UID for USER (DL3066) and
  consolidate HEALTHCHECK CMD onto a single line (DL3025)
- System prompt v74: add ticket ID fidelity rule — always use exact board-issued ticket IDs in API calls, never abbreviate or reconstruct from narrative memory.  Added 404 warning logs in ``ticket_poll`` and ``worker_mill`` to flag narrative-derived ticket IDs that fail to match on the board.
- **Monitor auto-resume on PR merge**: The background watcher now polls GitHub for PR merge status in addition to polling the mill for ticket state changes. When a paused periodic monitor's checkpoint records a tracked PR (`pr_number` + `repo_full_name`), the watcher checks whether that PR has been merged and auto-resumes the monitor. This catches merges that the board ticket API may not immediately reflect. Also fixed a gap where monitors closed with `pre_authorized_approval` were never eligible for auto-resume.
- `direct_fix` and `patch_direct_repo_file` now use the component-request roster path for implement-cycle counting when `component_request` is available, matching the fallback already used for ticket-state verification.  Fixes failures where the direct board API was unreachable but the roster-based path worked.
- Operator consent propagation: the agent now carries operator authorization forward through subsequent approval gates — when an operator provides credentials, explicitly approves a change, or authorizes a specific operation, the agent treats that consent as covering all sub-operations (ticket approval, MR approval, merge) without re-asking, reducing redundant approval latency.
- Fix hadolint warnings in Dockerfile: suppress DL3066 on ``uv pip install``
  (versions are pinned via ``uv.lock``) and convert HEALTHCHECK CMD to exec
  form (DL3025).
- Updated `ticket_poll` skill docs: now correctly describe roster-first routing
  (`component_request` preferred, direct board API as fallback) instead of the
  outdated "bypass the component roster" / "no roster dependency" claims. (mill: Direct-path tools `ticket_poll`/`ticket_poll_batch` target wrong host (127.0.0.1:8077 instead of mill host) (20260730T130836Z-direct-path-tools-ticket-poll-ticket-pol-ca68))
- **ticket_poll:** `ticket_poll` and `ticket_poll_batch` now route through the
  component roster (`component_request`) when available, falling back to the
  direct `board_api_base_url` only when the roster is unavailable. This fixes
  the tools targeting unreachable `127.0.0.1:8077` in production deployments
  where the mill is only reachable through the roster. (mill: Direct-path tools `ticket_poll`/`ticket_poll_batch` target wrong host (127.0.0.1:8077 instead of mill host) (20260730T130836Z-direct-path-tools-ticket-poll-ticket-pol-ca68))
- Clarify in the agent instruction that the ticket fingerprint guard hashes only the spec text — editing the description without changing the spec will not clear the guard. This prevents agents from wasting cycles trying to bypass the guard by appending to the ticket description.
- Added `push_patch_to_pr_branch` tool: push a unified-diff commit to an existing PR's head branch, with BLOCKED-state, scope, and same-repo authorization checks. Removes the need to create a new branch for every PR update.
- Bumped `robotsix-llmio` (v0.1.1 → v0.1.4) and `robotsix-http` (v0.1.dev16 → v0.1.dev38) pinned git revisions to pick up upstream routing fixes for the OpenRouter 402 fallback issue (robotsix-llmio #499/#500, robotsix-http #45).
- Add confirmation-gated merge tool (`merge_direct_repo_pr`) and auto-merge tool
  (`arm_direct_repo_auto_merge`) to the chat agent's direct-repo toolset.
  Both tools require explicit operator approval in-chat before proceeding,
  enforce installation scope, and produce actionable diagnostics when the
  merge is blocked (draft, conflicts, CI not green, etc.).
- Strengthen subsession consolidation prompt: the CONSOLIDATION RULE is now the
  primary instruction (applied first, before per-outcome handling) in the reaction
  prompt template, using mandatory MUST language and explicit conversation-history
  scanning. The active-plan template receives the same treatment. This prevents the
  assistant from listing individual subsession outcomes when multiple complete in
  the same compaction cycle.)
- Add `POST /config/import` endpoint and startup bootstrap for one-time config import from central-deploy's export endpoint, gated behind `lifecycle.config_import_enabled`. Components can now self-own their runtime configuration: import once from the deploy plane, then manage all settings through the local `GET/PUT /config` API without any central-deploy dependency for their own config.
- Removed stale `github_security.timeout` and `github_actions.timeout` doc entries from `docs/configuration.md` (fields were removed from the models in a prior PR and silently stripped by legacy validators). (mill: Remove stale timeout doc entries from GitHub Security and GitHub Actions sections in docs/configuration.md (20260730T074059Z-remove-stale-timeout-doc-entries-from-gi-35fa))
- Document `langfuse_inspect` settings (`enabled`, `max_traces`) in configuration reference.
- Extract ``_ok_or_error`` helper in GitHub routes to eliminate four repeated error-handling postludes
  in ``github_settings_endpoint``, ``github_repo_create_endpoint``,
  ``github_actions_secret_endpoint``, and ``github_actions_workflow_endpoint``.
- Add OpenSSF Scorecard workflow (`.github/workflows/scorecard.yml`) — weekly analysis publishing results to the repo's Security tab and uploading SARIF to Code Scanning.
- Docker digest resolution: add ``registry_host`` / ``auth_url`` settings fields so operators can point at registry mirrors; detect OCI image indexes as multi-arch manifests; short-circuit ``@sha256:`` refs without a network call.
- Extend auto-resume criteria to include fingerprint-guarded tickets where a working fix already exists despite an unchanged spec fingerprint (e.g. a PR with passing tests blocked on spec fingerprint). The assistant can now call resume-blocked with justification for this case without operator authorization.
- `fetch_public_url` now supports fleet-auth Basic Auth credential injection for operator-configured hosts. Hosts listed in `public_fetch.fleet_auth.auth_hosts` receive a server-side `Authorization` header (never exposed to the agent) and bypass the SSRF and domain-allowlist checks, allowing the agent to inspect authenticated fleet UIs directly.
- Re-export `SftpSettings` from `robotsix_chat.config` package, following the existing convention where every settings model is importable from `robotsix_chat.config`.
- Extract `_parse_json_body` helper in `github.py` routes, removing duplicated JSON body parsing logic from `_github_endpoint` and `github_repo_create_endpoint`
- Extract shared `_handle_terminal_on_resume` helper in `resume.py` to eliminate ~39 lines of duplicate terminal-check-and-close logic across the three `_resume_*_entry` functions.
- Fixed `_resolve_path` path-containment bug that rejected all paths when `remote_root` was absolute (e.g. `/var/www`). The normalisation step incorrectly stripped the leading `/`, causing the subsequent containment check to always fail.
- Added `mdformat` (with `mdformat-gfm` and `mdformat-frontmatter` plugins) to the dev dependency group so `uv run mdformat` works in the sandbox. The implement agent now runs mdformat on changed `.md` files as a pre-flight check, eliminating wasteful CI auto-fix cycles on non-compliant markdown.
- Session carryover: when a chat session ends (close, delete, or idle compaction), an action-plan summary is automatically saved to the knowledge store and injected into the next new session so the assistant can pick up pending work across session boundaries.
- Fix `DirectRepoClient.apply_patch` negative-index bug when a hunk starts at line 0 (`@@ -0,0 +1,N @@`): `orig_pos` is now clamped to 0 instead of wrapping via Python negative indexing.
- Strengthen the ticket-filing dedup guard in the agent system prompt: always query the board's ticket list first (by board, keywords, or error message) before filing, explicitly noting that the CI system and periodic agents may have already auto-filed a ticket for the same issue.
- Agent system prompt v70: require concrete, copy-paste-ready operator instructions when surfacing server-side blockers. The assistant must now include exact env variable names, config file paths, restart commands, or endpoint URLs rather than vague diagnoses like "flip the toggle." Common remediation recipes should be stored in a knowledge note (`operator-remediation-recipes`).
- Add programmatic CI workflow verification gate to `complete_subsession`
  for periodic ticket monitors — the tool now rejects summaries that lack
  CI workflow evidence when the monitor's checkpoint has a `ticket_id`,
  breaking the redraft loop where monitors reported success despite a
  still-failing publish pipeline. The periodic system prompt supplement
  is also strengthened to document the programmatic gate and instruct the
  agent to auto-file a diagnostic ticket on CI failure.
- Fleet-auth hosts in `http_probe` are now implicitly allowlisted — the operator no longer needs to duplicate fleet hostnames in both `allowlist` and `fleet_auth.auth_hosts`.  The `http_probe` skill document has been updated to accurately describe the fleet-auth capability, and a new `render_url` skill document has been created so the agent knows it can render authenticated fleet UIs.
- Added documentation for the central-deploy `allow_chat_access` per-repo
  toggle in the deployment guide and lifecycle skill, explaining how operators
  enable chat-agent mutation endpoints (service registration, restart,
  config-write) via the central-deploy dashboard.
- Add deadlocked ticket closure guidance to the agent system prompt (v69). When a ticket is deadlocked and normal close transitions are rejected by the mill API, the agent surfaces the deadlock to the operator and uses `DELETE /tickets/{id}` (via `component_request`) as a last resort with operator approval. A superseding ticket may be filed if the issue still needs attention.
- `reset_implement_spawn_counter` tool now routes its DELETE
  request through the roster-based `component_request` proxy
  when available, matching the connectivity used by other
  direct-repo tools for ticket-state verification.  When
  `component_request` is unavailable the tool falls back to
  the configurable `board_api_base_url`.  This prevents the
  tool from failing when the mill host is not reachable at
  the default `127.0.0.1:8077` address.
- Stalemate detection in autonomous chat now tells the agent to close the session (emit the completion marker) when the user repeatedly sends continuation messages while the session is in executing state, rather than suggesting plan-level alternatives that only apply in proposal state.
- Add ``monitor_9d6a`` periodic agent to watch the implement-stage pre-LLM abort fix ticket (blocked at spawn limit) and alert when it unblocks.
- Added `ticket_poll_batch` tool for bulk read-only ticket triage. Fetches full ticket data
  (state, history, events, comments) for multiple tickets concurrently via `GET /tickets/{id}`,
  enabling failure-mode classification without N sequential round-trips.
- Include the full conversation transcript alongside the agent-written summary when
  delivering closed user_chat subsession outcomes to the parent session.  This ensures
  the main assistant can act on operator decisions even when the ``complete_subsession``
  summary is terse (e.g. ``"Decisions recorded"``).
- Subsessions: require the assistant to synthesize multiple subsession outcomes into a single cohesive narrative paragraph, never outputting raw `[id] kind=...` bullet-list enumerations. Trivial no-change monitors are now explicitly omitted from reporting.
- Added ``patch_direct_repo_file`` tool to the direct-repo tool set
  (gated on ``direct_fix_enabled``).  Accepts a file path and a unified
  diff, fetches the current file content from the target branch, applies
  the patch, and pushes the result as a commit — enabling targeted edits
  on large files without full-file reconstruction.
- Invert connectivity priority for `ticket_poll` and `reset_implement_spawn_counter`: try the component roster path (resolving `"mill"` via central-deploy or component fallbacks) first, then fall back to the direct `board_api_base_url` path. This prevents connection failures when the direct URL is misconfigured (e.g. `127.0.0.1:8077` from the chat container) while the roster path correctly resolves to the mill service host. (mill: Chat-agent direct-path tools (ticket_poll, reset_implement_spawn_counter) target unreachable 127.0.0.1:8077 instead of mill host (20260728T204530Z-chat-agent-direct-path-tools-ticket-poll-aaff))
- Add mutation-authorization guard to the autonomous session prompt: the agent must now verify operator authorization before performing state-changing actions (resume-blocked, merge-now, ingest, config/deploy/restart). When told to perform read-only work, the agent is instructed to hold all mutations and output a blocking message until explicitly authorized.
- Autonomous sessions: periodic monitors no longer deadlock session completion; NO_CHANGE / idle auto-continue replies are suppressed from the browser; a new `max_idle_auto_turns` config (default 5) halts the loop after N consecutive idle turns instead of spinning to `max_auto_turns`.
- Added `step-security/harden-runner` egress monitor to `docs.yml`, `release.yml`, `release-image.yml`, and `dependency-review.yml` workflows, matching the pattern already used in `ci.yml`.
- Refactor `github_create_repo_endpoint` and `github_job_log_endpoint` to use a shared `_check_settings_and_auth` helper, eliminating duplicated 503/403 preamble code. Add installation-scope check to `github_create_repo_endpoint` so repos cannot be created under an org outside the GitHub App installation scope.
- Fix backward-incompatible config loading crash: `GitHubSecuritySettings` and `GitHubActionsSettings` now accept the legacy `timeout` key (stripped before validation) so existing deployed configs do not fail on startup when the field was removed from the model.
- Add missing `model_config = ConfigDict(extra="forbid")` to `GitHubActionsSettings` so it rejects unknown JSON keys like every other config model.
- Remove dead ``timeout`` fields from ``GitHubActionsSettings`` and
  ``GitHubSecuritySettings`` — neither field was ever read by any code
  path (``DirectRepoSettings.timeout`` is the active timeout).  Removed
  the fields from the pydantic model, config template, JSON Schema, and
  class docstrings.
- Extracted shared `_request` helper method in `LifecycleClient`, replacing duplicated boilerplate across `_get`, `_get_raw`, `_post`, and `_put`.
- Extract `_list_subsessions` helper from duplicate registry-check boilerplate in `autonomous/runner.py`.
- System prompt: added guidance to compress monitor outcomes to delta-only when the user was recently told about a ticket's state, suppressing stale IDs, timestamps, PR URLs, and lifecycle chains.
- Add block cascade triage instruction to the system prompt: when a periodic monitor reports a stabilized cascade (≥10 blocked tickets across ≥2 boards, no change for ≥3 runs), the assistant must not bulk-resume and instead present a categorized failure-mode summary grouped by root cause, with severity labels, and ask the operator to choose between per-board triage or individual-ticket focus. (prompt v65)
- Add bulk-resume failure-mode classification heuristic to the system prompt (v65). Before bulk-resuming blocked tickets, the assistant must now query each ticket's history to infer failure categories and abort if >2 distinct modes are detected, surfacing a categorized diagnosis instead.
- Exclude `CHANGELOG.md` from `markdownlint-cli2` in pre-commit hooks via `.markdownlint-cli2.jsonc` `ignores`, preventing the auto-generated changelog from being reformatted and causing spurious pre-commit failures on main.
- Fix markdownlint MD012 (multiple consecutive blank lines) in CHANGELOG.md that caused pre-commit hooks to fail on the release commit, blocking the release-image workflow.
- Deduplicate installation scope check in `github_job_log_endpoint` by delegating to `_check_installation_scope()` instead of inlining the identical logic (~15 lines removed).
- Removed `[tool.uv] exclude-newer = "7 days"` from `pyproject.toml` — the relative-date format is incompatible with uv ≥0.8.15, which requires an absolute ISO-8601 date. The `uv.lock` already carries the equivalent constraint (`exclude-newer-span = "P7D"`) in a valid format.
- Strengthen memory-recall guardrails against stale plans and options from past sessions: the system prompt's Autonomy section now explicitly warns about stale plans, solution options, and decisions (not just identifiers); the per-turn memory header gains a "CRITICAL — stale plans and decisions" block instructing the model to verify recalled plans/options against the current conversation before presenting them.
- Fix: `AutonomousRunner.create_session` no longer logs a warning for idempotent re-creation of an already-tracked session (same `session_id`).
- System prompt v59: Periodic subsessions now track deploy status (image digest, rollout, health) alongside board status when monitoring code-change tickets, preventing redundant fix proposals for already-deployed changes.
- System prompt v59: Add mandatory live-deploy-state pre-check to the hand-authoring PR escape hatch. Before proposing any mill-targeting fix, the assistant must verify the live mill deploy state (running image digest, commit, recently merged PRs) to confirm the defect hasn't already been fixed — preventing wasted effort on outdated assumptions.
- Add "Hand-authoring PRs as a mill-failure escape hatch" guidance to the system prompt (v58). Defines qualifying criteria (≥5 tickets across ≥2 repos blocked by the same mill defect), mandatory pre-checks (no existing PR, unique branch name, minimal scope), and a structured escalation path with an explicit expiry-and-move-on rule when the operator does not respond.
- Added `POST /chat/github/repos` endpoint to create GitHub repositories under the configured organisation. Repos are created with `auto_init: true` by default so they have an initial commit and are immediately cloneable.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions. (mill: Unify periodic sub-session summaries into one consolidated monitor view (20260725T010746Z-unify-periodic-sub-session-summaries-int-6dc6))
- Added ``reset_implement_spawn_counter`` tool to the direct-repo capability, allowing the chat agent to delete the ``implement_spawn_count`` board artifact and unblock tickets stuck at the implement spawn limit.  Includes ``delete_ticket_artifact`` board API method in ``DirectRepoClient``.
- Add `langfuse_inspect` tool: the agent can now fetch and summarise Langfuse traces by ticket or trace id via `inspect_langfuse_trace(trace_id=..., ticket_id=...)`. Gated behind `langfuse_inspect.enabled` (default `false`); reuses existing `langfuse` credentials for API auth. This lets the assistant self-diagnose implement-stage failures without human-in-the-loop trace inspection.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Fix remaining `robotsix` org URLs in SECURITY.md and CONTRIBUTING.md, correcting to `damien-robotsix` (follow-up to PR #936).
- Fixed wrong GitHub org (`robotsix` → `damien-robotsix`) and outdated Python version (`3.12` → `3.14`) in SECURITY.md and CONTRIBUTING.md.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Honour ``paused_monitor_poll_interval_seconds=0`` semantic: the watcher now exits early (logging "polling disabled") instead of clamping 0 to 60s, so paused monitors only resume on service restart as documented.
- Added `scripts/resolve_github_sha.py` — resolves a GitHub `owner/repo` + ref to a 40-char commit SHA via `git ls-remote`. Callable as `uv run scripts/resolve_github_sha.py <owner/repo> <ref>`.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Switch `_export_langfuse_env` from `setdefault` to direct assignment so config.json values always win over stale deploy-plane env vars (config-ownership Rule 1).
- Extend subsession ticket-state pre-check from PERIODIC-only to all subsession kinds (TASK, USER_CHAT). A subsession with a `ticket_id` in its checkpoint now verifies the ticket is still in a non-terminal state before proceeding — if the ticket was closed during an outage the subsession aborts immediately with a clear explanation instead of silently doing nothing on a stale branch.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Implement resume mechanism for paused periodic monitors: a background
  watcher (`watch_paused_monitors`) polls paused monitors' ticket states
  via the mill API and reopens + re-spawns them when the state changes.
  Adds `SubsessionRegistry.reopen()` method and
  `paused_monitor_poll_interval_seconds` config key (default 60 s).
- New ``public_fetch`` tool: ``fetch_public_url`` fetches raw content from any public URL with SSRF protection (DNS-level IP filtering on every redirect hop), size limits, and no authentication. Designed for reading files from public forges (GitLab, Bitbucket, codeberg, etc.) not covered by the GitHub-scoped repo-study tools. Disabled by default — set ``public_fetch.enabled`` to ``true`` to activate. (mill: Implement tool to fetch public repo content from non-GitHub forges (20260725T112315Z-implement-tool-to-fetch-public-repo-cont-5a5f))
- Added SFTP config-restore capability (`sftp_read_file`, `sftp_write_file` (confirmation-gated), `sftp_list_directory`, `sftp_file_exists`) behind the ``sftp`` feature-flag in config.  Write operations require explicit operator approval in-chat and cannot be auto-approved.  Credentials are sourced from the config file via ``SecretStr`` fields to avoid plaintext secrets in conversation history.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- System prompt: teach the agent to pass a ``justification`` parameter to
  ``resume-blocked`` when re-implementing a fingerprint-guarded ticket where
  a pending question has been answered or a prerequisite has been resolved.
  The ``justification`` field bypasses the fingerprint guard that otherwise
  blocks re-implementation when the spec is unchanged.
- Migrate hand-rolled HTTP retry and backoff to shared `robotsix-http` library: delegate mill-recovery exponential-backoff math to `RetryConfig`, wrap roster fetch and ticket-poll requests in `RetryClient` for transient-error resilience.
- Fix background event stream for queued-message draining on session switch: replace stream-preservation hack with dedicated ``openBackgroundEventStream()`` that has its own generation counter, watchdog, and reconnect logic — the old approach reused the foreground stream's stale callbacks which dropped every frame. Also fix a ``var``-in-loop closure bug in ``drainBackgroundSession`` that caused only the last queued message to be posted.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Queued messages on a session are now drained when that session's turn
  completes, even if the user has switched focus to another session.
  Returning to a session with stale queued messages also re-triggers
  pickup defensively.
- Run mdformat on changed .md files in the document stage before
  committing, so doc commits land with canonical formatting and avoid
  the CI auto-fix round-trip.
- Add a "Conflict Resolution" instruction to the system prompt: when a user gives an instruction that conflicts with an existing pending ticket, the assistant now automatically attempts to merge the instruction into the ticket rather than just flagging the conflict and waiting for manual intervention.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Enable the `notify_user` browser notification tool by default (`notification.enabled: true` in the config template).  The agent can now proactively push alerts to connected browsers when background tasks complete, escalate, or need user attention.)
- Ticket lifecycle policy (system prompt v56): monitors now require live endpoint verification before closing. When a ticket reaches done/closed, the monitor probes the relevant endpoint with `component_request` and only closes after confirming the change is live. Ticket specs must include acceptance criteria that verify the change works (e.g., "endpoint returns 2xx"), not just "PR merged".
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- System prompt v56: add "Ambiguous field references" verification rule — when a user describes a desired change to a form field or UI element, confirm the specific field(s) before filing a ticket rather than assuming which field they mean.
- Add empty-repo guard to the implement agent's ``no_change_needed`` logic: an empty
  repository (no source files beyond ``.git/`` metadata) must never be short-circuited
  as "already satisfied".  The implement agent now checks for non-trivial file presence
  (a README, ``src/`` directory, or similar) before declaring ``no_change_needed=true``.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Add `regenerate-config-schema` pre-commit hook that auto-regenerates
  `config/config.schema.json` when `src/robotsix_chat/config/settings.py` changes.
- Add `regenerate-config-schema` pre-commit hook that auto-regenerates `config/config.schema.json` whenever `src/robotsix_chat/config/settings.py` changes; the hook exits non-zero so the regenerated schema is staged and the commit must be retried.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Extract shared ``_build_github_app_auth_headers`` helper in ``robotsix_chat.common.github_auth``, eliminating duplicate GitHub App token-minting logic from ``refdocs``, ``version_check``, ``repo/direct/client``, and ``repo/study/workspace`` HTTP clients.  The helper accepts an optional ``token_cache`` dict for callers that need per-installation-id caching.
- Add `check-activity-kinds` Makefile target (`python scripts/check_activity_kinds.py`).
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Prompt: add deterministic recovery guidance for periodic subsession spawn failures — fall back to inline monitoring immediately instead of presenting options to the user.
- Added `human_approval_timeout_seconds` (default 300 s / 5 min) — a wall-clock backstop for the `human_issue_approval` stuck-ticket gate. When the checkpoint has carried `last_known_state='human_issue_approval'` for longer than the timeout, the system auto-escalates even if the NO_CHANGE run count has not reached `human_approval_timeout_runs`.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Strengthen memory-recall prompt header with explicit stale-identifier guidance: ticket IDs, task IDs, subsession IDs, and other structured identifiers in recalled text are from past sessions and must be verified against the current conversation before being presented as current work.
- Add "batch closely related issues" guidance to the implement agent prompt: when discovering multiple incremental issues on the same page/file/component in a single session, combine them into one ticket instead of filing one per issue.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Added self-hosted runner guidance to implement agent prompt: prefer self-hosted runners over workflow deletion when CI fails due to runner-minute exhaustion in private repos; distinguish runner minutes from paid/licensed CI as separate concerns.
- Autonomous sessions now detect repeated identical user prompts (3+ consecutive occurrences) and respond with a stalemate notice that prompts the agent to acknowledge the stalling pattern and suggest alternative interaction modes or offer to abort, instead of cycling through the same plan→proposal loop.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Suppress duplicate restart notices when background-task state is unchanged — the
  dedup check now scans the full conversation history instead of only the last turn,
  so intervening user messages no longer defeat suppression.
- Add pre-commit hook to auto-regenerate `config/config.schema.json` whenever
  `src/robotsix_chat/config/settings.py` is modified.  The hook stages the
  updated schema and fails the commit so the regeneration is included on retry,
  eliminating the recurring CI `check-config-schema` drift auto-fix cycle.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Add `fetch_workflow_run_annotations` agent tool: fetches GitHub Actions CI annotations (linter warnings, test failures, compiler errors) via the Check Runs API. The tool takes a repo name and workflow run ID and returns annotations grouped by check run with file paths, line ranges, and full message text. Uses the existing GitHub App installation for authentication; no new config keys required.
- Added `central_deploy.component_fallbacks` config — a baked-in map of component_id → base_url that supplements the central-deploy roster when components go missing (e.g. after a redeploy). Monitors and tool calls now survive transient roster gaps without operator intervention. Unknown-component errors include a precise remediation step telling the operator which config key to set. The roster is logged at startup so operators can verify which components are registered.)
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- System prompt v54: add rule requiring the agent to verify source code before advising on component configuration (e.g. central-deploy's `docker_sdk.py` may inject secrets fleet-wide, making per-repo advice incorrect).
- Remove dead `.robotsix-mill/periodic/state_sync.yaml` and `security_posture.yaml` name-only files that were silently rejected by the loader.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Autonomous chat sessions now use conversational approval/rejection instead of UI buttons: the agent proposes a plan directly in the chat, and the operator approves or rejects by writing in natural language (e.g. "approved", "reject"). The "Awaiting review" state label is replaced with "Plan ready — reply to approve".
- Add `check_workflow_run` agent tool for diagnosing CI failures, including private-repo billing-failure detection (runs with zero jobs or that never started).
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Subsessions: reaction turns are now plan-aware when the main session has an active
  autonomous plan. When a subsession completes while the session is in proposal or
  executing state, the reaction prompt reminds the agent to acknowledge the outcome
  as a note and stay on its plan — preventing the agent from dropping approved work
  and re-requesting approval.
- System prompt (v53): added troubleshooting instruction to fetch live system state (deploy contract, service registry, logs) before hypothesizing causes for user-reported errors, preventing fabricated guesses that waste back-and-forth.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- **Periodic monitor `consecutive_no_change` counter now persists across server restarts**, preventing the auto-stop and auto-pause thresholds from being defeated by process restarts. Added ``consecutive_no_change`` field to ``SubsessionInfo`` (persisted in the subsession store and restored on resume).
- **Lowered ``auto_stop_no_change_runs`` default from 5 to 3** so periodic monitors auto-terminate sooner when the watched ticket shows no changes.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Suppress duplicate terminal-state reports from ticket monitors.  When two
  periodic monitors track the same ticket and both detect a closed/done
  state, only the first delivers a completion notice to the parent
  conversation; the second is suppressed to avoid a redundant (and often
  verbose) assistant reaction turn.
- Added `subsessions.pre_authorized_ticket_patterns` config key — a
  list of glob patterns matching ticket IDs that are pre-authorized
  under a standing operator directive.  When a monitored ticket matches
  a pattern and enters `human_issue_approval`, the system auto-escalates
  immediately instead of waiting for the configured timeout.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Suppress duplicate consecutive restart notices: when the chat service restarts with no change in background-task state, the system notice is now skipped instead of being written again — eliminates noise from repeated identical restart notices.
- Fix Trivy scanning in release-image.yml: tag locally-loaded image with a non-registry-qualified tag (`local/robotsix-chat:scan`) so Trivy resolves via the Docker daemon instead of pulling from GHCR, eliminating `/tmp` exhaustion in CI
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Add mandatory pre-planning step to load live knowledge notes and board state before drafting plans; strengthen warnings that recalled session memories may be stale or contain phantom identifiers
- Redesign autonomous session lifecycle: remove Approve/Reject gate flags.
  Session spawns in ``planning`` state, transitions to ``proposal`` after the
  agent drafts a plan, and waits for the operator to comment before executing.
  After execution the session stays open until the operator explicitly closes
  it — no more auto-close/respawn. The ``auto_approve`` config flag and
  ``/sessions/{id}/approve`` / ``/sessions/{id}/reject`` endpoints are removed.
  Config key ``approval_marker`` renamed to ``proposal_marker``.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Autonomous sessions now gate completion on active subsessions: the `---AUTONOMOUS COMPLETE---` marker is suppressed when the session still owns any running subsession (including periodic monitors), preventing premature session closure that would lock the agent out of spawning tracking monitors.
- Persist rejected autonomous session subjects. When an operator rejects
  a proposed subject, the plan text is recorded in ``rejected_subjects``
  on the session, persisted to ``autonomous_sessions.json``, and
  injected into the next subject-selection prompt so the agent is
  instructed not to re-propose the same subject.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Suppress AUTONOMOUS COMPLETE marker when non-periodic subsessions are still
  active, preventing premature session closure that would block spawning new
  lifecycle-tracker subsessions.
- System prompt v51: add guardrails preventing self-authored behavioral rules in knowledge notes. Knowledge notes now explicitly limited to operational facts/findings; behavioral restrictions like "never use X" belong in the system prompt. Added verification bullet instructing the agent to trust the system prompt over contradicting knowledge-note rules.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Add missing `log_json_format` and `compaction_min_turns` fields to `config/config.json` defaults template so operators can discover them.
- Autonomous sessions no longer wait for periodic monitors when deciding
  whether to continue — only task and user_chat subsessions block the
  auto-continue loop.  A new config field
  `autonomous.stale_monitor_runs_before_completion` (default 3) controls
  how many consecutive NO_CHANGE cycles the agent should observe before
  it may declare the session complete while leaving the monitors running
  in the background.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- `self_restart` failures now return a structured diagnostic report with
  plain-language explanation and actionable next steps (e.g. "check
  lifecycle.base_url", "verify the API key"), replacing raw HTTP error
  strings.  The retry + backoff mechanism continues to handle transient
  errors; non-retryable failures and exhausted retries both receive the
  new diagnostic format.
- Document five previously-undocumented config groups in `docs/configuration.md`: GitHub Security, GitHub Actions, Notification, HTTP Probe, and Autonomous. Add `log_json_format` to the Server table.
- Fix `render_url` returning `AttributeError: 'Page' object has no attribute 'accessibility'` — migrated from the removed `page.accessibility.snapshot()` API to the ARIA snapshot API (`page.locator("body").aria_snapshot()`), which returns a YAML-like string instead of a nested dict.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Periodic subsessions are now closed immediately at startup when
  ``central_deploy.url`` is not configured, preventing futile retries
  and child-task churn.  The missing tool is logged as a diagnostic
  warning.
- Add ``GET /admin/disk`` and ``POST /admin/prune`` endpoints for disk-full
  resilience.  ``/admin/disk`` reports free/total/used bytes on the data
  volume using stdlib ``shutil.disk_usage`` (no disk writes, survives a full
  disk).  ``/admin/prune`` triggers available cleanup methods on the
  conversation store and subsession registry to free space in an emergency.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Periodic monitor deduplication: `spawn_subsession` now cross-references
  active PERIODIC subsessions' `checkpoint.ticket_id` against the new
  spawn's `dedup_key`, so a duplicate monitor for the same ticket is
  caught even when the original was spawned without a dedup_key.
- Allow push/PR operations to bypass the GitHub App installation scope
  check when the mill pipeline credential (``component_request``) is
  available.  When a request comes through the component roster the mill
  already has its own GitHub access, so the scope check is an unnecessary
  gate.  The scope check is still enforced for direct board-API calls.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- `component_request` tool: when a component returns HTTP 429 with a
  ``Retry-After`` header, wait the full cooldown window and retry once
  before returning the error to the agent. This prevents the agent from
  busy-polling long rate-limit windows (e.g. 300 s disk-reclaim
  endpoints) inside its own conversation loop.
- Clarified periodic subsession role in system prompt and turn input to suppress misleading "not supported" warning when a monitor is spawned directly from a conversation.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Re-spawn auto-closed periodic monitors on restart when the close reason
  was `no_change_auto_stop`, `paused`, or `human_approval_timeout`.  These
  monitors were previously restored as terminal (CLOSED) and never re-spawned
  after a restart, requiring manual re-creation by the operator.  The worker's
  built-in `_check_resume_status` re-verifies the ticket state on the first
  post-restart tick and closes immediately if conditions have not improved.
- Suppress intermediate periodic subsession run results from the main
  chat — only terminal summaries (complete_subsession) and escalations
  now reach the parent conversation.  The reporting contract is baked
  into both the system prompt (v49) and every periodic turn's input.
  `ParentDelivery.deliver_result` is removed; the `subsession_result`
  SSE frame still fires for UI notification bubbles.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- `render_url`: add `text_only` parameter — when `True` the full-page screenshot is skipped, producing a compact (text-only) response suitable for subsessions that lack file-slicing tools.
- Derive `__version__` from installed distribution metadata instead of
  hard-coding it, so `pyproject.toml` is the single source of truth.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Strengthen the subsession reaction prompt to suppress redundant state restatements: when a subsession reports no change (auto-stopped, auto-paused, or explicit NO_CHANGE), the parent agent now replies with a brief acknowledgment instead of re-listing the ticket ID, status, and timestamp.
- Revised system prompt to suppress internal tracking details (monitor IDs, subsession codes, pipeline job numbers) and report only key state changes with a clear call to action, unless the user explicitly requests detail.
- Add consolidation guidance to the system prompt for periodic subsession outcomes: when multiple monitors deliver outcomes in quick succession, the agent consolidates them into one grouped summary by state (NO_CHANGE, PROGRESS, GATE_PENDING) and hides trivial NO_CHANGE runs from duplicate monitor cycles.  Also updates the reaction prompt template (`_REACT_PROMPT_TEMPLATE`) with matching consolidation instructions.
- Added `ticket_poll` tool — a direct board-API fallback that lets periodic ticket monitors check state even when `component_request` is unavailable. The tool queries the mill board API directly via HTTP, bypassing the component roster. A corresponding skill document is injected into the agent instruction when the board API URL is configured.
- Add "Cognee recall retirement" guidance to the system prompt: when a monitor reports terminal state on a ticket, the agent should retire stale knowledge notes that reference obsolete PR numbers, monitor ids, or closed-fix paths, replacing them with fresh entries reflecting the current active path.
- Add missing `"ticket_unreachable"` human-readable phrase to `_REASON_PHRASES` in subsession delivery, preventing the raw snake_case code from appearing in user-facing reaction prompts.
- Added mandatory CI workflow failure diagnosis gate to the implement agent's
  system prompt.  Before editing any ``.github/workflows/*.yml`` file, the
  agent must fetch recent failure logs via the chat-allowlisted
  ``/actions/run-jobs`` and ``/actions/run-failed-logs`` endpoints, quote
  the verbatim error in the ticket, and refrain from proposing fixes for
  zero-step or parser-level rejections until the exact error text is shared.
  This prevents blind-fix loops like the deploy-ovh.yml incident (PRs #31,
  #33, #35).
- Lifecycle `self_restart` now validates the `base_url` at init time, applies a
  configurable default protocol (`default_protocol`, default `"http"`) when the
  URL is missing a scheme, and retries transient failures with exponential
  backoff (configurable via `self_restart_max_retries`, `self_restart_backoff_base`,
  `self_restart_backoff_cap`).  An empty `base_url` is logged and produces a
  clear error from `self_restart` instead of raising a protocol error.
- Periodic subsessions now retry transient API errors (e.g. OpenRouter upstream hiccups) with configurable exponential backoff instead of failing permanently. Three new settings under `subsessions` control the behaviour: `transient_error_max_retries` (default 3), `transient_error_backoff_base` (1.0 s), and `transient_error_backoff_cap` (30.0 s). When all retries are exhausted the cycle is skipped gracefully and the schedule continues.
- Background task auto-stop and failure notifications are now more robust:
  the reaction-prompt template instructs the assistant to always provide a
  substantive notification (removing the "just acknowledge it briefly"
  escape hatch), and when the LLM call itself fails a fallback
  ``agent_message`` frame is pushed directly so the user still sees the
  outcome in the chat.
- **Direct-repo tools**: Installation scope is now checked FIRST (before ticket state), providing a dedicated diagnostic step with an actionable message. The new ``check_installation_scope`` helper on ``DirectRepoClient`` is shared across direct-repo, GitHub Actions, and security tools, replacing duplicated inline checks. Out-of-scope errors now say "install the app on this repository and try again" with the current installation list.
- Strengthened periodic monitor verify-first policy: before reporting any state change or outcome, the monitor must do a live GET of the ticket and compare against previously verified state. Terminal-state claims now require a double-check via the PR API to confirm merge status before reporting.
- Strengthened the memory-recall prompt header with explicit guidance about stale action items: recalled text mentioning "pending", "awaiting confirmation", or similar unresolved-state language is often from a past conversation where the action was already completed. The LLM is now instructed to treat conversation history (not recalled memory) as the authoritative record of what is actually pending, and to label unverified recalled items explicitly.
- Refined the subsession reaction prompt to eliminate redundant "Acknowledged" responses: the assistant now jumps straight to a delta summary or a single terse sentence instead of echoing the subsession's full report.
- Subsessions: user_chat and task kinds now retry on failure (up to `user_chat_max_retries`, default 3) with the prior error folded into the retry prompt so the agent can self-correct. Retry state is persisted to survive restarts. When retries are exhausted for a user_chat subsession, the original decision prompt is surfaced in the main conversation as a fallback so the operator can answer directly. `_format_worker_error` now includes the exception type name for unrecognised errors so opaque SDK strings are never passed through verbatim.- Deduplicated `DirectRepoClient.get_ticket_state` and `get_ticket_data` by extracting a shared `_fetch_ticket_field` helper (57 duplicated lines → 2 one-liner callers).
- Add `check_autonomous_states.py` CI gate that verifies `AutonomousState` enum values stay in sync with bare-string comparisons in `chat.js`, following the same pattern as `check_subsession_kinds.py` and `check_activity_kinds.py`.
- Strengthen memory-recall prompt header: explicitly instruct the model to suppress recalled claims that the current conversation has already refuted or contradicted, preventing repeated presentation of disproven diagnoses.
- Made `AutonomousRunner._load_sessions` handle `persist_path.exists()` failures
  gracefully by moving the check inside the try/except block, preventing
  crashes when `_persist_path` is not a real filesystem path (e.g. under
  mocked test settings). (mill: ci_fix: out-of-scope CI failure — Python CI (shared) / Tests — test_chat_returns_409_when_awaiting_approval in tests/chat/server/test_autonomous_endpoints.py (autonomous_runner fixture — needs a non-existent persist_path) or src/robotsix_chat/autonomous/runner.py (_load_sessions — defensively handle non-real paths) (20260724T112927Z-ci-fix-out-of-scope-ci-failure-python-ci-340f))
- Periodic subsession monitors now include an explicit instruction to re-query the board API for canonical ticket state on every poll tick, preventing stale-state readback where the agent reports a cached `draft` state that diverged from the live board state.
- Periodic monitors now auto-pause after `max_idle_runs` consecutive
  NO_CHANGE runs (default 5), closing with reason ``"paused"`` instead of
  ``"no_change_auto_stop"``.  Set ``max_idle_runs`` to ``0`` to disable
  the pause behaviour and fall through to the existing hard auto-stop.
- Periodic subsessions can no longer spawn task child subsessions to work around
  the nesting restriction. The enforcement gate in `spawn_subsession` now blocks
  periodic→task spawns (periodic→periodic was already blocked). The system prompt
  no longer describes the prohibition as a workaround — periodic monitors poll
  directly on every cycle.
- Changed `GHSA-9xwg-3r6f-jcx2` (pymdown-extensions) advisory suppression from `--ignore` to `--ignore-until-fixed` in both the pre-commit uv-audit hook and CI lockfile job.
- Periodic monitors now include guidance to recognize decision-blocked tickets (human_issue_approval, awaiting operator choice) and recommend pausing rather than silently emitting NO_CHANGE until the auto-stop timeout.
- Subsessions that fail due to the Claude Agent SDK's degenerate-success frame
  (`is_error=True` / `subtype="success"` — a self-contradictory result that can
  persist across retries) now report a clear human-readable explanation instead of
  the raw "Claude Code returned an error result: success" text.  ProcessError
  (non-zero CLI exit) failures now include the exit code and stderr in the
  failure summary so operators can diagnose tool failures without log-diving.
- Replace hand-rolled retry loops with robotsix-http
- Config-ownership standard: `GET /config` now includes `version` and `schema` fields; `PUT /config` returns the new version and increments a monotonic counter; secrets masked as `"**********"` (was `"***"`); blank string on secret fields now also triggers preservation (was only the sentinel); validation errors now use RFC 9457 `application/problem+json`. New `GET /config/versions` and `POST /config/rollback` endpoints for append-only version history and rollback. Chat UI Settings panel updated to handle the new response shapes.
- Repo-study: add ``delete_workspace_artifact`` tool so the assistant can
  remove individual files and directories from a fetched workspace without
  dropping the entire workspace.
- Config validation now reports **all** precondition failures at once (instead of stopping at the first error), with per-precondition detail in the ``failures`` list of the 422 response. Server-side logging includes the full failure list.
- Added blocked-resume threshold detection: when a ticket monitor finds the
  ticket BLOCKED on every resume for `_MAX_BLOCKED_RESUMES` (3) consecutive
  attempts, the subsession is automatically closed with reason
  `"repeated_blocked"`.  This prevents the agent from cycling through the same
  dead-end implement→blocked→resume loop (e.g. config-standard footprint
  violations the assistant cannot fix on its own) and escalates to the operator
  via the parent delivery channel.
- test-only
- CI failure: Release image on main — `uv audit` now requires `--preview-features audit-command` on uv ≥0.12; added the flag to both the `lockfile` CI job and the `uv-audit` pre-commit hook. Also broadened the `verify-ci.js` deploy-job exclusion filter and fixed Python 2-style `except E, V:` syntax in `repo/direct/__init__.py`.
- Add "Bootstrap deadlock" guidance to the agent system prompt (v39): when a PR
  modifies the merge pipeline itself, auto-merge may be self-referential — escalate
  to the operator for a manual merge rather than looping on merge-now. Extracted
  from the stalled PR #688 (ticket 45b9); motivated by PR #2475's 14-iteration block.
- Add `SSE_NOTIFICATION_TYPE = "notification"` constant to `robotsix_chat.chat.events` and replace bare string literal in notification emitter and tests.
- Add ``POST /diagnostics/events`` and ``GET /diagnostics/events`` HTTP endpoints so external pipeline stages (e.g. robotsix-mill) can emit diagnostic events (including ``CI_FAILURE``) into the shared ``DiagnosticStore``.  Events posted via the API are immediately visible to agent tools (``list_diagnostic_events``, ``check_recurring_categories``) because the store instance is shared between the HTTP layer and the agent tool closures.
- Refactor `_inject_skills()` in `app.py`: replace five repeated skill-injection blocks with a table-driven `_skill_entries` loop, and promote the lazy `load_*_skill` imports to module-level (all target modules are lightweight).
- Fix queued messages never being dispatched after switching session focus in the UI: add a session-backgrounding drain in `switchSession()` and a focus-change drain in `restoreDraft()` so queued messages are dispatched both when leaving a session and when returning to it.
- Fixed Python 2 ``except X, Y:`` syntax in multiple files — changed to correct Python 3 ``except (X, Y):`` tuple form.  Split the 503 "not enabled" checks in the GitHub endpoints so ``github_security`` and ``direct_repo`` failures produce distinct error messages.  Removed a redundant ``follow_redirects=True`` from ``get_job_log``.  Added two missing tests (404 job-not-found and 400 blank path params) for the job-log endpoint.
- New `GET /chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs` endpoint fetches plain-text GitHub Actions job logs, following the GitHub 302 redirect server-side and returning log content as a 200 response so the agent can inspect deploy pipeline output directly.
- Settings UI: new settings panel (⚙ button in header) with config editor,
  ``GET /config`` (returns config with secrets masked), and ``PUT /config``
  (deep-merges submitted form over existing config, validates through Settings
  before persisting).  Prevents partial saves from blanking unrendered fields
  like ``memory.embedding.endpoint``, and rejects invalid configs with inline
  validation errors.
- Extract stale-worker resume handling from ``_check_resume_status`` into
  a private helper ``_check_stale_worker_resume``, reducing the function from
  ~289 to ~210 lines and max nesting depth from 7 to 5.
- Reuse a single `httpx.AsyncClient` across all tickets in `FeedbackRunner._file_tickets` instead of creating one per ticket.
- Add minimum description length guard to ``FeedbackRunner._parse_tickets``
  to filter out auto-generated tickets with trivially short (boilerplate)
  descriptions before they reach the ticket board.  The threshold is 10
  characters — below the length of any meaningful improvement ticket. (mill: Improve caretaker ticket auto-generation handling (20260720T232929Z-improve-caretaker-ticket-auto-generation-7021))
- Add ``self_restart`` tool to the lifecycle module — a privileged endpoint
  (``POST /self/restart``) that restarts the agent's own service without requiring
  the deploy server's per-repo access toggle.  The existing
  ``restart_lifecycle_service`` tool still requires the toggle for restarting
  arbitrary services.  The system prompt now references ``self_restart()`` as the
  primary self-restart path after capability upgrades.
- Decision-chat subsessions now enforce self-contained option context: the user_chat worker prepends a system note reminding the agent to restate option definitions inline on every turn, the spawn_subsession tool instructs callers to include one-line option definitions in user_chat prompts, and the base agent instruction adds a critical rule against bare option labels. No operator-facing decision turn should surface "Option B" without its definition.
- Increased font size throughout the subsession (decision-chat / side) panel to match the main conversation pane for readability. Body text and inputs now use the same 0.95rem size as main chat bubbles; labels, badges, and action buttons are scaled proportionally.
- Fix orphaned `.drain` snapshot recovery in cognee backlog drain: if a prior drain
  crashed mid-processing (after renaming the backlog but before completing the drain),
  the orphaned snapshot is now detected and replayed instead of being silently
  overwritten on the next drain cycle.
- Document the Pydantic `extra="forbid"` convention as a config-standard rule in AGENT.md
- Add ``extra="forbid"`` to all Pydantic config models (20 sub-models + top-level ``Settings``). Unknown JSON keys now raise a ``ValidationError`` instead of being silently ignored — a typo like ``"memry"`` for ``"memory"`` is caught at config load rather than causing the operator to wonder why a feature is disabled.
- Add "CI Failure on Main" triage boilerplate to `docs/triage-boilerplate.md`, with ACKNOWLEDGE decision for main-branch infrastructure failures distinct from the existing OUT-OF-SCOPE boilerplate for PR failures.
- Fix race condition in durable backlog drain that could silently drop entries
  queued by concurrent failing writes. Backlog file is now atomically renamed to
  a snapshot before processing; still-failing entries are appended (not
  overwritten) to the original path. Drain failures no longer masquerade as
  write failures, and backlog-entry failures now feed frozen-store detection.
- Fix race condition in ``_drain_backlog``: add ``_drain_lock`` to prevent
  overlapping drain calls from silently dropping backlog entries or replaying
  duplicates.  Also correct the docstring ghost reference to
  ``_check_frozen_store``.
- Cognee memory: throttle merge-insert concurrency with a configurable
  inter-write delay to prevent LanceDB worker OOM during bursts. Add
  durable JSONL backlog for exchanges that fail after retries (drained
  opportunistically on subsequent successful writes). Bound DataFusion
  memory pool via ``DATAFUSION_RUNTIME_MEMORY_LIMIT`` (default 256M).
  Detect frozen vector store: emit WARNING when consecutive write
  failures exceed ``frozen_store_alert_minutes`` (default 10 min).
- Move `lifecycle/skill.md` from `docs/` into the packaged source tree at `src/robotsix_chat/lifecycle/` so the lifecycle skill instructions reach the agent in production (previously the `docs/`-relative path resolved nowhere in the Docker image; `load_lifecycle_skill()` silently returned `""`).
  `docs/lifecycle/skill.md` is now a symlink to the canonical copy.
- Add contract-version troubleshooting guidance to the system prompt: when users encounter "missing or incorrect central-deploy-contract-version header" errors during onboarding, the assistant now provides concrete diagnostic steps (check for the header, check recent PRs, file a targeted ticket) instead of offering vague workarounds.
- Add `direct_fix` tool to the direct-repo capability: pushes a commit
  directly to a target branch as a last-resort escape hatch for blocked
  tickets that have exhausted the mill's implement cycle limit (≥3 cycles).
  Gated behind `direct_repo.direct_fix_enabled` (default `false`). Every
  invocation is audited at WARNING level.
- Remove stale `agent_instruction` field from `config/config.json` so the code default in `Settings.agent_instruction` (v35) applies automatically. The committed config file is no longer a second source of truth for the system prompt — it drifts from the code default by ~10 sections and no automated CI check validated it. Now the code default is the single source of truth; operators who need a custom prompt can still add `"agent_instruction"` to their local or deployed config.
- Lifecycle module now exposes self-service mutation tools (`restart_lifecycle_service`, `update_lifecycle_service_config`, `update_lifecycle_service_env`) alongside the existing read-only tools.  These succeed or fail based on the deploy server's per-repo access toggle — no new client-side toggle is introduced.  The system prompt now references the lifecycle tools for self-restart instead of the unreachable `component_request("central-deploy", …)` path.
- Use `VALID_MODEL_LEVELS` (derived from llmio's `TierLevel` enum) instead of a hardcoded `(1, 2, 3, 4)` tuple in subsession model-level validation, so the valid range stays in sync with llmio.
- Register the `agent_check` periodic workflow (`.robotsix-mill/periodic/agent_check.yaml`) for automated agent/tool integrity checks.
- Add pyright type checker to pre-commit and CI, alongside the existing mypy
  `--strict` check.  The baseline config (`[tool.pyright]` in `pyproject.toml`)
  uses `typeCheckingMode = "basic"` with the most valuable diagnostics enabled
  as warnings for gradual adoption; only `reportMatchNotExhaustive` and
  `reportUnnecessaryContains` are errors.

- release-image: Fix "Verify CI is green" self-exclusion timeout by adding name-based fallback when `getWorkflowRun` fails to return a check-suite id (#TBD)
- Add a "Deploy system" bullet to the Autonomy section of the system prompt clarifying that the robotsix-deploy (central-deploy) management plane is a runtime API server — component onboarding, lifecycle operations, and configuration changes are all API-driven (POST /onboard/preflight, /onboard/confirm, etc.) with no git PRs needed.
- Add batch-MR-approval guidance to the agent system prompt: when multiple MRs are pending human approval, the agent must first categorize them by relevance to active tickets, present a compact filter prompt, and approve the selected group in bulk through the mill's merge endpoint. (#TBD)
- Periodic subsession auto-stop (``no_change_auto_stop`` and ``human_approval_timeout``) now logs a ``WARNING``-level message so operators can see when a monitor ceases watching and decide whether to restart it.
- System prompt v34: Explicitly classify merge/rebase conflicts as never-auto-retryable substantive blockers in the Remediate step. The assistant now surfaces a clear "human must rebase manually" message via user_chat instead of looping on resume-blocked. Worker blocked-resume context also warns about merge conflicts.
- Show a relative timestamp ("2m ago") at the bottom of each chat session for the last model-generated message, with the absolute server time on hover.
- Add `workflow_dispatch` trigger to `release.yml` for manual recovery deploys.
- Added `workflow_dispatch` trigger to `.github/workflows/docs.yml` to allow manual deploy of docs from the Actions UI.
- Fix stale comment on `_active_dedup_keys` in `Registry` — remove `user_chat` qualifier and `or periodic monitors`, matching the dedup scope after the kind guard removal in PR #662.
- Prompt: when multiple unowned, actionable items exist, the assistant now immediately offers a high-signal scoped confirmation prompt listing each item (e.g. 'Say: merge 5f1c, merge 2a97, rebase 54ea.') instead of asking an open-ended 'Which do you mean?'
- Add dedicated "Mill & Deploy Endpoints" section to the agent system prompt (v31),
  listing all key mill and deploy endpoints with paths, methods, and descriptions
  so the agent can reliably reference available endpoints without trial-and-error
  discovery.
- Added defense-in-depth dedup guard in ``SubsessionRegistry.create()``: raises ``SubsessionDedupError`` when a ``dedup_key`` is already active, preventing duplicate monitors even if the ``spawn_subsession`` pre-check is bypassed.
- Feedback runner: record OTel span error status (`StatusCode.ERROR`) and
  exception details on each ingest POST span when filing fails (non-2xx or
  HTTP exception).  The trace root span now carries `feedback.failed_tickets`
  alongside the existing `feedback.filed_tickets` and `feedback.total_tickets`,
  making filing failures immediately visible in Langfuse traces.
- Fix: periodic subsessions (ticket monitors) now correctly restore their `dedup_key` after server restart, preventing duplicate monitors from spawning for the same ticket.
- Document dynamic feedback target-repo resolution in `docs/configuration.md`: allowed repos are derived from the deploy roster intersected with the mill repo registry, with a fallback to `["robotsix-chat"]`.
- Extend subsession `dedup_key` deduplication from `user_chat` only to all subsession kinds, preventing duplicate periodic ticket monitors when an agent re-files the same ticket.
- New `http_probe` tool: the chat agent can now perform read-only HTTPS GET requests
  against public URLs to verify uptime and content. The tool returns HTTP status,
  final URL (after redirects), response time, Content-Type, body size, and a body
  snippet with optional content assertions (`expect_status`, `expect_contains`,
  `expect_absent`). Gated behind `http_probe.enabled`, hostname-allowlisted,
  size-capped, and timeout-limited — safe for autonomous use.
- Periodic monitor prompt: narrow `NO_CHANGE` to only when the observed state is truly identical to the prior run. Any state transition (e.g. draft → implement_complete) now produces a concise acknowledgment with an optional next-step offer instead of being silently suppressed.
- Add "Merge / PR management" bullet to agent system prompt (v28) documenting
  that direct-repo tools push branches and open PRs without auto-merge, and
  that merge capability exists through the mill API via component_request
  (merge-now and related endpoints). Prevents the agent from falsely claiming
  it cannot merge approved MRs.
- Added ``update_pr_branch`` and ``check_pr_merge_conflict`` agent tools to the direct-repo capability. The tools let the agent rebase a PR branch via the GitHub update-branch API and inspect mergeability state, enabling autonomous conflict detection and resolution for blocked tickets.
- System prompt v28: Add "Verification" section instructing the agent to cross-reference
  memory-based claims against live system state through available tools. When the user
  challenges a claim with contradictory observable evidence, re-verify immediately rather
  than doubling down on memory. Prefer timestamped evidence (commit SHA, deployment
  timestamp, tool call result) over recollection.
- Periodic subsessions now auto-escalate when a monitored ticket is stuck at `human_issue_approval`: a new config key `subsessions.human_approval_timeout_runs` (default 5) controls how many consecutive `NO_CHANGE` runs trigger an auto-escalation close with reason `human_approval_timeout`.  The subsession's parent agent receives the summary and can act on it (re-open, notify, etc.).  The resume status check also detects `human_issue_approval` state and updates the checkpoint so the periodic loop can enforce the timeout without re-polling the board.
- Enable `changelog_autofill` periodic task for auto-committing changelog entries on PRs with failing changelog CI checks.
- **Breaking:** Remove static `feedback.repo_ids` config key and `FEEDBACK_TARGET_REPOS` env override.  Allowed feedback target repos are now resolved dynamically at run-time from the deploy server's chat-component roster (``DEPLOY_API_KEY`` env var) intersected with the mill board's repo registry.  Falls back to ``["robotsix-chat"]`` when deploy is unreachable.  No chat-side config change or redeploy is needed to add/remove target repos — granting or revoking access in robotsix-deploy is sufficient.
- Add `watch_service_redeploy` lifecycle tool that polls a service config until a redeploy is detected or a timeout expires, helping the agent break redraft-loops after mill fixes are merged but not yet deployed.
- Extract `_missing_note_error` helper in `KnowledgeStore` to deduplicate the inline error-entry construction in `append()` and `update()`.
- Convert subsession error helpers and inline `JSONResponse` sites to raise `HTTPException` so they flow through the centralized error envelope and include `correlation_id`.
- Unify error response envelope: all error handlers and inline validation errors now emit ``{"error": "...", "correlation_id": "..."}`` instead of mixing ``{"detail": ...}`` and ``{"error": ...}`` shapes. Added catch-all ``Exception`` handler for graceful 500s.
- Subsessions: add `dedup_key` parameter to `spawn_subsession` for global-issue deduplication. When spawning a `user_chat` with a `dedup_key` that matches an already-active user_chat, the spawn returns the existing subsession id instead of creating a duplicate — preventing redundant side-chats for a single root-cause error (e.g. an `asyncio.run` crash affecting multiple ticket monitors).
- Periodic subsession `NO_CHANGE` suppression now covers minor, low-value
  state transitions (draft→ready, waiting_for_ci→in_progress, label changes,
  routine CI runs) — only substantive changes (first-time blocking, completion,
  failure, user-action transitions) produce full reports. Minor but notable
  changes surface as a concise one-liner.
- Add "Secret handling" section to the agent system prompt (v26) covering three
  rules: pre-empt secrets before they are pasted, never echo plaintext secrets,
  and remediate already-exposed credentials with a rotation warning.
  The section names the concrete secure channel (vault / one-time-secret link /
  registration ticket secure scope) for credential registration.
- Blocked-ticket resume now verifies worker freshness before auto-resuming. The resume logic queries the mill's ``/health`` endpoint for ``started_at`` and compares it against a stored checkpoint value. If the worker has not been redeployed after two consecutive blocked-ticket resumes, the subsession is closed with reason ``stale_worker`` to prevent futile retries on a stale image.
- Add docstring to `CogneeMemory._configure()` documenting its purpose and key side-effects.
- Fix: resume context messages ("Ticket TICKET-1 is BLOCKED", etc.) are no longer silently discarded on the first turn of a recovered periodic subsession.
- System prompt v24: add Efficiency rule instructing the assistant to condense repeated service-restart notices into a single summary rather than repeating each one verbatim.
- Feedback pipeline now supports multiple target repos via `feedback.repo_ids` (default `["robotsix-chat"]`). Each candidate ticket carries a `target_repo` field; the runner validates it against the configured list and POSTs to the correct board. Env override `FEEDBACK_TARGET_REPOS` (comma-separated) allows changing targets without a code change.
- Deduplicate repetitive restart notice entries: when a chat restart affects multiple subsessions with the same title and kind, the restart notice now collapses them into a single line with a count rather than repeating the same message verbatim.
- When an agent task fails with a retryable error, the enclosing subsession now emits a
  ``subsession_message`` frame (severity: warning) as feedback to the parent agent in real
  time, so the parent can react to the failure without polling.
- Removed a `>>>` prompt marker left over in `robotsix_chat/knowledge/store.py`
  that appeared as raw markdown in the intermediate `knowledge_review`
  presentation; also removed the empty notebook-code sections after the `>>>` shell snippets
  in that same generated content.
- System prompt v23: add "Trust but Verify" rule: check the agent's own reasoning
  traces, challenge source-level assertions, and require tool-based verification
  of access claims before affirming them.
- `list_tasks` `broker_query` now includes `status='needsAction'` to skip
  completed tasks automatically, matching the human-facing calendar view.
- `query_calendar` broker query now maps `model_level` to a certainty threshold:
  level-1 (claude-sdk) → >= 0.99, level-2 (opus-4.5) → >= 0.95, level-3
  (haiku-4.5) → >= 0.90, level-4 (gpt-5-nano) → >= 0.80.  This prevents
  low-certainty OpenRouter results from producing garbage tool calls.
- System prompt v21: add two "Avoid going in circles" rules instructing the
  assistant to label retried-failed actions so the operator can recognise the
  loop, and to stop retrying after 2 consecutive identical-step failures and
  instead escalate to the operator with a concise summary.
- Added `mill.retry_queue_enabled` config (default `true`, env
  `MILL_RETRY_QUEUE_ENABLED`), with `False` passthrough mode for integration
  testing.
- Added `ROBOTSIX_MILL_BASE_URL` env-var override for `mill.base_url`, matching
  all other `*_BASE_URL` named env vars.
- Added `query_calendar` and `query_tasks` agent tools providing live read
  access to the user's calendar events and Google Tasks via the
  `robotsix-calendar` broker.
- Calendar client authenticates via a configured `CALENDAR_API_TOKEN` (bearer
  token), validates the `robotsix-calendar` server's TLS certificate, and fails
  fast (5 s timeout) so the agent gets a clear error instead of a hung tool
  call.
- Added `CALENDAR_CACHE_TTL` env var override for `CalendarSettings.cache_ttl`.
- Added "Self-review and Health Checks" section to the system prompt (v20),
  instructing the assistant to run periodic self-reviews (check-now) via the
  `check_loop` tool when not in an active user conversation. Includes explicit
  starting prompt, instructions to vary review topics, filter to
  in-progress/actionable tickets only, and report summary-only unless
  attention-worthy.
- New `delegate_task` tool allows the assistant to spawn background agents
  (sub-agents) for asynchronous work like ticket filing, multi-day check
  monitoring, and config generation.  Each sub-agent gets its own LLM call and
  keeps a conversation independently of the main conversation.  Supports
  `kind: "check_loop"` for periodic checks with interval and TTL, and
  `kind: "one_shot"` for fire-and-forget tasks.
- The `start_check_loop` tool now wraps `delegate_task(kind="check_loop")` for
  legacy API compatibility.
- New `list_delegate_tasks` tool returns all running tasks (kind, id, prompt
  preview, running time) for the current session.
- Added server-side TTL (time-to-live) for check loops: automatic termination
  after the configured number of iterations or elapsed time, with a notification
  sent to the parent agent via the SSE events channel.
- Check-loop iteration count is now persisted to `.data/check_loops.json` and
  survives container restarts.  Restarted loops pick up where they left off
  instead of resetting their iteration counter.
- Added `latest_feedback` field to the Check Loops UI panel, showing the most
  recent sub-agent response summary in real time.  Stale feedback (≥2 minutes)
  is visually dimmed.

- Documentation site rebuilt in-repo under `docs/` using MkDocs Material,
  deployed via GitHub Actions to `gh-pages` branch at
  `https://damien-robotsix.github.io/robotsix-chat/`. Includes detailed
  setup, configuration, and architecture guides with cross-references.
  Config is at `mkdocs.yml`; CI workflow at
  `.github/workflows/docs.yml`.

- Added `version_check` module: at startup, the server fetches the latest
  release tag from GitHub (via saved `etag` for cache-aware conditional
  GET), compares it against the running image's `SOURCE_SHA` label, and
  logs a WARNING when the image is behind the latest release.
  `version_check.enabled` (default `true`) gates the check;
  `SOURCE_SHA` is injected by the Dockerfile (`org.opencontainers.image.revision`).

- Default `llmio_model_level` lowered from `3` (claude-sdk-haiku-4.5) to
  `1` (claude-sdk-sonnet-4.5) in the committed config template.
  Level 1 is the canonical default for the agent loop.

- `component_client` module for querying other robotsix components via
  their `/health` endpoints (read-only).  The agent's `component_request`
  tool now communicates through the robotsix chat broker
  (`component_request/*` topics) instead of direct HTTP.  This removes
  the last direct outbound HTTP call from the agent loop — all tool calls
  now go through the broker.  The direct HTTP path is retained in
  `component_client` for operational use (not wired to tools).

- `robotsix-chat` can now be configured to act as a proxy for an agent
  hosted on a separate `robotsix-agent` server.  When
  `COMPONENT_AGENT_ENABLED=true`, the component agent path is used
  instead of the local factory, and the component agent host/port/auth
  are read from env vars.

- `component_request` tool now supports requesting agent-inaccessible
  endpoints (e.g. `/onboard/preflight`, `/onboard/confirm`) by internally
  using the chat server's own HTTP client instead of forwarding the
  request to the agent.

- `knowledge_store` agent tool and endpoints for persistent key/value
  knowledge storage.  The assistant can `store_knowledge` (upsert a note
  with an immutable id and optional topic and tags), `update_knowledge`
  (append text to a note), `list_knowledge` (list all notes with metadata),
  `read_knowledge` (retrieve a note by id), and `search_knowledge` (match
  topic or content by substring).  Knowledge is persisted to
  `/data/knowledge.json`.

- added `mail` module with `send_mail` broker integration so the agent can
  send emails through the `robotsix-mail` broker.

- Enhanced broker resilience: `BrokerPubClient` now caches the `broker_client`
  across calls, and all three client-facing endpoints (`/chat`, `/loops`,
  `/loop/{id}/stop`) acquire a resilient client (with retry/backoff) on first
  use rather than creating a new client for every invocation.

- Added `delegate_task` tool — long-running board-tick tasks now run on their
  own pydantic agent, decoupled from the main executor loop.  Tasks return
  status through the SSE channel without blocking the main agent.

- Streaming tokens now flow to the frontend per-task, enabling state to be
  reported to the user without waiting for the full reply.

- `GET /health` now includes `llmio_model_level`, `agent_count`, and per-session
  agent metadata, reflecting the live state of all active sessions.

- Chat UI shows streaming token-by-token updates as the assistant generates
  each reply.

- Added `--output-dir` CLI flag to the `robotsix-chat` console script for
  specifying where generated files should be saved.

- ⚠️ **Doc-only breaking change**: Renamed `docs/user-guide/configuration.md` to
  `docs/configuration.md` as part of standardizing the docs layout under the
  top-level `docs/` directory.

- System prompt v46: add "Repo creation bootstrap" guidance — proactively seed an initial commit during repo creation to prevent tool-chain deadlocks with empty repos.
- System prompt v46: added conciseness rule for periodic subsession terminal-state
  notifications — report outcome in one sentence instead of echoing full run history.
- System prompt v46: added two deduplication rules to prevent redundant subsession creation — periodic subsessions must not spawn task children to perform their own monitoring work, and `list_subsessions` must be checked for existing periodic monitors before spawning a task subsession for the same ticket.
- System prompt v46: instruct the assistant to spawn periodic monitors directly rather than creating child task subsessions whose only job is to launch a monitor, preventing redundant model round-trips and duplicate spawning logic.
- Add explicit-instruction rule to system prompt Autonomy section: when a user gives a clear, firm instruction (e.g. "close the superseded ticket without asking"), the agent must carry it out literally without requesting additional confirmation.
- Added `__all__ = ["build_render_url_tools"]` to `robotsix_chat.render_url` for consistency with all other tool sub-packages.
- Added `search_knowledge_notes` tool to the knowledge base — the agent can now query
  prior diagnostic notes, deployment statuses, and other key facts by content substring
  match, without needing to recall exact note IDs. Results are ranked by relevance
  (exact topic match > topic contains > content contains).
- Add pytest-benchmark microbenchmarks for the chat server's critical request paths (SSE streaming, /health, / UI), gated behind `--benchmark-only` and run on push to main only.
- Added 28 unit tests for ``subsessions.py`` route handlers covering all error branches (503, 404, 400, 409) and the idempotent close flow.
- `push_direct_repo_branch` ticket-state verification now uses the same roster-based board connectivity as `component_request`, fixing failures when `board_api_base_url` differs from the roster-provided mill URL. Error messages now include the connectivity path or URL tried for direct diagnosis. `fetch_repo_for_study` URL-encodes branch refs containing slashes (e.g. `mill/20260723T...`), fixing 404 errors on such refs.
- Fix stale `SYSTEM_PROMPT_VERSION` in `docs/configuration.md` (v38 → v45). Add CI governance test to prevent future drift.
- Subsessions: guarantee delivery of `complete_subsession` outcomes to the parent
  conversation even when an external close (HTTP endpoint) races the worker.
  The `complete_subsession` tool now delivers the summary immediately inside
  the tool call, with a `delivery_done` flag on `CloseState` preventing the
  worker from delivering a second time. This closes a race where a user_chat
  spawned by a periodic subsession could strand its outcome — the tool
  persisted the terminal state but the worker was cancelled before delivering,
  and the HTTP close endpoint saw "already closed" and also skipped delivery.
- Periodic subsessions are now excluded from the restart notice injected
  after a server restart.  They resume silently and report results via
  their normal delivery channels, preventing the parent agent from
  taking unnecessary action on every redeploy.
- The ``SubsessionPeriodicSpawnError`` message now includes actionable
  alternatives when a periodic subsession attempts to spawn a periodic
  child (use a one-shot task, modify the existing monitor, or ask the
  operator).
- Remove 23 dead re-exports from `robotsix_chat.chat.server` that had zero
  external consumers importing through the package path.
- Add Model Policy section to system prompt defining named tier labels (cheap-high-perf, default, strong-reasoning, primary-frontier) mapped to existing model levels 1-4. Assistant now uses tier labels instead of hardcoded model names when filing tickets that specify model requirements, keeping configurations evergreen. [v46]
- Remove the "New autonomous" button from the UI (button element in index.html,
  handler + function + config-display toggle in chat.js). The single-session
  model makes manual creation unnecessary and dangerous.
- Autonomous mode: auto-start one bootstrap session on startup when the session store is empty (e.g. fresh deploy or wiped data), so autonomous is never permanently idle with no way to bootstrap.
- Subsessions panel now auto-opens when switching to a session that has active background subsessions, making autonomous-session subsessions visible in the same way as interactive-session subsessions.
- Autonomous sessions resumed after a process restart now receive a restart-context
  message ("SYSTEM RESTARTED") in the agent prompt so the agent knows the system
  was restarted and can resume appropriately. Covers selecting_subject and executing
  states. Added ``is_restart`` parameter to ``_kickoff_initial_turn`` and
  ``_auto_continue``.
- ``_close_and_respawn`` now wraps its body in try/except with logger.exception,
  matching the pattern in ``_auto_continue`` and ``_kickoff_initial_turn``, so
  unhandled exceptions in background respawn tasks produce actionable log messages.
- Rework autonomous `_close_and_respawn` to be non-blocking: respawn and its kickoff
  are scheduled as background tasks so startup/lifespan is never blocked by respawn.
  Enforce single-session invariant: at most one open autonomous session per owner at
  any time; `create_session` returns the existing open session when one already exists.
- Extract `_stream_summary` helper from the duplicated stream-collect-join pattern shared by `_generate_title` and `_generate_idle_summary` in `chat.py`.
- Deduplicate `sessions_approve_endpoint` and `sessions_reject_endpoint` by extracting a shared `_session_action` helper parameterised by the action verb.
- Fix queued messages not being dispatched after session focus switch: `restoreDraft()` now calls `drainQueue()` so restored queued messages are dispatched automatically when the user returns to a backgrounded session.
- Enhanced the auto-stop summary for periodic subsession monitors: when a
  watcher closes after ``auto_stop_no_change_runs`` consecutive ``NO_CHANGE``
  runs, the summary now includes the elapsed monitoring time, the last-known
  checkpoint state, and actionable guidance (e.g. "consider checking
  step-level logs"). The ``human_approval_timeout`` close reason also
  includes elapsed time.
- Add CI check (`check-activity-kinds`) to validate `frame.kind` comparisons in `chat.js` against the canonical `ACTIVITY_KINDS` frozenset in `events.py`, preventing silent frontend breakage when activity frame kinds are added or renamed.
- Extract shared boilerplate from three GitHub endpoint functions into a ``_github_endpoint`` helper, eliminating ~62 lines of duplicated settings/auth/path-param/body-parse/scope-check code.)
- DirectRepoClient now automatically detects expired GitHub App installation tokens (HTTP 401) and re-mints the token before retrying the request once. This prevents push failures in long-running sessions where the token expires between clone and push.
- Autonomous sessions now receive the same subsession and notification tools as interactive chat sessions (`spawn_subsession`, `notify_user`, etc.). Previously the autonomous agent factory omitted `subsession_env` and `event_sink`, so per-request tools were never built, and the system prompt instructed the agent to use tools that didn't exist.
- Autonomous sessions: strengthened the post-approval proceed message from
  a passive "Proceed with the approved plan." to an explicit "OPERATOR
  APPROVAL RECEIVED" directive that instructs the agent to begin executing
  the first step immediately, preventing stalled sessions after approval.
- Autonomous sessions now stream live tokens via the `/events` SSE channel
  (`autonomous_token` frames during each turn) and publish a completed
  `agent_message` frame after each turn is recorded — so the conversation
  area renders live progress and the transcript is immediately visible in
  `/history`, matching the normal `/chat` experience.
- System prompt v45: add cognee memory-recall verification rule. Recalled memory is similarity-based and can be stale, incomplete, or fabricated — when a recalled claim asserts a concrete fact about external state, cross-check it against the live API before acting. The recall wrapper in ``_MEMORY_PROMPT_HEADER`` now explicitly warns the model that recalled text may be hallucinated.
- Added CI workflow edit checklist to the implement-stage agent guidance
  (shadow-package override in ``src/robotsix_mill/agent_definitions/implement.yaml``).
  The checklist covers the three most common preventable CI failure classes:
  missing permissions blocks, wrong tool install methods (``uv pip install --system``
  vs ``uv tool install``), and missing ``--extra`` dependencies.  The mill's
  implement agent now checks these when a ticket touches ``.github/workflows/`` files.
- Decision chats spawned by periodic subsessions now surface in the main conversation immediately while also being relayed to the periodic parent's inbox so it doesn't re-spawn duplicates on its next wake.
- User-chat subsessions can no longer spawn nested user-chat subsessions, preventing stacked orphaned decision chats.
- Fix `GET /sessions` 500 error for owners with autonomous sessions: the autonomous-annotation block referenced `app.state.settings` which was never set. Expose `max_auto_turns`/`session_color` as properties on `AutonomousRunner` and use them directly from the runner already in scope.
- Fix autonomous session kickoff crash: `RuntimeError: asyncio.run() cannot be called from a running event loop`. Agent factory calls in `_kickoff_initial_turn`, `_auto_continue`, and `_close_and_respawn` are now offloaded to a thread executor via `asyncio.to_thread`, matching the subsession worker pattern.
- Add `POST /chat/github/repos` endpoint that creates GitHub repositories with `auto_init=true`, ensuring every new repo has a default `main` branch with an initial README commit — prevents `git clone --branch main` failures on empty repos.
- `repo_study`: fix private-repo fetch by preserving the GitHub App installation token across the API→codeload redirect (httpx was stripping it). Token-exchange failures and 403 scope errors now raise loud, specific errors instead of silently falling back to unauthenticated access.
- System prompt v43: add "Deploy preflight" gate requiring the assistant to retrieve deploy/docker-compose.yml, check the chat_agent_deployable_components allowlist, and verify endpoint capabilities before any deploy call — prevents guessing at deploy endpoint support for multi-service components.
- Migrate in-container GitHub App minting to the shared ``robotsix-github-auth`` library.  Removes 120 lines of JWT creation, caching, and token-exchange logic from ``src/robotsix_chat/repo/direct/client.py``.  ``DirectRepoClient``, ``WorkspaceManager``, ``RefDocsClient``, and ``VersionCheckClient`` now all mint installation tokens through ``robotsix_github_auth.mint_installation_token``.
- Remove ``github_token`` PAT fields from ``RefDocsSettings`` and ``VersionCheckSettings``.  Both doc-fetch and version-check read access now authenticate via the GitHub App installation token (falling back to unauthenticated when the App is not configured), matching the pattern already used by ``RepoStudySettings``.
- Add "Server-side capability probes" guidance to system prompt (v43): when checking
  whether a new server-side capability is available, the agent must probe the
  endpoint directly and compare the server's running image digest against the
  expected digest from the merged PR, rather than relying on static skill
  descriptions or treating catch-all redirects as confirmation.
- Add optional ``session_color`` and ``initial_task`` fields to autonomous settings, allowing operators to configure a CSS accent color for autonomous session rows and a default initial task that the agent spawns on session start.
- System prompt v43: Add verification guidance to read relevant source files
  (gate functions, compose labels, deploy contracts) before filing tickets
  involving authorization or configuration changes, and include accurate
  context in the ticket spec rather than filing based on assumptions.
- Add unit tests for ``maybe_generate_towncrier_fragment`` covering all
  code paths (no pyproject.toml, missing towncrier config, malformed TOML,
  fragment creation, custom directory, existing fragment skip, OSError).
- Config: migrate two remaining env-secret slots to config-standard
  (``config/config.json`` + pydantic ``SecretStr``). Added
  ``FeedbackSettings.deploy_api_key`` (replaces ``DEPLOY_API_KEY`` env
  var), ``ComponentCredentials`` model keyed by component id, and
  ``CentralDeploySettings.component_credentials`` dict. Updated
  ``component_access/tools.py`` to resolve credentials from config
  instead of env-var indirection, and ``feedback/runner.py`` to thread
  the deploy API key from settings. Regenerated
  ``config/config.schema.json``.
- Session-draft persistence: queued messages and attached images survive
  session switches, page refreshes, and tab focus loss. A new
  `GET/PUT /sessions/{session_id}/draft` endpoint persists the draft
  state per session; the frontend syncs on navigation, blur, and
  beforeunload, and rehydrates on session load.
- Fix deadlock in `_auto_continue` when an autonomous session completes: release the per-owner `asyncio.Lock` before calling `_close_and_respawn` to avoid re-acquiring the same non-reentrant lock via `_kickoff_initial_turn`.
- Autonomous sessions now start immediately on creation: the agent is kicked
  off to perform subject selection and plan drafting, and state transitions
  are streamed live to the browser via the `/events` channel. The session
  list shows per-state feedback ("Selecting a subject…", plan preview,
  "Executing (turn N)", "Completed"), and the "🤖 New autonomous" button
  is styled consistently with the sessions panel.
- Add retry loop (3 attempts) around `playwright install --with-deps chromium`
  in the Dockerfile to handle transient Debian mirror inconsistencies
  during image builds.
- Autonomous sessions: add creation path (``POST /sessions`` with ``{"autonomous": true}``), persistence to ``/data/autonomous_sessions.json`` so sessions survive restarts, and frontend approve/reject buttons for ``awaiting_approval`` sessions. Also add ``autonomous`` and ``github_actions`` to the settings ``SECTION_ORDER`` so their config panels render in proper position.
- Add self-mutation bootstrap guidance to the system prompt (v42): when a permission flag requires a service recreate to take effect, the agent now recognizes the chicken-and-egg problem and directs the operator to a one-time external action rather than filing tickets for fixes that already exist.
- `component_request` tool: added optional `max_response_chars` parameter for per-call truncation control, so the agent can request a compact summary of large ticket histories before expanding
- Enable `--strict` mode on the MkDocs build with an explicit `validation:` block (nav + links), so broken internal links, dead anchors, and removed pages fail the `Docs / Build docs` CI job instead of silently deploying a degraded site.
- Fix guard paragraph in system prompt to clarify the agent **can** access external systems and the network through its explicit tools, rather than falsely stating it has no network access at all (which contradicted http_probe, component_request, lifecycle mutation tools, direct-repo tools, and mill board API).
- Flush pending Langfuse traces on server shutdown so observation trees are
  captured even when the server stops soon after a trace completes.
- Extract mill-communication helpers from ``worker.py`` into dedicated ``worker_mill.py`` module (``_check_resume_status``, ``_handle_mill_unreachable``, ``_get_mill_started_at``, ``_reset_mill_failure_counter``, and related constants).

- Removed dead `ConfigError` exception class — `robotsix_config.load_config()` already wraps all errors in its own `InvalidConfigError(ConfigError)`, making the local class redundant.

- Fix `memory.langfuse.host` config field: `CogneeMemory._register_litellm_langfuse_callback()` now reads the host from `settings.memory.langfuse.host` directly instead of from environment variables (`LANGFUSE_BASE_URL` / `LANGFUSE_HOST`), which were being set from the top-level `langfuse.host` config rather than the memory-specific one.

- Periodic ticket monitor: when reporting terminal state (done/closed), the agent is now instructed to check ticket events/history for PR merge status rather than relying solely on the `pr_url` field, avoiding misleading "no PR URL" reports for auto-merged PRs.
- One-shot (`task`) subsessions are now re-enqueued automatically after a server restart instead of being lost. The task's checkpoint (if any) is preserved so the agent can pick up where it left off.
- Mark 30 expert-only config settings as `advanced: true` in the committed schema so the central-deploy Configure UI hides them behind the "Show advanced settings" toggle. Common settings (`llmio_model_level`, `llmio_api_key`, `idle_timeout_minutes`, `log_level`, `log_json_format`, `langfuse`, `knowledge`) remain always visible.
- Add `robotsix.deploy.chat-agent-mutatable: "true"` label to the production
  deploy compose file so the chat agent can mutate its own service config
  (restart, config-write, config-rollback) via central-deploy endpoints.
- Add prompt-level instructions for the assistant to automatically track unresolved operator prerequisites. When a ticket completes but a human-only action (e.g. provisioning a credential or token) is still required, the agent now files a follow-up tracking ticket and surfaces the prerequisite in session summaries and autonomous closure steps.
- System prompt v41: add deploy pre-check instruction — the agent now automatically verifies PR merge status before proceeding with deployment after a migration or fix ticket, rather than asking the user for confirmation.
- Extract `_session_metadata()` helper from duplicated session-metadata dict construction in `conversation.py`.
- Extract shared `_git_push_files` helper from `push_branch` and `push_commit_to_branch` in `DirectRepoClient` to eliminate 51 lines of duplicated Git blob/tree/commit pipeline code.
- Added `[tool.uv] exclude-newer = "7 days"` to `pyproject.toml`, preventing packages published less than 7 days ago from entering the lockfile. Complements the existing `UV_MALWARE_CHECK=1` hardening.
- Autonomous protocol: added guidance to detect and escalate the "empty-diff sub-ticket" failure pattern. When all child tickets of a split close immediately as no-change-needed referencing non-existent modules, the agent is now instructed to consolidate into a single re-implementation ticket rather than repeating the split.
- Periodic ticket monitors whose persisted checkpoint records a terminal
  ``last_known_state`` (``closed`` or ``done``) are no longer respawned on
  service restart — the resume hook now checks the checkpoint before
  spawning a worker and closes the subsession instead, preventing
  re-polling of tickets whose monitors had already been cleanly stopped.
- Updated the built-in `health` periodic check to use live HTTP probes (`http_probe`) as the primary health signal instead of relying on deploy-run status alone. A green deploy pipeline no longer counts as "healthy" — the probe must confirm the site is actually serving content.
- Ticket monitors no longer self-close after 2 consecutive mill-unreachable
  failures.  Instead they enter a recovery mode with exponential backoff
  (configurable via `subsessions.mill_recovery_initial_backoff_seconds`,
  `mill_recovery_max_backoff_seconds`, and `mill_recovery_max_retries`),
  probing the mill health endpoint on each cycle and resuming automatically
  when the mill becomes reachable again.  The subsession is only permanently
  closed after exhausting all recovery retries.
- Periodic monitor no-change suppression is now more robust: catches common
  LLM paraphrases of "nothing changed" and suppresses verbatim duplicate
  replies, reducing noise when long-running background tasks are tracked.
- Added GitHub Actions tools: ``set_actions_secret`` (set repository Actions secrets via libsodium encryption) and ``dispatch_workflow`` (trigger ``workflow_dispatch`` events). Both are LLM tools and HTTP endpoints under ``/chat/github/repos/{owner}/{repo}/actions/``, gated by a new ``github_actions`` config block with the same enable/API-key/auth pattern as ``github_security``. Requires ``pynacl`` (optional ``github-actions`` extra) for secret encryption.
- System prompt v40: add "user statements as ground truth" rule to the Verification section — when the user states a concrete fact, the agent must treat it as ground truth and raise a clarification question rather than contradicting it based on stale or misinterpreted evidence.
- Strengthen ticket deduplication check in agent system prompt: before filing a new ticket, check for any open or in-flight ticket addressing the same root cause or proposing a similar action, not just tickets with identical scope. Prevents symptom-workaround tickets from being filed when a root-cause fix is already in flight. (v40)

- Subsession children of periodic parents now relay their closure
  summaries directly to the active root conversation instead of the
  periodic parent's inbox, so operator decisions in side-chats are
  no longer stranded and ignored.
- Native autonomous session support: add `kind="autonomous"` as a first-class session type with built-in subject auto-selection, plan drafting, operator approval gate (409 server-side), execution, and auto-cycling (close + respawn). Gated behind `autonomous.enabled` (default `false`). Includes `AutonomousRunner` state machine, marker-based lifecycle transitions, approve/reject endpoints with `owner_id` authorization (403 on mismatch), and `max_auto_turns` enforcement.
- Fixed Python 2 ``except X, Y:`` syntax in multiple files — changed to correct Python 3 ``except (X, Y):`` tuple form.  Split the 503 "not enabled" checks in the GitHub endpoints so ``github_security`` and ``direct_repo`` failures produce distinct error messages.  Removed a redundant ``follow_redirects=True`` from ``get_job_log``.  Added two missing tests (404 job-not-found and 400 blank path params) for the job-log endpoint.
- New `GET /chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs` endpoint fetches plain-text GitHub Actions job logs, following the GitHub 302 redirect server-side and returning log content as a 200 response so the agent can inspect deploy pipeline output directly.
- Settings UI: new settings panel (⚙ button in header) with config editor,
  ``GET /config`` (returns config with secrets masked), and ``PUT /config``
  (deep-merges submitted form over existing config, validates through Settings
  before persisting).  Prevents partial saves from blanking unrendered fields
  like ``memory.embedding.endpoint``, and rejects invalid configs with inline
  validation errors.
- Extract stale-worker resume handling from ``_check_resume_status`` into
  a private helper ``_check_stale_worker_resume``, reducing the function from
  ~289 to ~210 lines and max nesting depth from 7 to 5.
- Reuse a single `httpx.AsyncClient` across all tickets in `FeedbackRunner._file_tickets` instead of creating one per ticket.
- Add minimum description length guard to ``FeedbackRunner._parse_tickets``
  to filter out auto-generated tickets with trivially short (boilerplate)
  descriptions before they reach the ticket board.  The threshold is 10
  characters — below the length of any meaningful improvement ticket. (mill: Improve caretaker ticket auto-generation handling (20260720T232929Z-improve-caretaker-ticket-auto-generation-7021))
- Add ``self_restart`` tool to the lifecycle module — a privileged endpoint
  (``POST /self/restart``) that restarts the agent's own service without requiring
  the deploy server's per-repo access toggle.  The existing
  ``restart_lifecycle_service`` tool still requires the toggle for restarting
  arbitrary services.  The system prompt now references ``self_restart()`` as the
  primary self-restart path after capability upgrades.
- Decision-chat subsessions now enforce self-contained option context: the user_chat worker prepends a system note reminding the agent to restate option definitions inline on every turn, the spawn_subsession tool instructs callers to include one-line option definitions in user_chat prompts, and the base agent instruction adds a critical rule against bare option labels. No operator-facing decision turn should surface "Option B" without its definition.
- Increased font size throughout the subsession (decision-chat / side) panel to match the main conversation pane for readability. Body text and inputs now use the same 0.95rem size as main chat bubbles; labels, badges, and action buttons are scaled proportionally.
- Fix orphaned `.drain` snapshot recovery in cognee backlog drain: if a prior drain
  crashed mid-processing (after renaming the backlog but before completing the drain),
  the orphaned snapshot is now detected and replayed instead of being silently
  overwritten on the next drain cycle.
- Document the Pydantic `extra="forbid"` convention as a config-standard rule in AGENT.md
- Add ``extra="forbid"`` to all Pydantic config models (20 sub-models + top-level ``Settings``). Unknown JSON keys now raise a ``ValidationError`` instead of being silently ignored — a typo like ``"memry"`` for ``"memory"`` is caught at config load rather than causing the operator to wonder why a feature is disabled.
- Add "CI Failure on Main" triage boilerplate to `docs/triage-boilerplate.md`, with ACKNOWLEDGE decision for main-branch infrastructure failures distinct from the existing OUT-OF-SCOPE boilerplate for PR failures.
- Fix race condition in durable backlog drain that could silently drop entries
  queued by concurrent failing writes. Backlog file is now atomically renamed to
  a snapshot before processing; still-failing entries are appended (not
  overwritten) to the original path. Drain failures no longer masquerade as
  write failures, and backlog-entry failures now feed frozen-store detection.
- Fix race condition in ``_drain_backlog``: add ``_drain_lock`` to prevent
  overlapping drain calls from silently dropping backlog entries or replaying
  duplicates.  Also correct the docstring ghost reference to
  ``_check_frozen_store``.
- Cognee memory: throttle merge-insert concurrency with a configurable
  inter-write delay to prevent LanceDB worker OOM during bursts. Add
  durable JSONL backlog for exchanges that fail after retries (drained
  opportunistically on subsequent successful writes). Bound DataFusion
  memory pool via ``DATAFUSION_RUNTIME_MEMORY_LIMIT`` (default 256M).
  Detect frozen vector store: emit WARNING when consecutive write
  failures exceed ``frozen_store_alert_minutes`` (default 10 min).
- Move `lifecycle/skill.md` from `docs/` into the packaged source tree at `src/robotsix_chat/lifecycle/` so the lifecycle skill instructions reach the agent in production (previously the `docs/`-relative path resolved nowhere in the Docker image; `load_lifecycle_skill()` silently returned `""`).
  `docs/lifecycle/skill.md` is now a symlink to the canonical copy.
- Add contract-version troubleshooting guidance to the system prompt: when users encounter "missing or incorrect central-deploy-contract-version header" errors during onboarding, the assistant now provides concrete diagnostic steps (check for the header, check recent PRs, file a targeted ticket) instead of offering vague workarounds.
- Add `direct_fix` tool to the direct-repo capability: pushes a commit
  directly to a target branch as a last-resort escape hatch for blocked
  tickets that have exhausted the mill's implement cycle limit (≥3 cycles).
  Gated behind `direct_repo.direct_fix_enabled` (default `false`). Every
  invocation is audited at WARNING level.
- Remove stale `agent_instruction` field from `config/config.json` so the code default in `Settings.agent_instruction` (v35) applies automatically. The committed config file is no longer a second source of truth for the system prompt — it drifts from the code default by ~10 sections and no automated CI check validated it. Now the code default is the single source of truth; operators who need a custom prompt can still add `"agent_instruction"` to their local or deployed config.
- Lifecycle module now exposes self-service mutation tools (`restart_lifecycle_service`, `update_lifecycle_service_config`, `update_lifecycle_service_env`) alongside the existing read-only tools.  These succeed or fail based on the deploy server's per-repo access toggle — no new client-side toggle is introduced.  The system prompt now references the lifecycle tools for self-restart instead of the unreachable `component_request("central-deploy", …)` path.
- Use `VALID_MODEL_LEVELS` (derived from llmio's `TierLevel` enum) instead of a hardcoded `(1, 2, 3, 4)` tuple in subsession model-level validation, so the valid range stays in sync with llmio.
- Register the `agent_check` periodic workflow (`.robotsix-mill/periodic/agent_check.yaml`) for automated agent/tool integrity checks.
- Add pyright type checker to pre-commit and CI, alongside the existing mypy
  `--strict` check.  The baseline config (`[tool.pyright]` in `pyproject.toml`)
  uses `typeCheckingMode = "basic"` with the most valuable diagnostics enabled
  as warnings for gradual adoption; only `reportMatchNotExhaustive` and
  `reportUnnecessaryContains` are errors.

- release-image: Fix "Verify CI is green" self-exclusion timeout by adding name-based fallback when `getWorkflowRun` fails to return a check-suite id (#TBD)
- Add a "Deploy system" bullet to the Autonomy section of the system prompt clarifying that the robotsix-deploy (central-deploy) management plane is a runtime API server — component onboarding, lifecycle operations, and configuration changes are all API-driven (POST /onboard/preflight, /onboard/confirm, etc.) with no git PRs needed.
- Add batch-MR-approval guidance to the agent system prompt: when multiple MRs are pending human approval, the agent must first categorize them by relevance to active tickets, present a compact filter prompt, and approve the selected group in bulk through the mill's merge endpoint. (#TBD)
- Periodic subsession auto-stop (``no_change_auto_stop`` and ``human_approval_timeout``) now logs a ``WARNING``-level message so operators can see when a monitor ceases watching and decide whether to restart it.
- System prompt v34: Explicitly classify merge/rebase conflicts as never-auto-retryable substantive blockers in the Remediate step. The assistant now surfaces a clear "human must rebase manually" message via user_chat instead of looping on resume-blocked. Worker blocked-resume context also warns about merge conflicts.
- Show a relative timestamp ("2m ago") at the bottom of each chat session for the last model-generated message, with the absolute server time on hover.
- Add `workflow_dispatch` trigger to `release.yml` for manual recovery deploys.
- Added `workflow_dispatch` trigger to `.github/workflows/docs.yml` to allow manual deploy of docs from the Actions UI.
- Fix stale comment on `_active_dedup_keys` in `Registry` — remove `user_chat` qualifier and `or periodic monitors`, matching the dedup scope after the kind guard removal in PR #662.
- Prompt: when multiple unowned, actionable items exist, the assistant now immediately offers a high-signal scoped confirmation prompt listing each item (e.g. 'Say: merge 5f1c, merge 2a97, rebase 54ea.') instead of asking an open-ended 'Which do you mean?'
- Add dedicated "Mill & Deploy Endpoints" section to the agent system prompt (v31),
  listing all key mill and deploy endpoints with paths, methods, and descriptions
  so the agent can reliably reference available endpoints without trial-and-error
  discovery.
- Added defense-in-depth dedup guard in ``SubsessionRegistry.create()``: raises ``SubsessionDedupError`` when a ``dedup_key`` is already active, preventing duplicate monitors even if the ``spawn_subsession`` pre-check is bypassed.
- Feedback runner: record OTel span error status (`StatusCode.ERROR`) and
  exception details on each ingest POST span when filing fails (non-2xx or
  HTTP exception).  The trace root span now carries `feedback.failed_tickets`
  alongside the existing `feedback.filed_tickets` and `feedback.total_tickets`,
  making filing failures immediately visible in Langfuse traces.
- Fix: periodic subsessions (ticket monitors) now correctly restore their `dedup_key` after server restart, preventing duplicate monitors from spawning for the same ticket.
- Document dynamic feedback target-repo resolution in `docs/configuration.md`: allowed repos are derived from the deploy roster intersected with the mill repo registry, with a fallback to `["robotsix-chat"]`.
- Extend subsession `dedup_key` deduplication from `user_chat` only to all subsession kinds, preventing duplicate periodic ticket monitors when an agent re-files the same ticket.
- New `http_probe` tool: the chat agent can now perform read-only HTTPS GET requests
  against public URLs to verify uptime and content. The tool returns HTTP status,
  final URL (after redirects), response time, Content-Type, body size, and a body
  snippet with optional content assertions (`expect_status`, `expect_contains`,
  `expect_absent`). Gated behind `http_probe.enabled`, hostname-allowlisted,
  size-capped, and timeout-limited — safe for autonomous use.
- Periodic monitor prompt: narrow `NO_CHANGE` to only when the observed state is truly identical to the prior run. Any state transition (e.g. draft → implement_complete) now produces a concise acknowledgment with an optional next-step offer instead of being silently suppressed.
- Add "Merge / PR management" bullet to agent system prompt (v28) documenting
  that direct-repo tools push branches and open PRs without auto-merge, and
  that merge capability exists through the mill API via component_request
  (merge-now and related endpoints). Prevents the agent from falsely claiming
  it cannot merge approved MRs.
- Added ``update_pr_branch`` and ``check_pr_merge_conflict`` agent tools to the direct-repo capability. The tools let the agent rebase a PR branch via the GitHub update-branch API and inspect mergeability state, enabling autonomous conflict detection and resolution for blocked tickets.
- System prompt v28: Add "Verification" section instructing the agent to cross-reference
  memory-based claims against live system state through available tools. When the user
  challenges a claim with contradictory observable evidence, re-verify immediately rather
  than doubling down on memory. Prefer timestamped evidence (commit SHA, deployment
  timestamp, tool call result) over recollection.
- Periodic subsessions now auto-escalate when a monitored ticket is stuck at `human_issue_approval`: a new config key `subsessions.human_approval_timeout_runs` (default 5) controls how many consecutive `NO_CHANGE` runs trigger an auto-escalation close with reason `human_approval_timeout`.  The subsession's parent agent receives the summary and can act on it (re-open, notify, etc.).  The resume status check also detects `human_issue_approval` state and updates the checkpoint so the periodic loop can enforce the timeout without re-polling the board.
- Enable `changelog_autofill` periodic task for auto-committing changelog entries on PRs with failing changelog CI checks.
- **Breaking:** Remove static `feedback.repo_ids` config key and `FEEDBACK_TARGET_REPOS` env override.  Allowed feedback target repos are now resolved dynamically at run-time from the deploy server's chat-component roster (``DEPLOY_API_KEY`` env var) intersected with the mill board's repo registry.  Falls back to ``["robotsix-chat"]`` when deploy is unreachable.  No chat-side config change or redeploy is needed to add/remove target repos — granting or revoking access in robotsix-deploy is sufficient.
- Add `watch_service_redeploy` lifecycle tool that polls a service config until a redeploy is detected or a timeout expires, helping the agent break redraft-loops after mill fixes are merged but not yet deployed.
- Extract `_missing_note_error` helper in `KnowledgeStore` to deduplicate the inline error-entry construction in `append()` and `update()`.
- Convert subsession error helpers and inline `JSONResponse` sites to raise `HTTPException` so they flow through the centralized error envelope and include `correlation_id`.
- Unify error response envelope: all error handlers and inline validation errors now emit ``{"error": "...", "correlation_id": "..."}`` instead of mixing ``{"detail": ...}`` and ``{"error": ...}`` shapes. Added catch-all ``Exception`` handler for graceful 500s.
- Subsessions: add `dedup_key` parameter to `spawn_subsession` for global-issue deduplication. When spawning a `user_chat` with a `dedup_key` that matches an already-active user_chat, the spawn returns the existing subsession id instead of creating a duplicate — preventing redundant side-chats for a single root-cause error (e.g. an `asyncio.run` crash affecting multiple ticket monitors).
- Periodic subsession `NO_CHANGE` suppression now covers minor, low-value
  state transitions (draft→ready, waiting_for_ci→in_progress, label changes,
  routine CI runs) — only substantive changes (first-time blocking, completion,
  failure, user-action transitions) produce full reports. Minor but notable
  changes surface as a concise one-liner.
- Add "Secret handling" section to the agent system prompt (v26) covering three
  rules: pre-empt secrets before they are pasted, never echo plaintext secrets,
  and remediate already-exposed credentials with a rotation warning.
  The section names the concrete secure channel (vault / one-time-secret link /
  registration ticket secure scope) for credential registration.
- Blocked-ticket resume now verifies worker freshness before auto-resuming. The resume logic queries the mill's ``/health`` endpoint for ``started_at`` and compares it against a stored checkpoint value. If the worker has not been redeployed after two consecutive blocked-ticket resumes, the subsession is closed with reason ``stale_worker`` to prevent futile retries on a stale image.
- Add docstring to `CogneeMemory._configure()` documenting its purpose and key side-effects.
- Fix: resume context messages ("Ticket TICKET-1 is BLOCKED", etc.) are no longer silently discarded on the first turn of a recovered periodic subsession.
- System prompt v24: add Efficiency rule instructing the assistant to condense repeated service-restart notices into a single summary rather than repeating each one verbatim.
- Feedback pipeline now supports multiple target repos via `feedback.repo_ids` (default `["robotsix-chat"]`). Each candidate ticket carries a `target_repo` field; the runner validates it against the configured list and POSTs to the correct board. Env override `FEEDBACK_TARGET_REPOS` (comma-separated) allows changing targets without a code change.
- Deduplicate repetitive restart notice entries: when a chat restart affects multiple subsessions with the same title and kind, the restart notice now collapses them into a single line with a count rather than repeating the same message verbatim.
- Extend ticket-lifecycle Initiate step with deduplication guidance: before filing a new ticket, check for an active ticket with the same scope; when a successor supersedes an older ticket, cancel the predecessor's monitor subsession to prevent duplicate monitors.
- Bump `SYSTEM_PROMPT_VERSION` to 23 and add v23 changelog entry for the reordered ticket-lifecycle steps (complete_subsession before restart) to satisfy system prompt governance.
- Prevent infinite restart loop from periodic monitor subsessions: `complete_subsession` now persists the closed state to the registry immediately (before the worker's post-turn check), so a subsession that triggers a self-restart is not re-loaded when the process comes back up. The prompt instructions now direct the agent to call `complete_subsession` *before* triggering a restart.
- Instrument mill ingest POST calls in feedback runner as TOOL spans so
  HTTP failures are visible in Langfuse traces.
- Fix feedback ticket filing: align ingest payload with mill's `TicketIngest` schema (`repo_id`, `title`, `body`, `source_tag`). Previously sent `description` instead of `body` and omitted required `repo_id`, causing 100% 422 rejection. Runner metadata (`kind`, `session_id`, `trigger_type`) is now folded into the body text. Added `feedback.repo_id` config field (default `"robotsix-chat"`).
- Subsession checkpoint persistence and automatic resume status check for ticket monitors: periodic subsessions can now store a `checkpoint` dict (e.g. watched ticket id, last-known state) that survives restarts. On service restart, recovered ticket monitors query the mill for current ticket state before resuming the monitoring loop — terminal tickets auto-close the subsession, blocked tickets get context injected for the agent to handle, and mill-unreachable errors trigger a consecutive-failures counter (capped at 2). A new `set_checkpoint` tool lets subsession agents update their own checkpoint data.
- Fix auto-scroll on session switch and page load: conversation view now reliably scrolls to the latest message after DOM layout completes.
- Refactor `MessageCoalescer._process_batch`: extract title-generation into `_maybe_generate_title` and SSE fan-out into `_fan_out` helper, reducing nesting depth from 7 to 5.
- Refactor `SubsessionRegistry` into three classes: extract `RegistryStore` (JSON persistence) and `RegistryIndex` (owner-scoped queries and tree operations), with `SubsessionRegistry` retaining core lifecycle and delegating to both.
- UI: conversation view now auto-scrolls to the bottom on session switch/load so the latest messages are always visible.
- Config: ``_normalize_legacy_empty_strings`` validator now also coerces JS-toString sentinels (``"[object Object]"``, ``"undefined"``, ``"null"``) to the appropriate empty container, preventing config corruption from a browser-side serialisation bug in the Configure UI.
- Fix summary panel layout shift: render summary as an absolute overlay
  outside the conversation's flex flow so appearing/resizing the summary
  no longer changes the chat scroll position.
- Conversation auto-scroll now preserves user scroll position: only scrolls to bottom when the user is already near the bottom (≤50px threshold), preventing viewport hijacking when reading history.
- Remove orphaned `[tool.bandit]` config from `pyproject.toml` and `security` target from `Makefile` (bandit is not a dependency; ruff's S rules cover the same checks)
- Extract `build_transcript()` utility into `_shared.py` to deduplicate a conversation transcript assembly loop shared between `chat.py` and `sessions.py`.
- Fix: ensure changelog fragment files (``changelog.d/*.misc.md``) end with a trailing newline, eliminating a ~7 min wasted CI ``fixing_ci`` cycle per ticket.  The fix overrides ``robotsix_mill.stages.towncrier`` via a local shadow package in ``src/robotsix_mill/``.
- Extract shared `_request_json(method, path, body)` from near-identical `_post_json` and `_patch_json` in `GitHubDirectClient` to eliminate 9 duplicated lines.
- Add docstring to `ConversationStore._evict_overflow` explaining its session-eviction and owner-registry cleanup behavior.
- Fix false unread highlight on the previously-active session: `refreshSessions()` now calls `markSessionRead(activeSessionId)` to keep the active session's unread baseline current on every auto-refresh cycle.
- Enable `state_sync` periodic check (`.robotsix-mill/periodic/state_sync.yaml`) to cross-reference enum members against string-literal reference sites across the codebase.
- Chat UI: LLM-generated session titles after the first assistant reply (uses the summary model tier). Fix sidebar "X days ago" timestamps by handling Unix-second timestamps correctly.
- Subsessions closing now trigger an immediate (fire-and-forget) reaction
  turn in the parent chat so the main agent sees and acts on the summary
  without waiting for the next user message.  Reaction turns are serialised
  with user-message turns via the per-owner ``RunSerializer`` lock and are
  depth-bounded (max 3) to prevent unbounded trigger chains. (mill: Subsession closure summary must trigger a main-agent run (redraft of a175) (20260717T233626Z-subsession-closure-summary-must-trigger-9e6a))
- Session sidebar: open by default on fresh load; persist close state in localStorage.
- Session list auto-refreshes every 20 seconds (paused when tab is hidden).
- Sessions with new agent messages since last viewed get a visual highlight (left border accent); clears on selection.
- Subsessions: add loop guard to reaction-turn delivery so a summary-triggered agent run that spawns and closes another subsession cannot create an unbounded trigger chain (`_reaction_in_progress` flag).
- Feedback runner now logs at WARNING level when `board_url` is empty, and at INFO level when disabled. Added config-validation: `feedback.board_url` must be non-empty when `feedback.enabled` is true.
- Guard cognee memory calls with configurable timeouts to prevent hung worker tasks
  when the LanceDB adapter lock is orphaned (recall 60 s, remember 300 s). Add a
  per-run watchdog in the subsession worker so a stuck periodic run is marked
  failed and the schedule continues instead of staying ``running`` forever.
- Deduplicate `.subs-header` and `.sessions-header` CSS into shared `.panel-header` class
- Extract `_parse_turns()` helper to eliminate duplicate turn-parsing loop in `ConversationStoreSerializer._load_legacy_format` and `_load_current_format`
- Remove redundant `_coerce_empty_string_to_*` field validators from `MemorySettings`, `RefDocsSettings`, and `ComponentClientSettings` — the top-level `_normalize_legacy_empty_strings` on `Settings` already handles all legacy `""` → `{}`/`[]` coercion before sub-model validation.
- Fix kuzu graph shadow-file self-heal to detect inconsistent databases
  where the DB entity exists but its companion ``.shadow`` is missing
  (the opposite of the orphan-artifact case).  Handle both file and
  directory DB forms.  Add open-time retry in ``recall()`` and
  ``remember()``: on a healable kuzu error (shadow-missing ENOENT or
  database-ID mismatch), the database set is removed and the operation
  is retried once, so the graph is rebuilt eagerly instead of degrading
  to "no memory" forever.
- **Memory (cognee):** Self-heal now handles the full kuzu consistency set — removes both `.shadow` and `.wal` artifacts together and recreates the database directory when any stale entries are found, preventing the "IO exception: Cannot open file" crash that occurred when a previously-deleted shadow was still referenced by a leftover WAL.
- Consolidate `github` module under shared `repo/` namespace as `repo.security` — move `src/robotsix_chat/github/` → `src/robotsix_chat/repo/security/`, `docs/github/` → `docs/repo/security/`, `tests/github/` → `tests/repo/security/`. Update all imports and module registration accordingly.
- Replace `docs/notification/skill.md` with a relative symlink to the canonical `src/robotsix_chat/notification/skill.md`, eliminating a duplicate copy.
- Replace `docs/github/skill.md` duplicate with a symlink to the canonical `src/robotsix_chat/github/skill.md` (deduplicate clone pair)
- Re-enable `copy_paste` periodic workflow: add `.robotsix-mill/periodic/copy_paste.yaml` to detect clone pairs with jscpd, triage by severity, and file draft tickets for high-severity duplication.
- Added `modules-registration` pre-commit hook that verifies every file in the repo is
  claimed by at least one module in `docs/modules.yaml`, catching unregistered new files
  before commit and preventing CI drift.
- Refactor `create_agent_from_settings` (213→98 lines): extract `_inject_skills`, `_build_static_tools`, and `_build_request_tools_factory` helpers.
- Enable `completeness_check` periodic agent to scan for dead code, unreferenced exports, and pattern gaps.)
- Split `subsessions/worker.py` (918 lines) into `worker.py` (turn loop, spawn logic) and new `resume.py` (startup resume hook, persistence entry helpers). Extracted kind-specific continuation into `_handle_kind_continuation` and kind-specific resume logic into `_resume_periodic_entry`, `_resume_user_chat_entry`, `_resume_task_entry`.
- Enable `bc_check` periodic agent to detect backward-compatibility debt and file draft removal tickets.
- Module curator: add premise-verification step to check for runtime references (`Path(__file__).parent / "skill.md"`) before proposing relocation of `skill.md` files from the source tree to `docs/`. Prevents silently broken runtime loads when a file is moved but a module still loads it from the old location.
- Restore `src/robotsix_chat/github/skill.md` — the file was moved to `docs/github/` in a prior reorganization but `load_github_skill()` still loads from the module directory, so the GitHub skill instructions were silently empty at runtime.
- Move `src/robotsix_chat/github/skill.md` → `docs/github/skill.md` to align with the per-module docs layout.
- Moved `src/robotsix_chat/notification/skill.md` to `docs/notification/skill.md` to align with per-module docs layout convention.
- Move `src/robotsix_chat/lifecycle/skill.md` → `docs/lifecycle/skill.md` to align with per-module docs layout.
- Added unit tests for ``FeedbackRunner`` and its helpers (``_build_feedback_prompt``, ``_parse_tickets``) — 52 tests covering pure functions, agent I/O (mocked), HTTP ingest (``respx``), subsession summarisation, error handling, and the full ``_run`` cycle.
- FeedbackRunner now produces named Langfuse traces (`feedback-{trigger}`) tagged `feedback` and `{trigger}`, forwards the source `session_id` to the LLM call, and stamps trace metadata (trigger type, session id, filed ticket counts).  Feedback runs are filterable via `GET /api/public/traces?tags=feedback`.
- Subsessions now survive service restarts: periodic monitors are re-armed automatically on startup (with one immediate tick if the scheduled run elapsed during downtime), and a restart notice is injected into each affected conversation listing which subsessions were resumed or interrupted — so the model can reconcile on its next turn.
- Pin liblzma5 to `5.8.*` (was `5.*`) in the Dockerfile runtime stage to ensure
  the patched `5.8.1-1+deb13u1` is resolved instead of the vulnerable `5.8.1-1`
  (CVE-2026-34743).
- Upgrade liblzma5 in the runtime Docker stage to resolve CVE-2026-34743 flagged by the Trivy container scan gate.
- Fix release-image `Verify CI is green` job timing out waiting on itself: use `actions.getWorkflowRun` to discover the check-suite id directly instead of matching against check-run `details_url` (which uses check-run ids, not workflow-run ids). Also handles the edge case where no non-self CI checks exist for a commit (break immediately).
- Document feature-flag activation rule in `AGENT.md`: any flag-gated feature must include activation config, live-proof, and post-deploy follow-up in its definition of done.
- Chat UI: queued (not-yet-processed) user messages now show a cancel button. Users can cancel individual queued messages (per-message ✕) or bulk-cancel all queued messages. Cancelled messages are removed from the processing queue server-side; messages already in processing are rejected gracefully.
- Config loader now coerces legacy ``""`` placeholders to proper empty arrays/objects for ``cors_allow_origins``, ``allowed_image_media_types``, ``refdocs.repos``, ``memory.llm``/``langfuse``/``embedding``, and ``component_client.components``, so partial config updates via the deploy API no longer fail on untouched legacy keys.
- Added "one subsession per subject" rule to the agent system prompt, instructing the agent to spawn separate subsessions for distinct subjects rather than consolidating unrelated ticket batches or decision groups into a single subsession lifecycle.
- Chat UI now renders suggested answer options as clickable chips when the assistant includes a `` ```suggestions `` fenced block in its reply. Clicking a chip submits it as a user reply; the free-text input remains available. Applies to both the main conversation and user_chat subsession panels.
- Add ``render_url(url)`` agent tool (Playwright headless Chromium) — captures a full-page screenshot and accessibility tree for UI verification. Gated behind ``render_url.enabled`` in config; requires the ``render-url`` extra (``playwright``).
- Formalize autonomous ticket lifecycle in the agent's system prompt (v21): Initiate, Monitor (periodic subsession: 30 min, max 60 runs, terminate after 2 mill-unreachable failures), Remediate (auto-resume transient failures, surface blockers), Complete, Reload (self-restart for capability upgrades), and Exit — replaces the single capability-upgrade bullet in the Autonomy section.
- Run subsession workers in a fresh execution context so their agent runs
  form their own Langfuse traces, grouped under the subsession's session id.
  Previously the worker task inherited the spawning turn's context — including
  the active OTEL span — so every subsession span nested inside the owner
  session's trace, making subsession runs effectively invisible as traces.
- Fence the recalled-memory block prepended to the user turn with an explicit
  "End of recalled memory" marker before the live message. Similarity-recalled
  text reads like the current topic, and without the fence the model could take
  the whole turn as background and see no active request — observed on a
  subsession first turn, which idled with "no live instruction" instead of
  executing its spawn instructions.
- User notification channel: proactive alerts when agent needs user attention via browser/native notifications over the existing SSE/EventBus channel (no external push-provider infrastructure needed)
- Add `notify_user` push-notification tool so the agent can proactively alert the user outside the active conversation flow — three trigger classes: subsession chat opens, subsession completes/raises, and state/result requiring user awareness. Gated by `notification.enabled`.
- Add ``PATCH /chat/github/repos/{owner}/{repo}/settings`` endpoint to toggle
  repository security-and-analysis features (dependency graph, advanced
  security, secret scanning, push protection) on repos under the GitHub App
  installation scope.  Requires ``github_security.deploy_api_key`` via
  ``X-API-Key`` header.  Returns 403/404/503 for auth/scope/config errors.
- New **automated feedback run** at compaction and session-end boundaries:
  analyses the conversation, surfaces actionable improvements, and files
  tickets via ``POST /tickets/ingest`` with ``source_tag`` dedup. Disabled
  by default; enable with ``feedback.enabled`` + ``feedback.board_url``.
- Extract inline `<style>` and `<script>` blocks from `ui/index.html` into
  standalone `ui/static/chat.css` and `ui/static/chat.js` files; serve them
  via a Starlette `StaticFiles` mount at `/static`. The `IDLE_TIMEOUT_MINUTES`
  value is now passed to JS through a `<meta>` tag instead of a server-side
  template variable.
- Render message content as Markdown in the chat UI (headings, bold, lists, code blocks, links, tables). Uses marked.js for rendering and DOMPurify for XSS sanitization. Streaming continues to display plain text during token delivery and re-renders as formatted Markdown on completion.
- New `set_repo_security_and_analysis` tool: enable or disable repository-level security features (dependency graph, advanced security, secret scanning, push protection) on repos under the configured GitHub organisation. Gated behind `github_security.enabled`; dynamically scoped to the GitHub App's installation repositories.
- Migrate PROJECT_TITLE to a `<meta name="project-title">` tag in index.html, and read it from the DOM in the inline JS instead of using Jinja2 placeholders, to prepare for extraction of JS into a static file.
- Increase subsession panel detail text font size from 0.75rem to 0.85rem for improved readability.
- Add configurable `component_response_max_chars` (default 200,000) to `central_deploy` settings, used as the truncation limit for GET/HEAD component responses — write methods keep the existing 8,000 limit. This lets the agent enumerate large ticket lists (e.g. mill board blocked tickets) without truncation.
- Rebuild and wire server-side idle compaction: re-implement `compact_session` and `get_compacted_summary` on `ConversationStore`, and wire idle-timeout detection into the POST /chat route so that when a session has been idle longer than `idle_timeout_minutes`, a summary is generated and injected into the agent context on the next message.
- Add zizmor pre-commit hook (`v1.26.1`) after actionlint to detect
  GitHub Actions workflow security vulnerabilities (script injection,
  hardcoded credentials, unsafe permissions).
- Allow `model_level` 4 for subsession spawns. The config layer, system prompt, and tool docs all described level 4 as valid for frontier-tier reasoning, but the runtime validator rejected it; the validator now accepts levels 1–4 consistently.
- Extract repeated `_serializer.persist` guard into a private `_persist()` helper in `ConversationStore`.
- Remove orphaned `scripts/check_kind_literals.py` (dead code — no CI
  job, pre-commit hook, or Makefile target references it) and update
  `scripts/check_sse_event_types.py` docstring to drop stale reference.
- Move `docs/api/robotsix_chat/server.md` to `docs/chat/server.md` to align with per-module docs layout convention.
- Moved `docs/api/robotsix_chat/config.md` to `docs/config/api.md` to align with the per-module doc layout convention.
- Remove dead `ConversationStore.stats()` method — zero callers in the codebase.
- Moved `docs/api/robotsix_chat/agent.md` to `docs/llm/agent.md` to align with per-module docs layout.
- Moved `memory` API doc from `docs/api/robotsix_chat/memory.md` to `docs/memory/api.md` to follow the per-module layout convention.
- Added "Out-of-Scope CI Failure" boilerplate to `docs/triage-boilerplate.md` for use in scope-triage decisions during `draft → ready` transitions.
- Remove unused `# noqa: E402` comment from `src/robotsix_chat/chat/server/__init__.py` to satisfy RUF100 (unused noqa directive).
- Subsessions: add `inherit_context` parameter to `spawn_subsession` — when set, a compact ancestor-context block (root task plus each ancestor's title/prompt summary) is prepended to the child's first turn, so nested subsessions no longer start from scratch and fall back on memory.
- Subsessions: persist and resume `user_chat` across server restarts — the worker is re-spawned under its original id with the original prompt plus the last delivered assistant state, instead of being marked `INTERRUPTED`.
- Tracing: subsession worker turns and main-chat reaction turns now stamp `parent_session_id`/`owner_session_id`/`subsession_id` as Langfuse trace metadata, so the trace tree mirrors the subsession tree in observability.
- `SubsessionsSettings.default_model_level` changed from `3` to `2` to match the system prompt guidance that level 2 "is the default choice for general work."
- Derive `chat.server.__all__` from `routes.__all__` instead of duplicating
  the endpoint-name list across two `__init__.py` files.  When a new route
  endpoint module is added, the public API of the server package
  automatically picks it up (provided the symbol is imported), avoiding
  silent `__all__` drift.
- Expand ruff ruleset with `ARG`, `N`, `RUF`, and `T` to catch unused
  function/method arguments, naming convention violations, ambiguous unicode
  characters, unsorted `__all__`, unused `# noqa` directives, and stray
  `print()`/`pdb` calls before they reach CI.  Per-file-ignores suppress
  known-safe patterns (test fixtures, intentional en-dash bullets in prompt
  strings, `NullMemory` protocol stubs).
- Consolidated duplicated `_get`/`_post`/`_patch` HTTP methods in `GitHubClient`
  into a single `_request(method, path, body=None)` private method, eliminating
  ~35 lines of copy-paste duplication.
- Added ``_SHARED_PARAMS`` constant and a sync-guard test to verify
  ``create_app()`` and ``run_server()`` share the same keyword parameters,
  preventing silent drift between the two signatures.
- When the central-deploy `github` virtual component backend is unavailable or misconfigured (returning another component's skill doc, bare 303 redirects), `component_request(component_id="github", ...)` calls are now intercepted and handled locally using `GitHubClient`. The local handler serves the correct skill document at `/chat-skill`, returns a proper component root at `/`, and delegates repo operations to the GitHub REST API.
- Fix top toolbar buttons being hidden behind the subsessions/sessions side panels.
  The header now uses `position: sticky` with `z-index: 30` so toolbar buttons
  remain above the panels, and on desktop the header is pushed aside via CSS
  `:has()` rules that match the existing content-wrap push layout.
- Add `github` virtual component: agent can create GitHub repositories (confirmation-gated), update repo settings, and read repo details.  Token provisioned via `GitHubSettings.token` (`SecretStr`) — never exposed to the chat container.
- Removed three unused public symbols: `ConversationStore.compact_session`, `ConversationStore.get_compacted_summary`, and `EventBus.subscriber_count` (dead code with no callers)
- Mirror source directory structure under `tests/chat/`: moved `test_server.py` and `test_idempotency.py` into new `tests/chat/server/`, moved `test_shared.py` into new `tests/chat/server/routes/`.
- Register the new `github` virtual component: a scoped GitHub repository-administration capability reachable via `component_request(component_id="github", ...)`. The component skill documents creating repos, setting metadata (description, visibility), and registering new repos with the mill board — all behind a 🛑 confirmation gate requiring explicit user approval before every write operation. The GitHub token is server-side only, never exposed in the chat container.
- Thickened the border around subsession rows in the subsession panel from 1px to 2px for better visual distinction.
- Persist subsession panel open/closed state in localStorage so it survives page refreshes instead of always resetting to closed.
- Rapid-fire user messages for the same session are now coalesced into a single agent run. A configurable debounce window (default 0.3 s) batches pending messages together, concatenating them with a separator and passing them to the agent as combined context. This avoids redundant runs and disjointed handling when messages arrive in quick succession.
- Consolidate duplicated `JsonStoreBase` subclass boilerplate: base class now
  uses `dataclasses.fields()` to auto-generate `_to_dict`/`_from_dict`, and
  `_default_path` class attribute eliminates the need for per-subclass
  `__init__` overrides.  `DiagnosticStore`, `KnowledgeStore`, and
  `FixProposalStore` now only declare `_store_name` and `_default_path`.
- Added `scripts/check_subsession_kinds.py` CI gate to verify that `SubsessionKind` enum values in `models.py` stay in sync with `.kind` string comparisons in `index.html`, preventing silent frontend breakage when a kind value is renamed.
- Remove unused `compact_session()` and `get_compacted_summary()` methods and the
  `compacted_summary` field from `ConversationStore` — the idle-timeout compaction
  path was never wired into a route handler or test.
- Prevent periodic subsessions from spawning periodic children; a periodic
  run that needs follow-up polling must reuse its own schedule rather than
  creating new periodic pollers.
- Mill component calls now automatically retry on transient errors (empty responses, network failures, 5xx for idempotent methods) with exponential backoff (~1s, ~2s). A lightweight health probe runs before the first attempt to distinguish genuinely-down components from transient hiccups. Non-idempotent writes (POST/PATCH) are never retried on any HTTP response to avoid silent duplication.
- Prevent the summary container from consuming vertical space when empty (no summary banner present).
- Pin the conversation summary banner above the scrollable chat area so it stays
  visible regardless of conversation length. The summary now lives in a non-scrolling
  flex child (`#summary-container`) above `#chat`; only the message list scrolls.
- Idle-timeout notice now says "conversation has been compacted" instead of "reset" (the conversation history is preserved, not destroyed).
- Remove dead `idle_reset_seconds` parameter from `ConversationStore.__init__`, `ConversationSettings` config model, and all call-sites; the parameter has been a no-op since the session persistence refactor.
- Drop the governance-policy requirement to mirror `agent_instruction` verbatim in
  `docs/configuration.md` — the full multi-paragraph literal is impractical in a
  Markdown table cell.  The `(long default)` placeholder is the accepted
  representation (rule 4 and rollback procedure updated). (mill: Governance policy requires mirroring agent_instruction in docs/configuration.md but docs use placeholder (20260705T185420Z-governance-policy-requires-mirroring-age-439b))
- Extract duplicated ``owner_id`` query-parameter validation into a shared
  ``_require_owner_id`` helper, reducing duplication across session list,
  delete, and close endpoints.  Adds a JSON-aware ``HTTPException`` handler
  so validation failures return structured ``{"detail": "..."}`` responses.
- Component roster robustness: empty rosters are no longer cached for the full TTL; the last non-empty roster is preserved as a stale fallback. When the roster is unavailable, `component_request` returns an explicit "empty or unavailable" error instead of the misleading "unknown component_id".
- Pin `robotsix-config` git dependency to full 40-character commit SHA (`424f8ec5140e14e9699b92d5c3755d929625b570`), consistent with the other first-party git dependencies.
- Add `step-security/harden-runner` egress monitoring as the first step in all CI jobs that execute external actions directly (`lockfile`, `pre-commit`, `check-sse-types`, `image-scan`, `check-config-schema`), starting in `egress-policy: audit` mode for runtime supply-chain visibility.
- Consolidate `direct_repo` and `repo_study` modules under a shared `repo/` parent namespace (`src/robotsix_chat/repo/{direct,study}/`).
- Ensure changelog fragments (`changelog.d/*.md`) pushed via `push_direct_repo_branch` always end with a trailing newline, preventing `end-of-file-fixer` pre-commit failures on generated PRs.
- Remove dead re-export layer `src/robotsix_chat/chat/__init__.py` (14 symbols in `__all__`); all consumers import directly from submodule paths (`chat.server`, `chat.events`, `chat.conversation`).
- Refactor `_subsession_worker` main loop: extract `_run_task_turn`, `_run_user_chat_turn`, and `_run_periodic_turn` helper functions so the loop body reads as a clean kind-dispatch table.
- Fix knowledge tool name shorthands in `agent_instruction` prompt and `KnowledgeSettings` docstring to match actual tool names (`append` → `append_to_knowledge_note`, `list_knowledge_note` → `list_knowledge_notes`).
- Default `agent_instruction` no longer includes the "Component access:" section; it is now conditionally injected by `create_agent_from_settings()` only when a `central_deploy.url` roster is configured, so the prompt no longer promises a `component_request` tool in the default out-of-box deployment.)
- Remove dead `_idle_reset_seconds` attribute from `ConversationStore` (parameter retained for caller compatibility).
- Thread conversation `session_id` through memory `recall`/`remember` into cognee's session-memory API so session guidance (goals, rules, preferences) is scoped per-window instead of shared process-global.
- Add unit tests for `MessageIdempotencyStore` (LRU eviction, multi-session isolation)
- Add `_serialize()` / `_deserialize()` hook methods to `JsonStoreBase`, allowing subclasses like `EffectivenessStore` to provide custom serialisation without duplicating the atomic-write persistence pattern.
- Inline the Docs workflow from the `python-docs.yml` reusable workflow (external repo) into
  `.github/workflows/docs.yml`, splitting into separate build and deploy jobs. The build job runs
  on PRs too for early regression detection. The deploy job uses `continue-on-error: true` as
  a fallback for transient GitHub Pages infrastructure errors ("Deployment failed, try again
  later."). This is a restructuring that makes the full workflow visible and manageable within
  this repo; it does not fix the specific Pages infra flake.
- Add unit tests for the CLI entry point module (`tests/chat/test_cli.py`):
  `_configure_logging`, `_setup_observability`, `run_server`, and
  `run_server_from_config` — covering structlog wiring, Langfuse tracing
  fallback, uvicorn invocation, and full startup wiring. (mill: test gap: add unit tests for src/robotsix_chat/chat/server/cli.py (20260704T183942Z-test-gap-add-unit-tests-for-src-robotsix-d155))
- Register the deploy-lifecycle API as a read-only component:
  four new tools — ``list_lifecycle_services``,
  ``get_lifecycle_service_status``, ``get_lifecycle_service_config``,
  ``get_lifecycle_service_env`` — let the agent inspect the
  central-deploy lifecycle server (service inventory, status/health,
  config/env with secrets masked). Mutation endpoints are deliberately
  excluded. Config key: ``lifecycle``.
- Added a conversation summary banner at the top of the chat window. The summary is regenerated after each assistant turn and shows the session purpose, pending work, pending questions, blockers, and relevant info at a glance. The banner is collapsible and gracefully hides empty sections.)
- `message_subsession` and `close_subsession` now accept truncated (8-char prefix) subsession IDs as displayed by `list_subsessions`, fixing "No subsession in this conversation's tree" errors when the agent passes IDs shown in the listing. (mill: message_subsession/close_subsession fail with 'not in this conversation's tree' for a subsession that list_subsessions reports (20260704T144024Z-message-subsession-close-subsession-fail-9671))
- Split `routes.py` (850 lines) into a `routes/` package with focused modules:
  `constants.py`, `_shared.py`, `chat.py`, `events.py`, `sessions.py`,
  `subsessions.py`, `errors.py` — each holding a single responsibility.
- Fix two lifecycle bugs in the periodic subsession scheduler: first-run
  duplicate execution (now guarded by a persisted `completed_runs` set
  checked atomically via `claim_run`) and zombie subsessions where the
  tree record is lost but the timer survives (added `reap_orphans` reaper
  and post-wakeup liveness checks).  `spawn_subsession` and `create` are
  now idempotent — duplicate spawn/resume races cannot launch a second
  worker.  `complete_subsession` fails loudly (error returned to the
  agent) when the subsession is no longer active.
- Fix: in-flight assistant response is persisted to conversation history even when the client disconnects mid-stream (page reload, conversation switch). The SSE stream now lets the background producer task complete independently of the client connection.
- On desktop viewports (≥768px), opening the sessions or subsessions panel now shifts the central conversation column aside instead of overlaying it. Closing the panel restores full width with a smooth CSS transition. Narrow screens keep the overlay behaviour.
- Eliminate duplicated code between `fetch_roster` and `fetch_roster_sync` in `component_access.roster` — the sync variant now delegates to the async version via `asyncio.run()`.
- Exclude auto-generated CHANGELOG.md from the typos spell-check pre-commit hook to
  eliminate false positives on hyphen-separated issue reference slugs.
- Log resolved persistence paths at startup (conversation, knowledge, memory, diagnostics, subsessions) so a volume-mount mismatch is immediately visible in logs.
- Default `server_host` to `0.0.0.0` (bind all interfaces) instead of `127.0.0.1` — inside a
  container the loopback default causes silent gateway 502. Add a persistent named config volume in
  `deploy/docker-compose.yml` so the operator-managed config survives image updates.
- Migrate logging from hand-written text format to structlog-based JSON logging. All existing
  `logging.getLogger(__name__).info(...)` calls continue to work unchanged; the `ProcessorFormatter`
  bridge handles stdlib loggers transparently. A new `log_json_format` setting (default `True`) lets
  operators switch back to human-readable console output for local development.
- Removed stale `.gitmodules` file referencing the deleted `broker_src` submodule, and updated a
  leftover Dockerfile comment that mentioned the removed broker extra.
- Removed broker-related subsystem documentation from `docs/configuration.md` (Mill, Calendar,
  Component Agent, Skills) and updated Component Client description to reflect direct HTTP
  transport.
- Complete the broker-removal cleanup: fix broken `_mill_cache` import in `agent.py` (deleted
  `mill/` package), bump system prompt v15→v16 (remove `consult_mill` references, delete
  calendar/task tools section), and purge stale broker references from `AGENT.md`,
  `docs/configuration.md`, `docs/modules.yaml`, `docs/user-guide/deployment.md`, and
  `docs/system_prompt_changelog.md`.
- Replace broker-based mill, board, calendar, component-agent, and skills modules with a generic
  `component_access` mechanism that fetches the central-deploy roster (`GET /chat/components`),
  loads each component's skill into the agent, and exposes a single
  `component_request(component_id, method, path, json_body=None)` tool. Remove the `broker` extra,
  `robotsix-agent-comm` and `robotsix-board-agent` dependencies, `MillSettings`, `BoardSettings`,
  `CalendarSettings`, `ComponentAgentSettings`, and `SkillsSettings` config models. Add
  `CentralDeploySettings` (`url`, `api_token`, `roster_cache_ttl`).
- Replace hand-rolled `.github/workflows/lint-workflows.yml` with thin delegation wrapper calling
  `damien-robotsix/robotsix-github-workflows/.github/workflows/lint-workflows.yml` (shared
  reusable). Enables `run-actionlint`, `run-zizmor`, and `sarif-workflows` inputs.
- CI schema guard ensures `config/config.schema.json` stays in sync with the Settings model. Deploy
  compose updated to JSON config (`config/config.json`, `ROBOTSIX_CONFIG_FILE`). Documentation
  rewritten for single-JSON-file config (no env-var overlay). Breaking-change towncrier fragment
  with ops cutover table added.
- Document the deterministic-source auto-approve fast-path triage boilerplate in the
  `triage_boilerplate` periodic workflow marker.
- Remove the deprecated robotsix-agent-comm broker integration: delete `broker_client.py`, the
  `mill/`, `calendar/`, `component_agent/`, and `skills/` packages, the `broker` extra from
  `pyproject.toml`, and all associated config models (`MillSettings`, `CalendarSettings`,
  `ComponentAgentSettings`, `SkillsSettings`), env builders, and broker-credential validation from
  `Settings`. The broker is deprecated fleet-wide; its role will be re-absorbed into central-deploy
  management in a future ticket.
- Enable triage_boilerplate periodic workflow for automated triage boilerplate response templates.
- Extract `_fetch_json(repo, path, action)` private helper in `RefDocsClient` to deduplicate the
  allowlist-check + URL-build + fetch preamble shared by `read_file` and `list_files`.
- Extract repeated `env_set` closures from `env_builders` `_build_*_raw()` functions into a
  module-level `_env_set()` helper.
- Extract `ConversationStoreSerializer` class from `ConversationStore`, decoupling file I/O and
  format handling from the in-memory session/owner lifecycle.
- Add `SUBSESSIONS_TRANSCRIPT_MAX_ENTRIES` env var override for
  `SubsessionsSettings.transcript_max_entries` (was previously only settable via YAML).
- Migrate from YAML config (`robotsix-yaml-config`, `config/chat.local.yaml`) to JSON config via
  `robotsix-config` (`config/config.json`, located by `ROBOTSIX_CONFIG_FILE`). All secret fields are
  now `SecretStr`; the environment-variable overlay is removed. `LangfuseSettings` sub-model added
  to both the top-level `Settings` and `MemorySettings`. Config fields that were `str` are now
  `SecretStr`: `llmio_api_key`, `MemoryLlmSettings.api_key`, `MemoryEmbeddingSettings.api_key`, all
  `broker_token` fields, `api_token`, `github_token`, `github_app_private_key`, and
  `board_api_token`. Langfuse credentials are exported to process env at startup (per
  component-standard). The `CHAT_CONFIG_PATH` env var is replaced by `ROBOTSIX_CONFIG_FILE`.
- Fixed `docs/configuration.md` `llmio.model_level` default column from `4` to `3` to match the
  pydantic field default. Added CI test to catch future docs-vs-code default mismatches.
- Cognee's litellm LLM calls (cognify + recall) are now traced in Langfuse via the OTLP-based
  `langfuse_otel` callback, using dedicated `MEMORY_LANGFUSE_*` credentials for the
  `robotsix-chat-cognee` project. Both success and failure callbacks are wired, an OTLP import guard
  provides a clear diagnostic when the `tracing` extra is absent, and `component:cognee` default
  tags allow in-project trace filtering.
- Memory: wire litellm's Langfuse callback with dedicated cognee credentials
  (`MEMORY_LANGFUSE_PUBLIC_KEY` / `MEMORY_LANGFUSE_SECRET_KEY`) so internal LLM traffic lands in a
  separate `robotsix-chat-cognee` project. Graceful no-op when creds are absent.
- Pin `@anthropic-ai/claude-code` npm version to `2.1.199` in Dockerfile (resolves hadolint DL3016).
- Move persistent-data mount from `/home/app/.data` to `/data` per round-4 container standard. All
  code-level path defaults (memory data_dir, diagnostics store/proposals/effectiveness, knowledge
  path, subsessions store_path, conversation persist_path) now use absolute `/data/…` paths.
- Align `.pre-commit-config.yaml` to standard hook set: convert `actionlint` and `hadolint` from
  local hooks to their official pre-commit mirrors (`rhysd/actionlint` v1.6.24, `hadolint/hadolint`
  v1.19.0), fix trailing YAML corruption. `check-json`, `detect-private-key` already present;
  `bandit` already removed.
- Dockerfile: add `SHELL` with pipefail and pin apt package versions for hadolint compliance.
- Dockerfile: change `APP_UID`/`APP_GID` ARG defaults from 1001 to 1000 to align with the
  robotsix-standards 2026-07 revision (central-deploy overrides the container user to the
  deploy-host operator uid:gid; the 1000 default matches the common `debian` operator). **One-time
  volume migration required for existing deployments:** before redeploying, run
  `docker run --rm -v chat-data:/data busybox chown -R 1000:1000 /data` on the deploy host to re-own
  the persistent `chat-data` volume contents. Without this step, `.data/conversations.json` writes
  will fail with PermissionError.
- Silence bandit false positives in `scripts/check_modules_registry.py` (B404, B603, B607),
  `src/robotsix_chat/chat/server/routes.py` (B105), and `src/robotsix_chat/mill/retry_queue.py`
  (B311) with `# nosec` comments; update `.secrets.baseline` for the `.pre-commit-config.yaml` typos
  rev SHA.
- Dockerfile: migrate from `/opt/venv` copy pattern to canonical `uv export --frozen` +
  `uv pip install --system` pattern, installing directly into the runtime image's system Python.
  Removes the builder-stage virtualenv indirection; build-only tooling (git, uv binary) is pruned
  from the final image.
- Migrated deploy-compose app config and secrets from `environment:` slots to the mounted config
  file (`robotsix.deploy.config-target` label). `deploy/docker-compose.yml` `environment:` now
  carries only infrastructure wiring (`CHAT_CONFIG_PATH`); a committed `deploy/config.example.yaml`
  replaces the old per-key env slots.
- Add `hypothesis` dev dependency and property-based roundtrip tests for Pydantic config models
  (`AuthSettings`, `Settings`), catching validation edge cases in combinatorial field interactions.
- Add Dependabot auto-merge caller workflow (`.github/workflows/dependabot-auto-merge.yml`).
- Reorganize test directory for `robotsix_chat.board` module: move tests from `tests/board_reader/`
  to `tests/board/` to match the per-module naming convention after the module rename (PR #367).
- Register `tests/common/subsession_fakes.py` under the `robotsix_chat.common` module.
- Extract `_close_and_publish` helper from four terminal-state methods (`mark_closed`,
  `cancel_and_close`, `fail`, `mark_interrupted`) in `SubsessionRegistry`, removing ~30 lines of
  duplicated SSE/persist logic.
- Removed stale `pip-audit` dev dependency and updated documentation references to `uv audit` (PR
  #349 cleanup).
- Extract shared `_entry_to_common_kwargs` helper in `subsessions/worker.py`, deduplicating the
  7-field entry-mapping block used by both `spawn_subsession` and `SubsessionInfo` construction in
  resume/restore code.
- Extract `_resolve_subsession` helper to deduplicate subsession-registry lookup boilerplate across
  four route handlers (`subsessions_get_endpoint`, `subsessions_transcript_endpoint`,
  `subsessions_message_endpoint`, `subsessions_close_endpoint`).
- Migrate board reader to shared `BoardHTTPClient` from `robotsix-board-agent`, replacing the
  standalone `BoardReader` class. Board tools (`list_board_tickets`, `read_board_ticket`,
  `create_board_ticket`) now use `ErrorStrategy.RETURN` for never-raise error handling with TTL
  caching. Removed `BoardReaderSettings` in favour of `BoardSettings`; the `board_reader` config key
  and `BOARD_READER_*` env vars remain unchanged.
- Rename `spawn_subsession_tool` to `spawn_subsession` (via `__name__` / `__qualname__` mutation) so
  the LLM-visible tool name matches the system prompt.
- Add link to robotsix-standards in README.md and AGENT.md
- DRY repetitive validation and builder boilerplate in `Settings`: extract `_require_broker_creds`
  and `_require_min` helpers for `model_post_init`, and replace 16 builder blocks and 5 `_parse_int`
  blocks in `_build` with dict-driven loops.
- Replace monkeypatch-based httpx mocking with `respx` (httpx's official transport-layer mock
  library) across all 7 test modules. Removes ~55-line shared `mock_helpers.py` module and ~16
  inline mock class definitions. `respx` is added to `[dependency-groups] dev`.
- Bump `reviewdog/action-actionlint` from v1.68.0 to v1.72.0 (dependabot PR #354).
- Bump `actions/upload-artifact` from v4.6.2 to v7.0.1 (dependabot PR #352).
- Bump pytest from 9.1.0 to 9.1.1 (dependabot PR #337)
- Migrate `ConfigContractError` to canonical `robotsix_agent_comm.protocol.ConfigContractError`;
  delete the local definition from `component_agent/config_contract.py`.
- Add OpenSSF Scorecard GitHub Action workflow (weekly Monday + push to main), uploading SARIF
  results to the security tab for supply-chain posture scoring.
- Generate CycloneDX SBOM at release time and submit to GitHub Dependency Graph; re-enable Docker
  image SBOM attestation in release-image workflow.
- Remove dead `_terminal_result` function from `chat/delegation.py` (superseded by
  `_terminal_state_result` in `chat/loops.py`)

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- towncrier release notes start -->

## [0.5.0] - 2026-08-03

### Features

- Add a Docker registry tag→digest resolver tool for the chat agent ([#20260729T111423Z-add-a-docker-registry-tag-digest-resolve-22ec](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T111423Z-add-a-docker-registry-tag-digest-resolve-22ec))
- Memory is now two-tier: the automatic per-message recall is retrieval-only (`recall_search_type` default changed from `GRAPH_COMPLETION` to `CHUNKS` — no LLM call per turn), and the expensive LLM-mediated graph search became an on-demand `search_memory` tool the agent calls deliberately when the cheap snippets are not enough. Live, the old design ran `GRAPH_COMPLETION` on every message and timed out (90 s) eight times in one observed day — each time stalling the reply and then proceeding memory-less anyway. The tool runs under its own more generous `deep_recall_timeout_seconds` (default 180 s) and returns explanatory messages instead of empty strings on failure, so the model knows why nothing came back. New settings: `deep_recall_search_type`, `deep_recall_timeout_seconds`. ([#20260801T124500Z-memory-cheap-auto-recall-deep-tool](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T124500Z-memory-cheap-auto-recall-deep-tool))
- Subsession and autonomous agents can now READ long-term memory without writing to it. They previously got a `NullMemory` — no recall and no cognify — because letting them cognify every turn around the clock produced the ~$22/day cognee bill. That reasoning only ever applied to the write side: recall is a retrieval-only vector lookup (~0.4 s warm, no LLM call) since the two-tier split, while `remember` is still a multi-minute LLM pipeline that also contends with every concurrent recall. A new `ReadOnlyMemory` wrapper gives background agents full recall plus the on-demand `search_memory` tool while dropping writes, so they benefit from what the main conversation has learned without paying to write it back. Controlled by `memory.background_recall_enabled` (default `True`); set it to `False` to restore the previous all-or-nothing behaviour. The existing `subsession_enabled` / `autonomous_enabled` flags keep their meaning as the write gates. ([#20260801T163000Z-background-agents-read-memory](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T163000Z-background-agents-read-memory))

### Bugfixes

- A chat turn now degrades to another capability tier when the Claude credential expires, instead of failing. Previously the tier fallback was wired to `ClaudeSDKUsageExhaustedError` alone, so an expired OAuth token — which every claudeSDK tier shares, since they all drive the same `claude` CLI against the same `.credentials.json` — took the whole conversation down until someone re-authenticated, even with a healthy OpenRouter key configured.

  `ClaudeSDKAuthError` now triggers the same fallback, with enough reach to walk past every keyless claudeSDK tier to a keyed provider, and the agent forwards its OpenRouter key to fallback tiers that take one (keyless tiers are still called without it). Usage exhaustion keeps its existing single-promotion behaviour. ([#claude-auth-tier-fallback](https://github.com/damien-robotsix/robotsix-chat/issues/claude-auth-tier-fallback))
- The weekly container vulnerability rescan now actually runs. Every run since the workflow was added ended in `startup_failure`, so it never scanned anything and never reported: GitHub refused to start it, which produces no logs and no check runs, leaving nothing to notice.

  A calling job's `permissions:` block replaces the workflow-level block rather than merging with it, so the top-level `contents: read` did not reach the `rescan` job — it granted only `security-events: write`. Since a reusable workflow cannot request more than its calling job was given, and the shared `scan-container.yml` declares both `contents: read` and `security-events: write`, the missing scope killed the call before it began. The job now grants both. ([#rescan-workflow-permissions](https://github.com/damien-robotsix/robotsix-chat/issues/rescan-workflow-permissions))
- The cognee self-heal no longer deletes the knowledge graph on every startup. Cognee 1.4 replaced kuzu with its own embedded ladybug engine, whose graph is a file that writes a `.wal` and never a `.shadow` — so it matched both heal conditions written for kuzu (the orphan-`.wal` rule and the missing-`.shadow` rule) and the live graph was removed each time chat started. A graph holding 564 nodes and 1366 edges was 4 KB and empty immediately after a restart, so only the handful of documents ingested since the last restart were ever visible. Ladybug databases are now identified by their `LBUG` magic header and excluded, alongside the SQLite relational store and the LanceDB vector store; genuine kuzu databases are still healed. ([#20260801T113000Z-cognee-shadow-heal-no-longer-wipes-graph](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T113000Z-cognee-shadow-heal-no-longer-wipes-graph))
- Memory writes are now retried with exponential backoff instead of being parked on the first failure, and one attempt gets 900 s instead of 300 s. The docstring already promised "retries exhausted" for a code path that made exactly one attempt, so every slow cognify silently dropped a conversation into the backlog — 20 consecutive `memory write timed out` in one afternoon, each one an exchange that never reached long-term memory. Cognify is a multi-minute LLM pipeline contending with recall for cognee's stores, so 300 s was simply too short. Only transient faults (timeouts, lock/freeze signatures) are retried; a deterministic error still parks immediately rather than burning minutes of backoff for a guaranteed failure. New settings: `remember_max_attempts` (default 3), `remember_retry_backoff_seconds` (default 30). ([#20260801T153000Z-memory-write-retry-longer-timeout](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T153000Z-memory-write-retry-longer-timeout))

### Misc

- [#drop-deploy-api-key-env](https://github.com/damien-robotsix/robotsix-chat/issues/drop-deploy-api-key-env), [#list-repo-files-accepts-path](https://github.com/damien-robotsix/robotsix-chat/issues/list-repo-files-accepts-path), [#20260801T000333Z-migrate-ticket-poll-s-direct-board-api-f-f73e](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T000333Z-migrate-ticket-poll-s-direct-board-api-f-f73e), [#20260731T001747Z-ci-fix-out-of-scope-ci-failure-pre-commi-bac8](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T001747Z-ci-fix-out-of-scope-ci-failure-pre-commi-bac8), [#20260728T003116Z-add-failure-mode-classification-to-bulk-0839](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T003116Z-add-failure-mode-classification-to-bulk-0839), [#20260728T003118Z-provide-explicit-guidance-for-handling-s-6120](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T003118Z-provide-explicit-guidance-for-handling-s-6120), [#20260728T003504Z-reduce-verbose-re-summarization-in-monit-9774](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T003504Z-reduce-verbose-re-summarization-in-monit-9774), [#20260731T004249Z-ci-fix-out-of-scope-ci-failure-hadolint-de83](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T004249Z-ci-fix-out-of-scope-ci-failure-hadolint-de83), [#20260802T005538Z-prevent-duplicate-subsession-creation-fo-6f8a](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T005538Z-prevent-duplicate-subsession-creation-fo-6f8a), [#20260802T005540Z-correctly-attribute-priority-ticket-clos-f0d0](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T005540Z-correctly-attribute-priority-ticket-clos-f0d0), [#20260731T010441Z-add-knowledge-store-entry-to-shared-para-fa61](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T010441Z-add-knowledge-store-entry-to-shared-para-fa61), [#20260802T010451Z-avoid-redundant-status-polling-that-crea-4130](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T010451Z-avoid-redundant-status-polling-that-crea-4130), [#20260728T010615Z-add-pre-commit-hook-to-auto-correct-sha2-ed7b](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T010615Z-add-pre-commit-hook-to-auto-correct-sha2-ed7b), [#20260727T010651Z-validate-proposed-solutions-against-live-7756](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T010651Z-validate-proposed-solutions-against-live-7756), [#20260727T010655Z-resolve-conflicting-option-a-memory-by-g-f4f2](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T010655Z-resolve-conflicting-option-a-memory-by-g-f4f2), [#20260728T011842Z-ci-failure-release-image-on-main-f6d8](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T011842Z-ci-failure-release-image-on-main-f6d8), [#20260728T011941Z-add-update-changelog-sha256-hook-to-pre-004f](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T011941Z-add-update-changelog-sha256-hook-to-pre-004f), [#20260728T012308Z-wire-update-changelog-sha256-into-pre-co-73cb](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T012308Z-wire-update-changelog-sha256-into-pre-co-73cb), [#20260731T012557Z-add-anti-re-emission-guidance-to-react-p-9c35](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T012557Z-add-anti-re-emission-guidance-to-react-p-9c35), [#20260802T012755Z-settings-ui-agent-instruction-field-shou-989c](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T012755Z-settings-ui-agent-instruction-field-shou-989c), [#20260802T013303Z-autonomous-sessions-self-refine-the-pres-c81c](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T013303Z-autonomous-sessions-self-refine-the-pres-c81c), [#20260731T020625Z-fix-board-api-connectivity-flakiness-in-8a2c](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T020625Z-fix-board-api-connectivity-flakiness-in-8a2c), [#20260731T020625Z-patch-tool-chain-too-restrictive-create-a2e8](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T020625Z-patch-tool-chain-too-restrictive-create-a2e8), [#20260731T020626Z-provide-a-notify-user-tool-or-fallback-f-28af](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T020626Z-provide-a-notify-user-tool-or-fallback-f-28af), [#20260731T020731Z-batch-approval-should-resolve-ticket-ids-32be](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T020731Z-batch-approval-should-resolve-ticket-ids-32be), [#20260731T020731Z-monitor-reporting-awaiting-approval-shou-0e99](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T020731Z-monitor-reporting-awaiting-approval-shou-0e99), [#20260731T020731Z-truncate-long-pr-lists-or-provide-them-a-bb9c](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T020731Z-truncate-long-pr-lists-or-provide-them-a-bb9c), [#20260731T024622Z-fix-stale-only-public-unauthenticated-ur-12ef](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T024622Z-fix-stale-only-public-unauthenticated-ur-12ef), [#20260731T024623Z-agent-md-testing-conventions-any-change-bd62](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T024623Z-agent-md-testing-conventions-any-change-bd62), [#20260731T025738Z-add-test-coverage-for-chat-server-routes-c592](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T025738Z-add-test-coverage-for-chat-server-routes-c592), [#20260731T025738Z-extract-shared-check-preconditions-helpe-fc42](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T025738Z-extract-shared-check-preconditions-helpe-fc42), [#20260729T030932Z-add-mdformat-as-a-dev-dependency-to-elim-bc96](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T030932Z-add-mdformat-as-a-dev-dependency-to-elim-bc96), [#20260731T044329Z-fix-changelog-md-system-prompt-v73-label-fc06](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T044329Z-fix-changelog-md-system-prompt-v73-label-fc06), [#20260731T050133Z-remove-orphaned-hadolint-bin-binary-from-caf3](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T050133Z-remove-orphaned-hadolint-bin-binary-from-caf3), [#20260731T050134Z-agent-md-ci-workflow-conventions-never-c-4dab](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T050134Z-agent-md-ci-workflow-conventions-never-c-4dab), [#20260729T050819Z-extract-shared-parse-json-body-helper-in-fa8e](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T050819Z-extract-shared-parse-json-body-helper-in-fa8e), [#20260729T050819Z-extract-shared-terminal-ticket-close-hel-f6b4](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T050819Z-extract-shared-terminal-ticket-close-hel-f6b4), [#20260730T051112Z-add-test-coverage-for-common-github-auth-2023](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T051112Z-add-test-coverage-for-common-github-auth-2023), [#20260730T051112Z-extract-shared-ok-or-error-helper-from-d-fddc](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T051112Z-extract-shared-ok-or-error-helper-from-d-fddc), [#20260730T051112Z-fix-stale-cors-documentation-in-security-ebca](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T051112Z-fix-stale-cors-documentation-in-security-ebca), [#20260725T051515Z-run-mdformat-on-doc-files-before-doc-sta-ed03](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T051515Z-run-mdformat-on-doc-files-before-doc-sta-ed03), [#20260801T052237Z-autonomous-max-idle-auto-turns-missing-f-bbcc](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T052237Z-autonomous-max-idle-auto-turns-missing-f-bbcc), [#20260801T052237Z-feedback-deploy-api-key-missing-from-fee-b4d0](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T052237Z-feedback-deploy-api-key-missing-from-fee-b4d0), [#20260801T052237Z-memory-config-table-missing-5-recovery-s-f9b0](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T052237Z-memory-config-table-missing-5-recovery-s-f9b0), [#20260801T052237Z-render-url-fleet-auth-missing-from-rende-7a9b](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T052237Z-render-url-fleet-auth-missing-from-rende-7a9b), [#20260802T052246Z-dockerdigestsettings-undocumented-no-doc-078c](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T052246Z-dockerdigestsettings-undocumented-no-doc-078c), [#20260802T052249Z-direct-repo-direct-fix-enabled-missing-f-86ee](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T052249Z-direct-repo-direct-fix-enabled-missing-f-86ee), [#20260802T052249Z-memory-table-missing-5-deep-recall-backg-6487](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T052249Z-memory-table-missing-5-deep-recall-backg-6487), [#20260731T052408Z-agent-md-configuration-config-standard-e-f24a](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T052408Z-agent-md-configuration-config-standard-e-f24a), [#20260722T061630Z-fix-empty-repo-bootstrap-deadlock-by-aut-8b7f](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T061630Z-fix-empty-repo-bootstrap-deadlock-by-aut-8b7f), [#20260728T061925Z-extract-list-subsessions-helper-from-dup-ea4a](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T061925Z-extract-list-subsessions-helper-from-dup-ea4a), [#20260728T061925Z-extract-shared-request-helper-from-dupli-2178](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T061925Z-extract-shared-request-helper-from-dupli-2178), [#20260731T063148Z-reconcile-two-stale-no-merge-capability-074f](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T063148Z-reconcile-two-stale-no-merge-capability-074f), [#20260731T065442Z-fix-stale-max-idle-runs-docs-source-docs-76ea](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T065442Z-fix-stale-max-idle-runs-docs-source-docs-76ea), [#20260730T074059Z-add-langfuseinspectsettings-documentatio-16fd](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T074059Z-add-langfuseinspectsettings-documentatio-16fd), [#20260730T074059Z-remove-stale-timeout-doc-entries-from-gi-35fa](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T074059Z-remove-stale-timeout-doc-entries-from-gi-35fa), [#20260729T075502Z-add-sftpsettings-to-config-init-py-impor-48c4](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T075502Z-add-sftpsettings-to-config-init-py-impor-48c4), [#20260801T082252Z-wire-up-fleet-auth-credential-so-the-cha-f5c5](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T082252Z-wire-up-fleet-auth-credential-so-the-cha-f5c5), [#20260731T084111Z-agent-md-python-conventions-always-write-7254](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T084111Z-agent-md-python-conventions-always-write-7254), [#20260728T092234Z-add-missing-model-config-configdict-extr-2e53](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T092234Z-add-missing-model-config-configdict-extr-2e53), [#20260728T092234Z-remove-dead-config-githubactionssettings-0304](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T092234Z-remove-dead-config-githubactionssettings-0304), [#20260726T092714Z-add-retry-with-justification-support-to-6928](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T092714Z-add-retry-with-justification-support-to-6928), [#20260731T093854Z-wire-ticket-id-resolution-into-merge-pul-99b8](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T093854Z-wire-ticket-id-resolution-into-merge-pul-99b8), [#20260731T100742Z-fix-github-job-log-endpoint-returning-30-bd7c](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T100742Z-fix-github-job-log-endpoint-returning-30-bd7c), [#20260731T100742Z-monitor-subsessions-should-not-auto-paus-23d2](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T100742Z-monitor-subsessions-should-not-auto-paus-23d2), [#20260731T101036Z-fix-patch-direct-repo-file-board-api-and-e5b6](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T101036Z-fix-patch-direct-repo-file-board-api-and-e5b6), [#20260802T101159Z-support-on-close-trigger-for-background-088a](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T101159Z-support-on-close-trigger-for-background-088a), [#20260802T101228Z-surface-silent-pr-closure-and-unmergeabl-ae43](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T101228Z-surface-silent-pr-closure-and-unmergeabl-ae43), [#20260731T101840Z-assistant-conflated-temporarily-unreacha-da31](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T101840Z-assistant-conflated-temporarily-unreacha-da31), [#20260731T101840Z-stale-monitor-auto-pause-notices-are-not-698f](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T101840Z-stale-monitor-auto-pause-notices-are-not-698f), [#20260731T101920Z-prevent-placeholder-hashes-in-credential-075a](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T101920Z-prevent-placeholder-hashes-in-credential-075a), [#20260727T102906Z-ci-failure-ci-on-main-4ea4](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T102906Z-ci-failure-ci-on-main-4ea4), [#20260727T102910Z-ci-failure-release-image-on-main-7229](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T102910Z-ci-failure-release-image-on-main-7229), [#20260729T105016Z-implement-force-resume-command-for-block-7181](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T105016Z-implement-force-resume-command-for-block-7181), [#20260729T105017Z-add-tool-or-capability-for-authenticated-bb3e](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T105017Z-add-tool-or-capability-for-authenticated-bb3e), [#20260729T105022Z-avoid-restating-full-triage-tables-when-4060](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T105022Z-avoid-restating-full-triage-tables-when-4060), [#20260801T111618Z-persist-canonical-reply-style-file-so-th-0f92](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T111618Z-persist-canonical-reply-style-file-so-th-0f92), [#20260801T113544Z-add-frame-assertion-tests-for-subsession-bf26](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T113544Z-add-frame-assertion-tests-for-subsession-bf26), [#20260802T114005Z-robotsix-chat-enable-pin-bump-periodic-w-5146](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T114005Z-robotsix-chat-enable-pin-bump-periodic-w-5146), [#20260801T120000Z-pin-cognee-and-drop-kuzu-heal](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T120000Z-pin-cognee-and-drop-kuzu-heal), [#20260731T121208Z-agent-limitation-the-implement-agent-spe-41da](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T121208Z-agent-limitation-the-implement-agent-spe-41da), [#20260801T122049Z-configurable-autonomous-sessions-named-p-1298](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T122049Z-configurable-autonomous-sessions-named-p-1298), [#20260731T122420Z-monitor-9d6a-yaml-misleading-ticket-id-r-ce90](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T122420Z-monitor-9d6a-yaml-misleading-ticket-id-r-ce90), [#20260731T122421Z-monitor-9d6a-yaml-anomalous-system-promp-f3d4](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T122421Z-monitor-9d6a-yaml-anomalous-system-promp-f3d4), [#20260802T122435Z-periodic-monitor-auto-pause-actually-clo-9d32](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T122435Z-periodic-monitor-auto-pause-actually-clo-9d32), [#20260731T124025Z-auto-continue-prompts-fire-while-subsess-f5c7](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T124025Z-auto-continue-prompts-fire-while-subsess-f5c7), [#20260731T124140Z-copy-paste-1-file-clone-in-ticket-poll-e-27c4](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T124140Z-copy-paste-1-file-clone-in-ticket-poll-e-27c4), [#20260801T125024Z-test-gap-add-dedicated-unit-tests-for-re-4a32](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T125024Z-test-gap-add-dedicated-unit-tests-for-re-4a32), [#20260802T125134Z-surface-human-approval-tickets-as-clear-472e](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T125134Z-surface-human-approval-tickets-as-clear-472e), [#20260730T130836Z-direct-path-tools-ticket-poll-ticket-pol-ca68](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T130836Z-direct-path-tools-ticket-poll-ticket-pol-ca68), [#20260730T130923Z-monitor-stall-guard-on-publish-pipeline-4dd0](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T130923Z-monitor-stall-guard-on-publish-pipeline-4dd0), [#20260730T130926Z-explicit-halt-and-re-scope-confirmation-5f87](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T130926Z-explicit-halt-and-re-scope-confirmation-5f87), [#20260730T130927Z-monitor-auto-pause-on-human-mr-approval-a924](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T130927Z-monitor-auto-pause-on-human-mr-approval-a924), [#20260730T132238Z-chat-agent-capability-auto-recover-a-gre-a816](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T132238Z-chat-agent-capability-auto-recover-a-gre-a816), [#20260731T135132Z-drain-in-flight-messagecoalescer-tasks-d-f9de](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T135132Z-drain-in-flight-messagecoalescer-tasks-d-f9de), [#20260801T140305Z-remove-14-deprecated-dead-directrepoclie-0e2c](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T140305Z-remove-14-deprecated-dead-directrepoclie-0e2c), [#20260801T140306Z-remove-dead-strip-legacy-timeout-model-v-3c4f](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T140306Z-remove-dead-strip-legacy-timeout-model-v-3c4f), [#20260731T140337Z-add-behavioral-tests-for-the-new-apply-p-3ab5](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T140337Z-add-behavioral-tests-for-the-new-apply-p-3ab5), [#20260730T141336Z-pre-existing-hadolint-violations-in-dock-5896](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T141336Z-pre-existing-hadolint-violations-in-dock-5896), [#20260730T141448Z-ci-fix-out-of-scope-ci-failure-hadolint-2ff6](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T141448Z-ci-fix-out-of-scope-ci-failure-hadolint-2ff6), [#20260730T141753Z-add-confirmation-gated-pr-merge-capabili-ebf2](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T141753Z-add-confirmation-gated-pr-merge-capabili-ebf2), [#20260730T142322Z-adopt-internal-per-component-settings-mi-4274](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T142322Z-adopt-internal-per-component-settings-mi-4274), [#20260802T150000Z-canonical-langfuse-credential-block](https://github.com/damien-robotsix/robotsix-chat/issues/20260802T150000Z-canonical-langfuse-credential-block), [#20260724T152212Z-add-ability-to-restore-critical-configur-1786](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T152212Z-add-ability-to-restore-critical-configur-1786), [#20260729T153653Z-improve-subsession-outcome-ingestion-and-2265](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T153653Z-improve-subsession-outcome-ingestion-and-2265), [#20260730T154300Z-hadolint-failures-on-dockerfile-dl3066-d-38d7](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T154300Z-hadolint-failures-on-dockerfile-dl3066-d-38d7), [#20260730T154412Z-ci-fix-out-of-scope-ci-failure-pre-commi-0622](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T154412Z-ci-fix-out-of-scope-ci-failure-pre-commi-0622), [#20260730T160505Z-add-uv-malware-check-to-benchmark-job-s-e14b](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T160505Z-add-uv-malware-check-to-benchmark-job-s-e14b), [#20260731T161321Z-split-directrepoclient-at-repo-direct-cl-5fc7](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T161321Z-split-directrepoclient-at-repo-direct-cl-5fc7), [#20260801T163503Z-split-tests-repo-direct-test-direct-repo-3942](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T163503Z-split-tests-repo-direct-test-direct-repo-3942), [#20260801T163504Z-add-test-coverage-for-src-robotsix-chat-6f86](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T163504Z-add-test-coverage-for-src-robotsix-chat-6f86), [#20260801T163504Z-add-test-coverage-for-src-robotsix-chat-cc00](https://github.com/damien-robotsix/robotsix-chat/issues/20260801T163504Z-add-test-coverage-for-src-robotsix-chat-cc00), [#20260730T163748Z-ci-fix-out-of-scope-ci-failure-hadolint-d5ec](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T163748Z-ci-fix-out-of-scope-ci-failure-hadolint-d5ec), [#20260729T165917Z-extract-shared-format-entries-helper-fro-3daa](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T165917Z-extract-shared-format-entries-helper-fro-3daa), [#20260729T165917Z-extract-shared-retry-with-kuzu-heal-help-6607](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T165917Z-extract-shared-retry-with-kuzu-heal-help-6607), [#20260729T165917Z-extract-shared-sftp-connection-async-con-0854](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T165917Z-extract-shared-sftp-connection-async-con-0854), [#20260729T165917Z-follow-up-deploy-openssf-scorecard-workf-421d](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T165917Z-follow-up-deploy-openssf-scorecard-workf-421d), [#20260728T172054Z-auto-update-failed-for-chat-7ad8](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T172054Z-auto-update-failed-for-chat-7ad8), [#20260731T174638Z-fix-mail-account-add-crash-when-default-601c](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T174638Z-fix-mail-account-add-crash-when-default-601c), [#20260731T174638Z-repeated-stale-monitor-auto-pauses-creat-1c17](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T174638Z-repeated-stale-monitor-auto-pauses-creat-1c17), [#20260728T175226Z-agent-md-configuration-config-standard-w-c2fa](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T175226Z-agent-md-configuration-config-standard-w-c2fa), [#20260730T181039Z-cost-monitor-migration-auto-closes-on-we-3dd4](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T181039Z-cost-monitor-migration-auto-closes-on-we-3dd4), [#20260730T181043Z-monitor-subsession-consolidation-fails-f-1f44](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T181043Z-monitor-subsession-consolidation-fails-f-1f44), [#20260730T181146Z-propagate-operator-consent-through-appro-e038](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T181146Z-propagate-operator-consent-through-appro-e038), [#20260730T181320Z-ticket-description-append-does-not-chang-ca44](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T181320Z-ticket-description-append-does-not-chang-ca44), [#20260731T182448Z-decision-escalations-present-operator-de-c54a](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T182448Z-decision-escalations-present-operator-de-c54a), [#20260730T182744Z-fix-transient-buildx-booting-buildkit-fa-d1e9](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T182744Z-fix-transient-buildx-booting-buildkit-fa-d1e9), [#20260728T190248Z-add-step-security-harden-runner-to-docs-7d3f](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T190248Z-add-step-security-harden-runner-to-docs-7d3f), [#20260728T190248Z-add-test-coverage-for-augment-with-fallb-dba2](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T190248Z-add-test-coverage-for-augment-with-fallb-dba2), [#20260728T190248Z-refactor-github-create-repo-endpoint-and-55c8](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T190248Z-refactor-github-create-repo-endpoint-and-55c8), [#20260731T193027Z-agent-md-direct-repo-tooling-when-adding-0c33](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T193027Z-agent-md-direct-repo-tooling-when-adding-0c33), [#20260729T193226Z-add-load-render-url-skill-and-wire-it-in-c703](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T193226Z-add-load-render-url-skill-and-wire-it-in-c703), [#20260729T193226Z-add-publicfetchsettings-documentation-se-5a9f](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T193226Z-add-publicfetchsettings-documentation-se-5a9f), [#20260729T193226Z-add-sftpsettings-documentation-section-t-50ab](https://github.com/damien-robotsix/robotsix-chat/issues/20260729T193226Z-add-sftpsettings-documentation-section-t-50ab), [#20260731T194144Z-add-happy-path-unit-tests-for-post-confi-ad5b](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T194144Z-add-happy-path-unit-tests-for-post-confi-ad5b), [#20260731T200830Z-pause-auto-retry-monitors-that-block-on-c6b8](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T200830Z-pause-auto-retry-monitors-that-block-on-c6b8), [#20260728T202717Z-autonomous-auto-continue-loop-spams-the-9422](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T202717Z-autonomous-auto-continue-loop-spams-the-9422), [#20260728T202733Z-do-not-mutate-tickets-unless-user-explic-1b80](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T202733Z-do-not-mutate-tickets-unless-user-explic-1b80), [#20260728T202734Z-consolidate-subsession-summaries-into-a-c7d8](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T202734Z-consolidate-subsession-summaries-into-a-c7d8), [#20260731T202920Z-agent-md-testing-conventions-test-files-041b](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T202920Z-agent-md-testing-conventions-test-files-041b), [#20260728T202952Z-add-diff-patch-capable-file-editing-tool-f2ca](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T202952Z-add-diff-patch-capable-file-editing-tool-f2ca), [#20260728T204530Z-chat-agent-direct-path-tools-ticket-poll-aaff](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T204530Z-chat-agent-direct-path-tools-ticket-poll-aaff), [#20260728T204724Z-add-read-only-triage-capability-to-diagn-d0aa](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T204724Z-add-read-only-triage-capability-to-diagn-d0aa), [#20260728T204724Z-monitor-for-mill-ticket-9d6a-blocked-on-7709](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T204724Z-monitor-for-mill-ticket-9d6a-blocked-on-7709), [#20260731T205526Z-land-tests-stages-test-document-py-from-be99](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T205526Z-land-tests-stages-test-document-py-from-be99), [#20260728T210505Z-monitor-stale-completion-logic-prevents-a062](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210505Z-monitor-stale-completion-logic-prevents-a062), [#20260728T210505Z-reset-implement-spawn-counter-tool-targe-ad8b](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210505Z-reset-implement-spawn-counter-tool-targe-ad8b), [#20260728T210505Z-triage-chat-decision-content-not-forward-8290](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210505Z-triage-chat-decision-content-not-forward-8290), [#20260728T210615Z-give-chat-agent-a-way-to-browse-authenti-43bb](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210615Z-give-chat-agent-a-way-to-browse-authenti-43bb), [#20260728T210855Z-avoid-duplicate-ticket-creation-by-check-7b0b](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210855Z-avoid-duplicate-ticket-creation-by-check-7b0b), [#20260728T210855Z-document-central-deploy-allow-chat-acces-6e0c](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210855Z-document-central-deploy-allow-chat-acces-6e0c), [#20260728T210856Z-add-loop-guard-to-periodic-monitors-chec-5679](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210856Z-add-loop-guard-to-periodic-monitors-chec-5679), [#20260728T210856Z-handle-blocked-closed-transition-when-pr-c578](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T210856Z-handle-blocked-closed-transition-when-pr-c578), [#20260731T213327Z-robotsix-chat-enable-mypy-baseline-perio-0226](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T213327Z-robotsix-chat-enable-mypy-baseline-perio-0226), [#20260728T222711Z-fix-two-shipped-bugs-in-sftpclient-priva-68ab](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T222711Z-fix-two-shipped-bugs-in-sftpclient-priva-68ab), [#20260730T223948Z-fix-inconsistent-board-api-endpoint-reac-5ea7](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T223948Z-fix-inconsistent-board-api-endpoint-reac-5ea7), [#20260730T223950Z-add-update-pr-branch-capability-for-push-5a40](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T223950Z-add-update-pr-branch-capability-for-push-5a40), [#20260730T223954Z-auto-detect-and-fix-ruff-format-violatio-a04f](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T223954Z-auto-detect-and-fix-ruff-format-violatio-a04f), [#20260730T223954Z-never-trust-paraphrased-ticket-ids-from-597e](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T223954Z-never-trust-paraphrased-ticket-ids-from-597e), [#20260731T225619Z-test-test-completion-suppressed-feedback-b626](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T225619Z-test-test-completion-suppressed-feedback-b626), [#20260730T225712Z-add-a-pr-merge-capability-or-auto-merge-9366](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T225712Z-add-a-pr-merge-capability-or-auto-merge-9366), [#20260730T225712Z-improve-monitor-subsession-consolidation-01c3](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T225712Z-improve-monitor-subsession-consolidation-01c3), [#20260728T230952Z-fix-apply-patch-0-0-hunk-bug-silent-cont-2f67](https://github.com/damien-robotsix/robotsix-chat/issues/20260728T230952Z-fix-apply-patch-0-0-hunk-bug-silent-cont-2f67), [#20260730T231318Z-add-tool-to-merge-pull-requests-or-enabl-2cbe](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T231318Z-add-tool-to-merge-pull-requests-or-enabl-2cbe), [#20260730T231319Z-fix-reset-implement-spawn-counter-tool-r-4cd6](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T231319Z-fix-reset-implement-spawn-counter-tool-r-4cd6), [#20260730T232905Z-chat-tools-patch-direct-repo-file-direct-526d](https://github.com/damien-robotsix/robotsix-chat/issues/20260730T232905Z-chat-tools-patch-direct-repo-file-direct-526d), [#20260731T235525Z-notify-operator-on-subsession-auto-pause-bacb](https://github.com/damien-robotsix/robotsix-chat/issues/20260731T235525Z-notify-operator-on-subsession-auto-pause-bacb)


## [0.4.0] - 2026-07-27

### Features

- Autonomous sessions gain an `autonomous.auto_approve` option: when enabled, a drafted plan skips the human approval gate and execution begins immediately (including auto-approving sessions that resume in `awaiting_approval` after a restart). Default stays `False` (safe). The `autonomous`, `self_review`, `notification`, `render_url`, and `http_probe` capabilities now default to enabled so autonomy is the durable default. ([#20260725T013954Z-autonomous-auto-approve-and-default-capabilities](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T013954Z-autonomous-auto-approve-and-default-capabilities))
- Live-reattach an in-progress assistant turn when switching between chat sessions. The foreground `/chat` turn is now mirrored onto the `/events` channel (`chat_turn_started` / `chat_token` / `chat_turn_done`), and the EventBus buffers the current turn per session so a view that connects mid-turn replays what has streamed so far (`chat_turn_resume`) and then follows the live tokens. Switching to a session that is mid-turn now shows the ongoing response streaming (instead of a blank/idle composer that lost the turn), and queued messages stay queued behind it and dispatch only once it completes — fixing the "queued vs. ongoing message" tangle on session switch. A second browser tab on the same session also renders the turn live. ([#20260724T132912Z-live-reattach-ongoing-turn-on-session-switch](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T132912Z-live-reattach-ongoing-turn-on-session-switch))

### Bugfixes

- Cognee memory now detects a frozen store (the recurring orphaned
  LanceDB/sqlite lock — recall timing out, writes failing) and recovers instead
  of silently degrading to "no memory" until a human restarts the container. The
  freeze is surfaced loudly — an ``ERROR`` log and a ``degraded`` flag on
  ``GET /health`` (which stays ``status: ok`` so liveness is unaffected) — and,
  once it persists past ``memory.frozen_store_recovery_minutes`` (default 15), a
  guarded self-restart is triggered (the proven remedy), rate-limited by
  ``memory.recovery_cooldown_minutes`` (default 30) so it cannot restart-loop.
  Auto-recovery is on by default and can be disabled via
  ``memory.auto_recovery_enabled``. ([#cognee-freeze-auto-recovery](https://github.com/damien-robotsix/robotsix-chat/issues/cognee-freeze-auto-recovery))
- Register autonomous sessions in the conversation store on create and reconcile them on resume, so they appear in `list_sessions` for their owner and survive restarts (previously the runner only registered them globally, leaving them absent from `conversations.json` and invisible in the UI). ([#20260725T020024Z-autonomous-sessions-invisible-after-restart](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T020024Z-autonomous-sessions-invisible-after-restart))
- Fix autonomous sessions being invisible after completion and leaving an un-closable empty "New chat" behind: deleting/closing an autonomous run now purges the runner and auto-starts a fresh one (auto-restart always), the autonomous pseudo-owner no longer spawns empty session husks, and the UI resolves each session's true owner so delete/close never 404s. ([#20260727T075622Z-fix-autonomous-session-lifecycle-and-closab](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T075622Z-fix-autonomous-session-lifecycle-and-closab))
- Surface autonomous sessions in the chat UI and let the operator interact with them. The session list only fetched the browser client's own sessions (`owner_id=<clientId>`), so autonomous sessions — owned by the fixed `autonomous` pseudo-owner — never appeared, even though the backend registered and served them. `fetchSessions` now also fetches the autonomous-owned sessions and merges them, and every per-session request (history, event stream, message send, summary, delete) resolves each session's real owner via a new `ownerFor()` helper. The operator can now open an autonomous session, read its proposed plan, and reply to approve it (the `proposal → executing` flow). ([#20260726T080106Z-autonomous-sessions-visible-in-ui](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T080106Z-autonomous-sessions-visible-in-ui))
- Gate cognee long-term memory off for unattended background agents. `memory.enabled` alone only ever gated the interactive main-chat agent, but subsession workers (task/periodic/user_chat) and the autonomous auto-continue runner also recalled + cognified on every turn — running around the clock, they drove cognee cost to ~$22/day (~3,100 LLM calls/day, most of it overnight with no user present). Two new settings, `memory.subsession_enabled` and `memory.autonomous_enabled` (both default `false`), give those agents a `NullMemory` unless explicitly opted in. The main chat agent is unchanged and keeps full memory. ([#20260725T104814Z-gate-cognee-memory-background-agents-9f3a](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T104814Z-gate-cognee-memory-background-agents-9f3a))
- Fix the cognee frozen-store auto-recovery self-restart, which never worked. `LifecycleClient.self_restart` posted to `POST /self/restart`, but central-deploy exposes no such route — and chat's `lifecycle.base_url` was empty, so the call failed client-side with "URL is missing an http(s) protocol" before it left the container. `self_restart` now targets `POST /chat/services/{name}/restart`, naming this service via a new `lifecycle.service_name` config field, and returns a clear message when that field is unset. This restores the guarded auto-recovery that restarts chat when the cognee LanceDB vector store freezes (orphaned worker), instead of leaving memory degraded until a manual restart. ([#20260724T142020Z-fix-cognee-autorecovery-self-restart](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T142020Z-fix-cognee-autorecovery-self-restart))
- Fix two error-banner issues on session switch. (1) The `#summary-container` overlay (absolute, `z-index: 10`) sat over the in-flow `#error-banner`, which had no stacking order — so real error messages were hidden behind the conversation summary; the opaque error banner now sits above it (`z-index: 11`). (2) Switching sessions aborts the in-flight `/chat` POST on purpose (its turn re-attaches via `/events`), but the abort leaked through the SSE parser's read loop and flashed a spurious "operation was aborted" error; that benign `AbortError` is now swallowed quietly. ([#20260724T153242Z-fix-error-banner-visibility-and-abort](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T153242Z-fix-error-banner-visibility-and-abort))
- Change the default cognee extraction LLM (`memory.llm.model`) from `claude-haiku-4.5` to `openrouter/openai/gpt-5-mini`. cognee runs an extraction/consolidation LLM call per message, so an expensive default silently burns credits (~$20/day observed) whenever the config is reset or clobbered back to defaults. `gpt-5-mini` is cognee's own default, gives reliable json_mode structured output, and costs a fraction of a frontier model. ([#20260724T161248Z-cheap-cognee-llm-default](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T161248Z-cheap-cognee-llm-default))
- Fixed `list_installation_repos` returning only the first 30 repos; now paginates through all installed repos so direct-repo tools (push branch, open PR, security tools) work for every repo in the installation scope. ([#20260724T215613Z-paginate-get-installation-repositories](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T215613Z-paginate-get-installation-repositories))

### Misc

- [#20260727T001240Z-add-capability-to-inspect-langfuse-trace-5bd6](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T001240Z-add-capability-to-inspect-langfuse-trace-5bd6), [#20260727T001245Z-add-tool-to-reset-mill-implement-spawn-c-1880](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T001245Z-add-tool-to-reset-mill-implement-spawn-c-1880), [#20260727T001245Z-guidance-for-hand-authoring-prs-as-escap-1b2e](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T001245Z-guidance-for-hand-authoring-prs-as-escap-1b2e), [#20260721T002404Z-periodic-spawned-decision-chats-fail-to-4684](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T002404Z-periodic-spawned-decision-chats-fail-to-4684), [#20260725T004305Z-automatic-respawn-of-dropped-periodic-mo-78c9](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T004305Z-automatic-respawn-of-dropped-periodic-mo-78c9), [#20260721T010508Z-native-autonomous-session-support-consol-93cd](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T010508Z-native-autonomous-session-support-consol-93cd), [#20260725T010635Z-render-url-subsession-lacks-file-slicing-be06](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T010635Z-render-url-subsession-lacks-file-slicing-be06), [#20260725T010647Z-periodic-subsessions-spawned-from-conver-288d](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T010647Z-periodic-subsessions-spawned-from-conver-288d), [#20260727T010657Z-expose-deploy-image-digest-and-health-st-ae07](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T010657Z-expose-deploy-image-digest-and-health-st-ae07), [#20260725T010741Z-force-log-driven-diagnosis-before-any-wo-e9ff](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T010741Z-force-log-driven-diagnosis-before-any-wo-e9ff), [#20260725T010745Z-retire-stale-recalled-memory-entries-to-8972](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T010745Z-retire-stale-recalled-memory-entries-to-8972), [#20260725T010746Z-unify-periodic-sub-session-summaries-int-6dc6](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T010746Z-unify-periodic-sub-session-summaries-int-6dc6), [#20260725T010756Z-add-missing-ticket-unreachable-entry-to-c9cc](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T010756Z-add-missing-ticket-unreachable-entry-to-c9cc), [#20260725T010844Z-provide-fallback-polling-for-ticket-moni-0b2e](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T010844Z-provide-fallback-polling-for-ticket-moni-0b2e), [#20260724T011749Z-ci-fix-out-of-scope-ci-failure-python-ci-2170](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T011749Z-ci-fix-out-of-scope-ci-failure-python-ci-2170), [#20260725T012916Z-improve-disk-reclaim-rate-limit-handling-0c18](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T012916Z-improve-disk-reclaim-rate-limit-handling-0c18), [#20260725T012917Z-allowlist-a-push-without-github-app-scop-1f31](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T012917Z-allowlist-a-push-without-github-app-scop-1f31), [#20260725T012921Z-prevent-deploy-server-disk-full-state-fr-d6cb](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T012921Z-prevent-deploy-server-disk-full-state-fr-d6cb), [#20260725T020628Z-prevent-duplicate-monitors-for-same-tick-db31](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T020628Z-prevent-duplicate-monitors-for-same-tick-db31), [#20260725T020629Z-avoid-subsessions-that-detect-already-re-ac1f](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T020629Z-avoid-subsessions-that-detect-already-re-ac1f), [#20260725T020629Z-handle-component-request-tool-being-temp-20ce](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T020629Z-handle-component-request-tool-being-temp-20ce), [#20260725T020841Z-render-url-accessibility-tree-extraction-cd37](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T020841Z-render-url-accessibility-tree-extraction-cd37), [#20260725T022915Z-robotsix-chat-remove-dead-internal-perio-9526](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T022915Z-robotsix-chat-remove-dead-internal-perio-9526), [#20260725T030419Z-improve-monitor-resilience-to-missing-co-310a](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T030419Z-improve-monitor-resilience-to-missing-co-310a), [#20260725T030420Z-add-a-tool-to-extract-github-actions-ann-f445](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T030420Z-add-a-tool-to-extract-github-actions-ann-f445), [#20260725T030435Z-add-private-repo-log-capture-to-github-t-f78e](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T030435Z-add-private-repo-log-capture-to-github-t-f78e), [#20260724T033103Z-periodic-monitor-spawns-nested-child-sub-c9c4](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T033103Z-periodic-monitor-spawns-nested-child-sub-c9c4), [#20260724T033311Z-reduce-redundant-recall-of-disproven-dia-1191](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T033311Z-reduce-redundant-recall-of-disproven-dia-1191), [#20260725T035700Z-add-test-coverage-for-ticket-poll-module-d6f3](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T035700Z-add-test-coverage-for-ticket-poll-module-d6f3), [#20260725T040518Z-avoid-re-requesting-approval-for-already-8015](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T040518Z-avoid-re-requesting-approval-for-already-8015), [#20260725T042454Z-remove-or-flag-self-authored-knowledge-n-13b9](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T042454Z-remove-or-flag-self-authored-knowledge-n-13b9), [#20260721T042642Z-add-unit-tests-for-parent-is-periodic-an-8d03](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T042642Z-add-unit-tests-for-parent-is-periodic-an-8d03), [#20260725T044051Z-chat-agent-declares-autonomous-complete-5a1f](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T044051Z-chat-agent-declares-autonomous-complete-5a1f), [#20260725T045105Z-prevent-autonomous-complete-when-subsess-e343](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T045105Z-prevent-autonomous-complete-when-subsess-e343), [#20260725T051201Z-bug-rejected-autonomous-session-subject-63c2](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T051201Z-bug-rejected-autonomous-session-subject-63c2), [#20260725T051201Z-redesign-autonomous-sessions-drop-approv-b89a](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T051201Z-redesign-autonomous-sessions-drop-approv-b89a), [#20260726T051941Z-extract-duplicate-make-bare-request-help-8b05](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T051941Z-extract-duplicate-make-bare-request-help-8b05), [#20260726T051941Z-wire-changelog-job-into-final-ci-gate-in-597d](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T051941Z-wire-changelog-job-into-final-ci-gate-in-597d), [#20260725T052924Z-detect-and-respond-to-repeated-session-i-d55c](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T052924Z-detect-and-respond-to-repeated-session-i-d55c), [#20260725T052924Z-rely-too-heavily-on-fallible-recalled-co-f451](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T052924Z-rely-too-heavily-on-fallible-recalled-co-f451), [#20260726T054214Z-consolidate-duplicate-github-app-token-m-c55f](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T054214Z-consolidate-duplicate-github-app-token-m-c55f), [#20260727T055214Z-deduplicate-check-installation-scope-cal-af98](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T055214Z-deduplicate-check-installation-scope-cal-af98), [#20260727T060355Z-ci-fix-out-of-scope-ci-failure-security-1102](https://github.com/damien-robotsix/robotsix-chat/issues/20260727T060355Z-ci-fix-out-of-scope-ci-failure-security-1102), [#20260720T065229Z-document-mill-merge-now-endpoint-and-add-feda](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T065229Z-document-mill-merge-now-endpoint-and-add-feda), [#20260720T065229Z-improve-handling-of-rebase-conflicts-avo-8b37](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T065229Z-improve-handling-of-rebase-conflicts-avo-8b37), [#20260724T070121Z-ci-failure-release-image-on-main-7ded](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T070121Z-ci-failure-release-image-on-main-7ded), [#20260725T071552Z-avoid-duplicate-terminal-state-reports-f-56c6](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T071552Z-avoid-duplicate-terminal-state-reports-f-56c6), [#20260725T071552Z-suppress-duplicate-background-task-resum-7d30](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T071552Z-suppress-duplicate-background-task-resum-7d30), [#20260725T071745Z-avoid-fabricating-causes-without-validat-0cb1](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T071745Z-avoid-fabricating-causes-without-validat-0cb1), [#20260725T071745Z-background-task-resumption-produces-dupl-2cb7](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T071745Z-background-task-resumption-produces-dupl-2cb7), [#20260725T071745Z-periodic-monitor-should-auto-terminate-a-627b](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T071745Z-periodic-monitor-should-auto-terminate-a-627b), [#20260725T071758Z-autonomous-chat-remplacer-l-approbation-badd](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T071758Z-autonomous-chat-remplacer-l-approbation-badd), [#20260725T071909Z-allow-operator-to-pre-authorize-an-entir-3f27](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T071909Z-allow-operator-to-pre-authorize-an-entir-3f27), [#20260726T073313Z-require-live-endpoint-verification-befor-754d](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T073313Z-require-live-endpoint-verification-befor-754d), [#20260726T073353Z-improve-accuracy-of-interpreting-user-re-86e2](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T073353Z-improve-accuracy-of-interpreting-user-re-86e2), [#20260726T073740Z-bootstrap-ticket-closed-prematurely-when-ee99](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T073740Z-bootstrap-ticket-closed-prematurely-when-ee99), [#20260725T073835Z-add-chat-agent-tool-to-fetch-public-repo-3cb3](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T073835Z-add-chat-agent-tool-to-fetch-public-repo-3cb3), [#20260726T075009Z-queued-message-not-drained-when-session-5b64](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T075009Z-queued-message-not-drained-when-session-5b64), [#20260724T082015Z-ci-fix-out-of-scope-ci-failure-python-ci-3270](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T082015Z-ci-fix-out-of-scope-ci-failure-python-ci-3270), [#20260724T082520Z-ci-fix-out-of-scope-ci-failure-python-ci-a35d](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T082520Z-ci-fix-out-of-scope-ci-failure-python-ci-a35d), [#20260720T083442Z-explicit-operator-approval-gate-for-batc-fd34](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T083442Z-explicit-operator-approval-gate-for-batc-fd34), [#20260724T083610Z-ci-fix-out-of-scope-ci-failure-python-ci-222a](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T083610Z-ci-fix-out-of-scope-ci-failure-python-ci-222a), [#20260724T084044Z-ci-fix-out-of-scope-ci-failure-python-ci-8ebf](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T084044Z-ci-fix-out-of-scope-ci-failure-python-ci-8ebf), [#20260720T090342Z-assert-timestamp-field-in-chat-done-sse-ce0d](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T090342Z-assert-timestamp-field-in-chat-done-sse-ce0d), [#20260722T090356Z-get-sessions-500s-attributeerror-state-h-2a67](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T090356Z-get-sessions-500s-attributeerror-state-h-2a67), [#20260725T090512Z-ci-failure-release-image-on-main-bb53](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T090512Z-ci-failure-release-image-on-main-bb53), [#20260724T090747Z-ci-fix-out-of-scope-ci-failure-ruff-form-fa7f](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T090747Z-ci-fix-out-of-scope-ci-failure-ruff-form-fa7f), [#20260721T091007Z-implement-ci-workflow-edit-checklist-fro-9970](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T091007Z-implement-ci-workflow-edit-checklist-fro-9970), [#20260721T091048Z-incorporate-user-statements-as-ground-tr-86d1](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T091048Z-incorporate-user-statements-as-ground-tr-86d1), [#20260721T091053Z-do-not-ask-for-permission-for-trivial-cl-70b7](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T091053Z-do-not-ask-for-permission-for-trivial-cl-70b7), [#20260721T091054Z-avoid-filing-tickets-for-issues-that-do-6fe3](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T091054Z-avoid-filing-tickets-for-issues-that-do-6fe3), [#20260721T091126Z-recover-monitors-automatically-after-mil-0ee3](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T091126Z-recover-monitors-automatically-after-mil-0ee3), [#20260721T091127Z-avoid-re-monitoring-a-closed-subsession-e38b](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T091127Z-avoid-re-monitoring-a-closed-subsession-e38b), [#20260726T091735Z-backend-user-notifications-when-backgrou-d543](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T091735Z-backend-user-notifications-when-backgrou-d543), [#20260726T091735Z-handle-conflicting-user-instructions-gra-dde5](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T091735Z-handle-conflicting-user-instructions-gra-dde5), [#20260726T092714Z-grant-mill-agent-network-access-or-a-cac-79cd](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T092714Z-grant-mill-agent-network-access-or-a-cac-79cd), [#20260725T093719Z-add-pre-commit-hook-to-auto-regenerate-c-6012](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T093719Z-add-pre-commit-hook-to-auto-regenerate-c-6012), [#20260725T093720Z-agent-md-configuration-config-standard-r-04c9](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T093720Z-agent-md-configuration-config-standard-r-04c9), [#20260722T094153Z-autonomous-sessions-display-nothing-in-t-eaee](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T094153Z-autonomous-sessions-display-nothing-in-t-eaee), [#20260726T094611Z-config-ownership-move-first-party-langfu-6fd6](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T094611Z-config-ownership-move-first-party-langfu-6fd6), [#20260722T095132Z-allow-auto-proceed-after-plan-approval-i-5a6c](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T095132Z-allow-auto-proceed-after-plan-approval-i-5a6c), [#20260722T095132Z-hallucinated-memory-summary-causes-redun-f44a](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T095132Z-hallucinated-memory-summary-causes-redun-f44a), [#20260721T095514Z-autonomous-sub-tickets-closed-empty-beca-e049](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T095514Z-autonomous-sub-tickets-closed-empty-beca-e049), [#20260722T101059Z-autonomous-sessions-lack-subsession-noti-8c9e](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T101059Z-autonomous-sessions-lack-subsession-noti-8c9e), [#20260720T101417Z-correct-mistaken-understanding-of-centra-0b5b](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T101417Z-correct-mistaken-understanding-of-centra-0b5b), [#20260720T101617Z-ci-failure-ci-on-main-ea36](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T101617Z-ci-failure-ci-on-main-ea36), [#20260720T102117Z-deploy-server-deny-list-blocks-chat-agen-a60e](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T102117Z-deploy-server-deny-list-blocks-chat-agen-a60e), [#20260725T102728Z-avoid-ticket-pile-up-for-same-page-itera-108b](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T102728Z-avoid-ticket-pile-up-for-same-page-itera-108b), [#20260725T102728Z-handle-stuck-human-issue-approval-ticket-3195](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T102728Z-handle-stuck-human-issue-approval-ticket-3195), [#20260720T102913Z-ci-failure-release-image-on-main-71d5](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T102913Z-ci-failure-release-image-on-main-71d5), [#20260722T103041Z-autonomous-redesign-single-session-model-cced](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T103041Z-autonomous-redesign-single-session-model-cced), [#20260722T103044Z-show-subsessions-spawned-by-an-autonomou-43fe](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T103044Z-show-subsessions-spawned-by-an-autonomou-43fe), [#20260722T103115Z-add-ability-to-re-mint-github-app-token-d903](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T103115Z-add-ability-to-re-mint-github-app-token-d903), [#20260722T103224Z-add-cross-session-persistent-knowledge-r-b5bb](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T103224Z-add-cross-session-persistent-knowledge-r-b5bb), [#20260724T103412Z-ci-fix-out-of-scope-ci-failure-python-ci-32ee](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T103412Z-ci-fix-out-of-scope-ci-failure-python-ci-32ee), [#20260724T103512Z-ci-fix-out-of-scope-ci-failure-python-ci-d8aa](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T103512Z-ci-fix-out-of-scope-ci-failure-python-ci-d8aa), [#20260720T105244Z-settings-ui-save-drops-unrendered-config-6539](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T105244Z-settings-ui-save-drops-unrendered-config-6539), [#20260725T105412Z-incorrect-advice-on-per-repo-ghcr-token-4895](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T105412Z-incorrect-advice-on-per-repo-ghcr-token-4895), [#20260725T105412Z-periodic-subsession-spawning-restriction-7981](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T105412Z-periodic-subsession-spawning-restriction-7981), [#20260725T105412Z-self-hosted-runner-ci-workflow-should-be-06ad](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T105412Z-self-hosted-runner-ci-workflow-should-be-06ad), [#20260725T105412Z-stale-recalled-memory-data-contaminates-e284](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T105412Z-stale-recalled-memory-data-contaminates-e284), [#20260725T112315Z-implement-tool-to-fetch-public-repo-cont-5a5f](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T112315Z-implement-tool-to-fetch-public-repo-cont-5a5f), [#20260724T112927Z-ci-fix-out-of-scope-ci-failure-python-ci-340f](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T112927Z-ci-fix-out-of-scope-ci-failure-python-ci-340f), [#20260726T113959Z-fix-doc-vs-code-mismatch-paused-monitor-0ee0](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T113959Z-fix-doc-vs-code-mismatch-paused-monitor-0ee0), [#20260720T114054Z-robotsix-chat-enable-agent-check-periodi-2846](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T114054Z-robotsix-chat-enable-agent-check-periodi-2846), [#20260720T114055Z-robotsix-chat-enable-trace-review-period-2d7f](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T114055Z-robotsix-chat-enable-trace-review-period-2d7f), [#20260720T114513Z-use-valid-model-levels-from-settings-ins-5d73](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T114513Z-use-valid-model-levels-from-settings-ins-5d73), [#20260720T114514Z-add-pyright-to-ci-and-pre-commit-alongsi-34e3](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T114514Z-add-pyright-to-ci-and-pre-commit-alongsi-34e3), [#20260720T114514Z-deduplicate-testing-conventions-section-756b](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T114514Z-deduplicate-testing-conventions-section-756b), [#20260721T115357Z-extract-git-push-files-helper-from-dupli-0f03](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T115357Z-extract-git-push-files-helper-from-dupli-0f03), [#20260721T115358Z-add-exclude-newer-dependency-cooldown-to-7591](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T115358Z-add-exclude-newer-dependency-cooldown-to-7591), [#20260721T115358Z-extract-session-metadata-helper-from-dup-edff](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T115358Z-extract-session-metadata-helper-from-dup-edff), [#20260722T115500Z-add-ci-check-for-activity-frame-kind-str-c304](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T115500Z-add-ci-check-for-activity-frame-kind-str-c304), [#20260722T115500Z-extract-shared-github-endpoint-boilerpla-3a14](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T115500Z-extract-shared-github-endpoint-boilerpla-3a14), [#20260723T115921Z-deduplicate-sessions-approve-endpoint-an-28a4](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T115921Z-deduplicate-sessions-approve-endpoint-an-28a4), [#20260723T115921Z-extract-duplicate-stream-then-join-patte-8815](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T115921Z-extract-duplicate-stream-then-join-patte-8815), [#20260723T115925Z-refactor-inject-skills-with-a-table-driv-7b6a](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T115925Z-refactor-inject-skills-with-a-table-driv-7b6a), [#20260724T120624Z-add-ci-check-for-autonomousstate-string-b211](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T120624Z-add-ci-check-for-autonomousstate-string-b211), [#20260724T120624Z-add-ci-check-for-subsessionstatus-string-60fb](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T120624Z-add-ci-check-for-subsessionstatus-string-60fb), [#20260724T120625Z-deduplicate-get-ticket-state-and-get-tic-ae91](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T120625Z-deduplicate-get-ticket-state-and-get-tic-ae91), [#20260725T121043Z-add-makefile-target-for-check-activity-k-4a51](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T121043Z-add-makefile-target-for-check-activity-k-4a51), [#20260725T121043Z-extract-shared-github-app-auth-header-he-cba5](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T121043Z-extract-shared-github-app-auth-header-he-cba5), [#20260723T122041Z-implement-per-precondition-error-reporti-be2f](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T122041Z-implement-per-precondition-error-reporti-be2f), [#20260723T123720Z-queued-messages-not-dispatched-when-sess-16df](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T123720Z-queued-messages-not-dispatched-when-sess-16df), [#20260721T123828Z-detect-and-flag-when-a-ticket-fix-requir-07a0](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T123828Z-detect-and-flag-when-a-ticket-fix-requir-07a0), [#20260725T123937Z-ci-failure-ci-on-main-a41f](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T123937Z-ci-failure-ci-on-main-a41f), [#20260723T123948Z-queued-messages-not-dispatched-on-ui-ses-22f2](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T123948Z-queued-messages-not-dispatched-on-ui-ses-22f2), [#20260723T131638Z-autonomous-auto-start-one-session-on-sta-6fc3](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T131638Z-autonomous-auto-start-one-session-on-sta-6fc3), [#20260724T132037Z-recalled-memory-hallucination-flagged-bu-67ab](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T132037Z-recalled-memory-hallucination-flagged-bu-67ab), [#20260724T132105Z-migrate-robotsix-chat-to-robotsix-http-r-504c](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T132105Z-migrate-robotsix-chat-to-robotsix-http-r-504c), [#20260724T132340Z-assistant-repeats-stale-action-items-fro-aa7c](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T132340Z-assistant-repeats-stale-action-items-fro-aa7c), [#20260724T132340Z-reduce-duplicate-acknowledged-responses-17a9](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T132340Z-reduce-duplicate-acknowledged-responses-17a9), [#20260721T132353Z-add-automatic-pr-merge-verification-befo-2329](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T132353Z-add-automatic-pr-merge-verification-befo-2329), [#20260724T132459Z-fix-self-restart-lifecycle-url-protocol-2c67](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T132459Z-fix-self-restart-lifecycle-url-protocol-2c67), [#20260724T132501Z-reduce-verbose-status-messages-to-essent-c3b8](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T132501Z-reduce-verbose-status-messages-to-essent-c3b8), [#20260721T132558Z-classify-config-settings-as-advanced-per-d016](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T132558Z-classify-config-settings-as-advanced-per-d016), [#20260721T132619Z-add-chat-agent-mutatable-compose-label-s-f397](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T132619Z-add-chat-agent-mutatable-compose-label-s-f397), [#20260720T134307Z-ci-fix-out-of-scope-ci-failure-pre-commi-916b](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T134307Z-ci-fix-out-of-scope-ci-failure-pre-commi-916b), [#20260722T135128Z-improve-background-task-resilience-for-p-61f7](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T135128Z-improve-background-task-resilience-for-p-61f7), [#20260722T135222Z-implement-ci-failure-emission-from-mill-e65e](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T135222Z-implement-ci-failure-emission-from-mill-e65e), [#20260723T135315Z-autonomous-auto-continue-throttle-contin-12f4](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T135315Z-autonomous-auto-continue-throttle-contin-12f4), [#20260722T135418Z-add-auto-init-support-to-repo-creation-t-e94b](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T135418Z-add-auto-init-support-to-repo-creation-t-e94b), [#20260722T135418Z-prevent-periodic-monitors-from-spawning-24b0](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T135418Z-prevent-periodic-monitors-from-spawning-24b0), [#20260722T135419Z-add-guidance-to-system-prompt-for-handli-8e03](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T135419Z-add-guidance-to-system-prompt-for-handli-8e03), [#20260723T135454Z-nested-user-chat-subsession-feedback-not-ca15](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T135454Z-nested-user-chat-subsession-feedback-not-ca15), [#20260722T135509Z-reduce-repetitive-no-change-watcher-outp-1c6a](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T135509Z-reduce-repetitive-no-change-watcher-outp-1c6a), [#20260722T135511Z-improve-terminal-state-notification-conc-70aa](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T135511Z-improve-terminal-state-notification-conc-70aa), [#20260724T135715Z-monitor-stale-state-readback-causes-unne-74be](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T135715Z-monitor-stale-state-readback-causes-unne-74be), [#20260720T141755Z-enable-triage-boilerplate-periodic-workf-b93e](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T141755Z-enable-triage-boilerplate-periodic-workf-b93e), [#20260721T142308Z-robotsix-chat-enable-module-size-periodi-0475](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T142308Z-robotsix-chat-enable-module-size-periodi-0475), [#20260723T142804Z-ci-fix-out-of-scope-ci-failure-python-ci-cbf4](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T142804Z-ci-fix-out-of-scope-ci-failure-python-ci-cbf4), [#20260720T143427Z-guide-user-through-contract-version-head-106c](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T143427Z-guide-user-through-contract-version-head-106c), [#20260724T144018Z-robotsix-chat-migrate-to-consume-robotsi-5da3](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T144018Z-robotsix-chat-migrate-to-consume-robotsi-5da3), [#20260719T144317Z-implement-missing-note-error-helper-extr-e530](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T144317Z-implement-missing-note-error-helper-extr-e530), [#20260725T144624Z-add-regenerate-config-schema-hook-to-pre-61ba](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T144624Z-add-regenerate-config-schema-hook-to-pre-61ba), [#20260723T144954Z-introduce-model-policy-abstraction-for-d-42d5](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T144954Z-introduce-model-policy-abstraction-for-d-42d5), [#20260723T145000Z-prevent-duplicate-subsession-creation-wh-de78](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T145000Z-prevent-duplicate-subsession-creation-wh-de78), [#20260720T145357Z-direct-fix-capability-chat-agent-can-pus-bf50](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T145357Z-direct-fix-capability-chat-agent-can-pus-bf50), [#20260720T145440Z-fix-stale-shared-workflow-sha-refs-on-ma-2d55](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T145440Z-fix-stale-shared-workflow-sha-refs-on-ma-2d55), [#20260721T150646Z-ensure-one-shot-background-tasks-survive-1d08](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T150646Z-ensure-one-shot-background-tasks-survive-1d08), [#20260721T150646Z-fix-ambiguous-ticket-terminal-state-repo-4785](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T150646Z-fix-ambiguous-ticket-terminal-state-repo-4785), [#20260724T151259Z-refactor-autonomous-runner-fixture-to-us-f9ea](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T151259Z-refactor-autonomous-runner-fixture-to-us-f9ea), [#20260724T151304Z-agent-md-testing-conventions-use-class-l-9129](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T151304Z-agent-md-testing-conventions-use-class-l-9129), [#20260725T151840Z-add-test-coverage-for-load-github-skill-8b6d](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T151840Z-add-test-coverage-for-load-github-skill-8b6d), [#20260724T152145Z-enable-autonomous-session-closure-when-b-073b](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T152145Z-enable-autonomous-session-closure-when-b-073b), [#20260724T152237Z-improve-error-handling-and-retry-for-sel-9b69](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T152237Z-improve-error-handling-and-retry-for-sel-9b69), [#20260720T152309Z-move-lifecycle-skill-md-from-docs-to-src-b6fa](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T152309Z-move-lifecycle-skill-md-from-docs-to-src-b6fa), [#20260722T152527Z-add-all-to-render-url-init-py-for-consis-a5db](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T152527Z-add-all-to-render-url-init-py-for-consis-a5db), [#20260722T152528Z-add-missing-sse-notification-type-consta-8a0c](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T152528Z-add-missing-sse-notification-type-consta-8a0c), [#20260721T152617Z-configerror-in-config-constants-py-66-is-e343](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T152617Z-configerror-in-config-constants-py-66-is-e343), [#20260721T152625Z-memory-langfuse-host-config-field-is-dec-a2f0](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T152625Z-memory-langfuse-host-config-field-is-dec-a2f0), [#20260724T152709Z-proactively-notify-user-of-background-ta-11bd](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T152709Z-proactively-notify-user-of-background-ta-11bd), [#20260724T152735Z-include-failed-stopped-task-summaries-in-ae8b](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T152735Z-include-failed-stopped-task-summaries-in-ae8b), [#20260723T153000Z-bump-pypdf-cve](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T153000Z-bump-pypdf-cve), [#20260721T153034Z-agent-limitation-the-trace-has-zero-obse-405d](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T153034Z-agent-limitation-the-trace-has-zero-obse-405d), [#20260720T154405Z-config-config-json-agent-instruction-is-52f7](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T154405Z-config-config-json-agent-instruction-is-52f7), [#20260721T155339Z-module-size-split-src-robotsix-chat-subs-8676](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T155339Z-module-size-split-src-robotsix-chat-subs-8676), [#20260720T155426Z-catch-config-typos-add-extra-forbid-to-a-2c8f](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T155426Z-catch-config-typos-add-extra-forbid-to-a-2c8f), [#20260721T155837Z-closing-guard-paragraph-in-agent-instruc-0956](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T155837Z-closing-guard-paragraph-in-agent-instruc-0956), [#20260721T160100Z-normalize-changelog-fragment-trailing-ne-133f](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T160100Z-normalize-changelog-fragment-trailing-ne-133f), [#20260723T161315Z-land-autonomous-throttle-subsession-gate-b326](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T161315Z-land-autonomous-throttle-subsession-gate-b326), [#20260725T161845Z-add-regenerate-config-schema-hook-to-pre-65cc](https://github.com/damien-robotsix/robotsix-chat/issues/20260725T161845Z-add-regenerate-config-schema-hook-to-pre-65cc), [#20260720T164221Z-cognee-lancedb-worker-oom-killed-under-c-35e6](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T164221Z-cognee-lancedb-worker-oom-killed-under-c-35e6), [#20260721T164456Z-enable-mkdocs-strict-mode-validation-blo-cfbe](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T164456Z-enable-mkdocs-strict-mode-validation-blo-cfbe), [#20260724T164654Z-ci-fix-out-of-scope-ci-failure-uv-audit-5dee](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T164654Z-ci-fix-out-of-scope-ci-failure-uv-audit-5dee), [#20260723T164749Z-docs-configuration-md-agent-instruction-1b48](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T164749Z-docs-configuration-md-agent-instruction-1b48), [#20260724T170307Z-config-config-json-missing-log-json-form-b8d4](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T170307Z-config-config-json-missing-log-json-form-b8d4), [#20260724T170308Z-docs-configuration-md-missing-six-config-d96d](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T170308Z-docs-configuration-md-missing-six-config-d96d), [#20260722T172335Z-consolidate-23-near-duplicate-validation-5cd1](https://github.com/damien-robotsix/robotsix-chat/issues/20260722T172335Z-consolidate-23-near-duplicate-validation-5cd1), [#20260723T172501Z-remove-23-dead-backward-compatibility-re-b541](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T172501Z-remove-23-dead-backward-compatibility-re-b541), [#20260720T173039Z-boilerplate-ci-failure-on-main-triage-re-ef6a](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T173039Z-boilerplate-ci-failure-on-main-triage-re-ef6a), [#20260721T173411Z-add-prompt-guidance-for-self-mutation-bo-0461](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T173411Z-add-prompt-guidance-for-self-mutation-bo-0461), [#20260721T173421Z-add-paging-or-truncation-for-large-ticke-01b8](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T173421Z-add-paging-or-truncation-for-large-ticke-01b8), [#20260726T175820Z-fix-wrong-github-org-and-outdated-python-77ef](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T175820Z-fix-wrong-github-org-and-outdated-python-77ef), [#20260723T181450Z-add-pytest-benchmark-microbenchmarks-for-fccc](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T181450Z-add-pytest-benchmark-microbenchmarks-for-fccc), [#20260726T181850Z-fix-remaining-github-org-url-inaccuracie-f0b1](https://github.com/damien-robotsix/robotsix-chat/issues/20260726T181850Z-fix-remaining-github-org-url-inaccuracie-f0b1), [#20260721T184938Z-autonomous-sessions-backend-exists-but-t-afa8](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T184938Z-autonomous-sessions-backend-exists-but-t-afa8), [#20260724T185056Z-add-github-app-installation-check-before-bb3a](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T185056Z-add-github-app-installation-check-before-bb3a), [#20260724T185058Z-robust-error-recovery-for-background-per-e765](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T185058Z-robust-error-recovery-for-background-per-e765), [#20260724T185842Z-smarter-subsession-reporting-only-surfac-8e33](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T185842Z-smarter-subsession-reporting-only-surfac-8e33), [#20260724T185848Z-fix-version-drift-derive-robotsix-chat-v-b2ae](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T185848Z-fix-version-drift-derive-robotsix-chat-v-b2ae), [#20260720T191051Z-agent-md-configuration-config-standard-a-80ef](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T191051Z-agent-md-configuration-config-standard-a-80ef), [#20260721T191150Z-prevent-autonomous-runner-tests-from-wri-5c05](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T191150Z-prevent-autonomous-runner-tests-from-wri-5c05), [#20260723T194606Z-push-direct-repo-branch-fails-board-stat-e979](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T194606Z-push-direct-repo-branch-fails-board-stat-e979), [#20260721T201656Z-persist-queued-messages-to-backend-keep-cf18](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T201656Z-persist-queued-messages-to-backend-keep-cf18), [#20260719T201925Z-support-setting-github-actions-secrets-v-375a](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T201925Z-support-setting-github-actions-secrets-v-375a), [#20260721T202004Z-ci-failure-release-image-on-main-6234](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T202004Z-ci-failure-release-image-on-main-6234), [#20260721T202138Z-autonomous-sessions-never-start-created-b80d](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T202138Z-autonomous-sessions-never-start-created-b80d), [#20260724T202241Z-reduce-verbosity-avoid-restating-unchang-087a](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T202241Z-reduce-verbosity-avoid-restating-unchang-087a), [#20260720T202314Z-test-gap-add-unit-tests-for-src-robotsix-273e](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T202314Z-test-gap-add-unit-tests-for-src-robotsix-273e), [#20260721T204446Z-test-gap-add-unit-tests-for-src-robotsix-9b91](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T204446Z-test-gap-add-unit-tests-for-src-robotsix-9b91), [#20260721T205430Z-config-clean-cutover-migration-to-robots-8c1e](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T205430Z-config-clean-cutover-migration-to-robots-8c1e), [#20260721T210910Z-add-session-color-and-initial-task-field-f16e](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T210910Z-add-session-color-and-initial-task-field-f16e), [#20260721T210912Z-ensure-ticket-analysis-by-worker-reads-a-3f31](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T210912Z-ensure-ticket-analysis-by-worker-reads-a-3f31), [#20260721T211135Z-always-verify-server-side-capability-by-2bbd](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T211135Z-always-verify-server-side-capability-by-2bbd), [#20260721T211140Z-do-not-assume-a-generic-one-shot-deploy-45a0](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T211140Z-do-not-assume-a-generic-one-shot-deploy-45a0), [#20260723T213410Z-test-gap-add-unit-tests-for-src-robotsix-35c8](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T213410Z-test-gap-add-unit-tests-for-src-robotsix-35c8), [#20260723T213410Z-test-gap-add-unit-tests-for-src-robotsix-5924](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T213410Z-test-gap-add-unit-tests-for-src-robotsix-5924), [#20260721T213811Z-give-repo-study-read-access-to-private-r-d1a1](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T213811Z-give-repo-study-read-access-to-private-r-d1a1), [#20260724T215613Z-paginate-get-installation-repositories-i-1b8b](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T215613Z-paginate-get-installation-repositories-i-1b8b), [#20260720T220420Z-recover-orphaned-drain-snapshots-in-cogn-5d1a](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T220420Z-recover-orphaned-drain-snapshots-in-cogn-5d1a), [#20260721T220535Z-autonomous-kickoff-crashes-asyncio-run-c-daf9](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T220535Z-autonomous-kickoff-crashes-asyncio-run-c-daf9), [#20260723T220807Z-enable-workspace-level-artifact-deletion-214e](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T220807Z-enable-workspace-level-artifact-deletion-214e), [#20260723T220959Z-auto-pause-periodic-monitors-after-n-con-5c4d](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T220959Z-auto-pause-periodic-monitors-after-n-con-5c4d), [#20260723T221002Z-detect-and-escalate-repeated-footprint-b-adb1](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T221002Z-detect-and-escalate-repeated-footprint-b-adb1), [#20260723T221005Z-fix-user-chat-subsession-failure-reporti-3383](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T221005Z-fix-user-chat-subsession-failure-reporti-3383), [#20260723T221006Z-add-monitor-guidance-to-suggest-unpausin-3b4e](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T221006Z-add-monitor-guidance-to-suggest-unpausin-3b4e), [#20260721T221218Z-migrate-chat-direct-repo-github-app-mint-053a](https://github.com/damien-robotsix/robotsix-chat/issues/20260721T221218Z-migrate-chat-direct-repo-github-app-mint-053a), [#20260720T225645Z-decision-chat-subsessions-must-embed-ful-77c1](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T225645Z-decision-chat-subsessions-must-embed-ful-77c1), [#20260720T225921Z-subsession-ui-increase-text-size-for-rea-0451](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T225921Z-subsession-ui-increase-text-size-for-rea-0451), [#20260720T230029Z-add-bootstrap-deadlock-guidance-to-syste-7f94](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T230029Z-add-bootstrap-deadlock-guidance-to-syste-7f94), [#20260723T230118Z-ci-fix-out-of-scope-ci-failure-python-ci-3d1e](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T230118Z-ci-fix-out-of-scope-ci-failure-python-ci-3d1e), [#20260724T231019Z-implement-resume-mechanism-for-paused-pe-006d](https://github.com/damien-robotsix/robotsix-chat/issues/20260724T231019Z-implement-resume-mechanism-for-paused-pe-006d), [#20260719T231543Z-extract-per-ticket-http-client-from-file-7081](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T231543Z-extract-per-ticket-http-client-from-file-7081), [#20260719T231543Z-extract-stale-worker-handling-from-check-3274](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T231543Z-extract-stale-worker-handling-from-check-3274), [#20260719T231656Z-classify-hidden-dotfiles-changelog-d-git-c30f](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T231656Z-classify-hidden-dotfiles-changelog-d-git-c30f), [#20260720T231716Z-add-deploy-server-restart-capability-for-144c](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T231716Z-add-deploy-server-restart-capability-for-144c), [#20260720T231716Z-prevent-false-green-deploy-status-from-m-1fce](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T231716Z-prevent-false-green-deploy-status-from-m-1fce), [#20260720T231718Z-provide-agent-access-to-git-actions-run-d3c4](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T231718Z-provide-agent-access-to-git-actions-run-d3c4), [#20260720T232335Z-suppress-no-change-periodic-monitor-outp-2758](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T232335Z-suppress-no-change-periodic-monitor-outp-2758), [#20260720T232929Z-improve-caretaker-ticket-auto-generation-7021](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T232929Z-improve-caretaker-ticket-auto-generation-7021), [#20260720T233057Z-bug-decision-chats-spawned-by-a-periodic-b620](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T233057Z-bug-decision-chats-spawned-by-a-periodic-b620), [#20260723T235114Z-periodic-monitor-hallucinates-state-tran-a97f](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T235114Z-periodic-monitor-hallucinates-state-tran-a97f), [#20260723T235114Z-side-chat-subsessions-for-user-decisions-903d](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T235114Z-side-chat-subsessions-for-user-decisions-903d), [#20260723T235215Z-adopt-config-ownership-standard-componen-de2c](https://github.com/damien-robotsix/robotsix-chat/issues/20260723T235215Z-adopt-config-ownership-standard-componen-de2c), [#20260720T235252Z-add-unit-tests-for-untested-http-route-h-48bb](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T235252Z-add-unit-tests-for-untested-http-route-h-48bb)

## [0.3.1] - 2026-07-20

### Bugfixes

- Cognee memory recall no longer breaks on every turn: the kuzu shadow-file self-heal was deleting
  cognee's SQLite relational store (`cognee_db`) and LanceDB vector store (`cognee.lancedb`) on every
  startup because they have no companion `.shadow` file, wiping the default user/dataset registry that
  `search()` requires. The heal now only ever removes genuine kuzu graph databases. ([#cognee-shadow-heal-preserves-relational-vector-stores](https://github.com/damien-robotsix/robotsix-chat/issues/cognee-shadow-heal-preserves-relational-vector-stores))
- Periodic subsessions resume with their run counter restored: previously a restart reset the counter
  to 0 while the run guard remembered every executed run, so the worker slept one full interval per
  historical run number ("skipping duplicate") and — with regular restarts — never executed again.
  Duplicate-run collisions now fast-forward instantly, and `max_runs` carries over unchanged instead
  of being shrunk by phantom skips on every restart. ([#periodic-resume-starvation](https://github.com/damien-robotsix/robotsix-chat/issues/periodic-resume-starvation))
- Fixed user replies to subsessions (notably ones spawned by periodic workflows) not appearing until a
  window reload: the subsession view now re-syncs its transcript from the server on send instead of
  depending solely on the SSE echo frame, the `/events` stream no longer enters a permanent 5s
  abort/reconnect loop after a session switch (stale-stream callbacks are now generation-guarded), and
  a 20s read-liveness watchdog recovers zombie `/events` connections after network changes or laptop
  sleep. ([#subsession-echo-sse-loop](https://github.com/damien-robotsix/robotsix-chat/issues/subsession-echo-sse-loop))

### Misc

- [#20260720T001452Z-ci-failure-lint-workflows-on-main-f456](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T001452Z-ci-failure-lint-workflows-on-main-f456), [#20260720T001503Z-ci-failure-release-image-on-main-bb30](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T001503Z-ci-failure-release-image-on-main-bb30), [#20260718T002044Z-robotsix-chat-enable-repo-description-sy-dfb5](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T002044Z-robotsix-chat-enable-repo-description-sy-dfb5), [#20260718T002044Z-robotsix-chat-enable-state-sync-periodic-66bf](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T002044Z-robotsix-chat-enable-state-sync-periodic-66bf), [#20260713T002222Z-unblock-feedback-runner-activation-chat-e6c5](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T002222Z-unblock-feedback-runner-activation-chat-e6c5), [#20260719T002710Z-instrument-mill-ingest-post-in-feedback-0df2](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T002710Z-instrument-mill-ingest-post-in-feedback-0df2), [#20260719T002710Z-prevent-infinite-restart-loops-from-moni-45f4](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T002710Z-prevent-infinite-restart-loops-from-moni-45f4), [#20260719T002716Z-avoid-posting-repetitive-resumed-system-160e](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T002716Z-avoid-posting-repetitive-resumed-system-160e), [#20260719T002716Z-prevent-redundant-ticket-creation-when-a-652b](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T002716Z-prevent-redundant-ticket-creation-when-a-652b), [#20260719T002807Z-improve-clarity-of-system-notices-for-re-1d76](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T002807Z-improve-clarity-of-system-notices-for-re-1d76), [#20260714T003000Z-restart-recovery-re-arm-periodic-subsess-a8a8](https://github.com/damien-robotsix/robotsix-chat/issues/20260714T003000Z-restart-recovery-re-arm-periodic-subsess-a8a8), [#20260719T003037Z-generalize-feedback-pipeline-for-cross-r-ce65](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T003037Z-generalize-feedback-pipeline-for-cross-r-ce65), [#20260719T004452Z-fix-steering-discarding-resume-context-m-a89f](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T004452Z-fix-steering-discarding-resume-context-m-a89f), [#20260713T010440Z-ci-fix-out-of-scope-ci-failure-cve-2026-6336](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T010440Z-ci-fix-out-of-scope-ci-failure-cve-2026-6336), [#20260720T011714Z-ci-failure-codeql-on-main-3a81](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T011714Z-ci-failure-codeql-on-main-3a81), [#20260720T014848Z-add-shellcheck-to-gitignore-to-prevent-a-3710](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T014848Z-add-shellcheck-to-gitignore-to-prevent-a-3710), [#20260720T024238Z-ci-fix-out-of-scope-ci-failure-lint-work-ce08](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T024238Z-ci-fix-out-of-scope-ci-failure-lint-work-ce08), [#20260716T040731Z-robotsix-chat-enable-bc-check-periodic-w-a238](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T040731Z-robotsix-chat-enable-bc-check-periodic-w-a238), [#20260717T042934Z-kuzu-graph-db-open-fails-cognee-graph-la-5859](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T042934Z-kuzu-graph-db-open-fails-cognee-graph-la-5859), [#20260720T063903Z-add-workflow-dispatch-to-all-deploy-work-d3a7](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T063903Z-add-workflow-dispatch-to-all-deploy-work-d3a7), [#20260720T065122Z-handle-ambiguous-single-word-commands-wi-1d61](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T065122Z-handle-ambiguous-single-word-commands-wi-1d61), [#20260720T065230Z-prevent-auto-stopping-of-monitors-on-no-e206](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T065230Z-prevent-auto-stopping-of-monitors-on-no-e206), [#20260720T065249Z-show-timestamp-on-the-last-model-message-7f58](https://github.com/damien-robotsix/robotsix-chat/issues/20260720T065249Z-show-timestamp-on-the-last-model-message-7f58), [#20260718T072104Z-fix-false-unread-highlight-for-active-se-67de](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T072104Z-fix-false-unread-highlight-for-active-se-67de), [#20260718T073849Z-ensure-changelog-fragments-emitted-by-im-a6aa](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T073849Z-ensure-changelog-fragments-emitted-by-im-a6aa), [#20260717T081732Z-kuzu-graph-shadow-file-self-heal-from-pr-dff9](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T081732Z-kuzu-graph-shadow-file-self-heal-from-pr-dff9), [#20260713T102824Z-ci-failure-ci-on-main-c977](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T102824Z-ci-failure-ci-on-main-c977), [#20260717T103958Z-remove-three-redundant-coerce-empty-stri-e9af](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T103958Z-remove-three-redundant-coerce-empty-stri-e9af), [#20260713T104337Z-ci-failure-release-image-on-main-2218](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T104337Z-ci-failure-release-image-on-main-2218), [#20260716T104916Z-robotsix-chat-enable-audit-periodic-work-3f67](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T104916Z-robotsix-chat-enable-audit-periodic-work-3f67), [#20260716T104916Z-robotsix-chat-enable-completeness-check-1f14](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T104916Z-robotsix-chat-enable-completeness-check-1f14), [#20260716T104916Z-robotsix-chat-enable-copy-paste-periodic-0755](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T104916Z-robotsix-chat-enable-copy-paste-periodic-0755), [#20260713T110534Z-ci-fix-out-of-scope-ci-failure-cve-2026-4bbf](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T110534Z-ci-fix-out-of-scope-ci-failure-cve-2026-4bbf), [#20260716T112208Z-add-test-coverage-for-the-ui-module-inde-24da](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T112208Z-add-test-coverage-for-the-ui-module-inde-24da), [#20260716T112208Z-ci-final-gate-missing-check-subsession-k-ce10](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T112208Z-ci-final-gate-missing-check-subsession-k-ce10), [#20260716T112208Z-refactor-create-agent-from-settings-213-c916](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T112208Z-refactor-create-agent-from-settings-213-c916), [#20260716T112208Z-split-subsessions-worker-py-918-lines-in-4f00](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T112208Z-split-subsessions-worker-py-918-lines-in-4f00), [#20260717T112938Z-deduplicate-subs-header-sessions-header-6f06](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T112938Z-deduplicate-subs-header-sessions-header-6f06), [#20260717T112938Z-extract-parse-turns-helper-to-eliminate-d6db](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T112938Z-extract-parse-turns-helper-to-eliminate-d6db), [#20260713T113358Z-ci-fix-out-of-scope-ci-failure-cve-2026-0a38](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T113358Z-ci-fix-out-of-scope-ci-failure-cve-2026-0a38), [#20260718T113640Z-extract-request-json-helper-from-duplica-c2bd](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T113640Z-extract-request-json-helper-from-duplica-c2bd), [#20260718T113645Z-add-missing-docstring-to-evict-overflow-7986](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T113645Z-add-missing-docstring-to-evict-overflow-7986), [#20260718T113645Z-extract-build-transcript-utility-from-du-149c](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T113645Z-extract-build-transcript-utility-from-du-149c), [#20260718T113645Z-remove-orphaned-bandit-config-from-pypro-d4b6](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T113645Z-remove-orphaned-bandit-config-from-pypro-d4b6), [#20260718T113645Z-run-deptry-in-ci-to-catch-unused-missing-b1af](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T113645Z-run-deptry-in-ci-to-catch-unused-missing-b1af), [#20260719T113837Z-extract-missing-note-error-helper-from-d-a41b](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T113837Z-extract-missing-note-error-helper-from-d-a41b), [#20260719T113838Z-add-missing-docstring-to-configure-in-me-8b4c](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T113838Z-add-missing-docstring-to-configure-in-me-8b4c), [#20260719T113839Z-enable-uv-malware-check-on-uv-sync-steps-6344](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T113839Z-enable-uv-malware-check-on-uv-sync-steps-6344), [#20260719T120859Z-implement-missing-note-error-helper-extr-17de](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T120859Z-implement-missing-note-error-helper-extr-17de), [#20260713T121508Z-ci-failure-release-image-on-main-954e](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T121508Z-ci-failure-release-image-on-main-954e), [#20260719T121654Z-simplify-credential-handling-avoid-expos-a275](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T121654Z-simplify-credential-handling-avoid-expos-a275), [#20260719T121739Z-prevent-resume-after-fix-on-stale-worker-88e3](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T121739Z-prevent-resume-after-fix-on-stale-worker-88e3), [#20260719T121740Z-add-post-merge-redeploy-trigger-for-mill-453d](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T121740Z-add-post-merge-redeploy-trigger-for-mill-453d), [#20260719T122334Z-reduce-verbosity-of-periodic-subsession-dece](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T122334Z-reduce-verbosity-of-periodic-subsession-dece), [#20260719T140417Z-unify-api-error-response-envelope-elimin-3a95](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T140417Z-unify-api-error-response-envelope-elimin-3a95), [#20260719T141104Z-deduplicate-known-broken-asyncio-run-err-54ea](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T141104Z-deduplicate-known-broken-asyncio-run-err-54ea), [#20260718T142458Z-summary-panel-causes-layout-shift-and-di-46fd](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T142458Z-summary-panel-causes-layout-shift-and-di-46fd), [#20260719T142532Z-improve-autonomous-recovery-from-redraft-bd29](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T142532Z-improve-autonomous-recovery-from-redraft-bd29), [#20260719T142535Z-feedback-pipeline-derive-allowed-target-5f1c](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T142535Z-feedback-pipeline-derive-allowed-target-5f1c), [#20260718T142550Z-auto-scroll-conversation-to-bottom-on-ne-2954](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T142550Z-auto-scroll-conversation-to-bottom-on-ne-2954), [#20260718T145145Z-configure-ui-corrupts-nested-config-obje-649c](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T145145Z-configure-ui-corrupts-nested-config-obje-649c), [#20260719T145650Z-add-feedbacksettings-and-renderurlsettin-c310](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T145650Z-add-feedbacksettings-and-renderurlsettin-c310), [#20260718T151128Z-remove-stale-bandit-references-from-cont-e5de](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T151128Z-remove-stale-bandit-references-from-cont-e5de), [#20260716T151558Z-self-heal-stale-kuzu-shadow-file-that-cr-abe2](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T151558Z-self-heal-stale-kuzu-shadow-file-that-cr-abe2), [#20260718T152322Z-ui-auto-scroll-conversation-to-bottom-wh-507a](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T152322Z-ui-auto-scroll-conversation-to-bottom-wh-507a), [#20260714T153350Z-robotsix-chat-enable-baseline-periodic-w-1417](https://github.com/damien-robotsix/robotsix-chat/issues/20260714T153350Z-robotsix-chat-enable-baseline-periodic-w-1417), [#20260718T153500Z-atomic-conversation-subsession-persistence](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T153500Z-atomic-conversation-subsession-persistence), [#20260716T153726Z-auto-register-new-python-files-in-docs-m-009c](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T153726Z-auto-register-new-python-files-in-docs-m-009c), [#20260719T154210Z-robotsix-chat-enable-changelog-autofill-d02b](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T154210Z-robotsix-chat-enable-changelog-autofill-d02b), [#20260717T172238Z-guard-cognee-memory-calls-with-timeouts-6f3e](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T172238Z-guard-cognee-memory-calls-with-timeouts-6f3e), [#20260717T172754Z-feedbackrunner-never-emits-feedback-disa-bac2](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T172754Z-feedbackrunner-never-emits-feedback-disa-bac2), [#20260719T173017Z-auto-resolve-idle-human-issue-approval-t-fa08](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T173017Z-auto-resolve-idle-human-issue-approval-t-fa08), [#20260715T173331Z-make-feedbackrunner-runs-observable-in-l-59a6](https://github.com/damien-robotsix/robotsix-chat/issues/20260715T173331Z-make-feedbackrunner-runs-observable-in-l-59a6), [#20260715T181457Z-test-gap-add-unit-tests-for-src-robotsix-2907](https://github.com/damien-robotsix/robotsix-chat/issues/20260715T181457Z-test-gap-add-unit-tests-for-src-robotsix-2907), [#20260718T184436Z-robotsix-chat-enable-docstring-coverage-063f](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T184436Z-robotsix-chat-enable-docstring-coverage-063f), [#20260718T184436Z-robotsix-chat-enable-health-periodic-wor-29b1](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T184436Z-robotsix-chat-enable-health-periodic-wor-29b1), [#20260718T184436Z-robotsix-chat-enable-survey-periodic-wor-0126](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T184436Z-robotsix-chat-enable-survey-periodic-wor-0126), [#20260715T191112Z-reorganize-module-github-align-to-per-mo-ed30](https://github.com/damien-robotsix/robotsix-chat/issues/20260715T191112Z-reorganize-module-github-align-to-per-mo-ed30), [#20260715T191112Z-reorganize-module-lifecycle-align-to-per-02ee](https://github.com/damien-robotsix/robotsix-chat/issues/20260715T191112Z-reorganize-module-lifecycle-align-to-per-02ee), [#20260715T191112Z-reorganize-module-notification-align-to-bf2e](https://github.com/damien-robotsix/robotsix-chat/issues/20260715T191112Z-reorganize-module-notification-align-to-bf2e), [#20260716T192336Z-robotsix-chat-re-enable-copy-paste-perio-4f8c](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T192336Z-robotsix-chat-re-enable-copy-paste-perio-4f8c), [#20260715T193936Z-module-curator-verify-runtime-references-aa94](https://github.com/damien-robotsix/robotsix-chat/issues/20260715T193936Z-module-curator-verify-runtime-references-aa94), [#20260715T193936Z-restore-src-robotsix-chat-github-skill-m-b0a4](https://github.com/damien-robotsix/robotsix-chat/issues/20260715T193936Z-restore-src-robotsix-chat-github-skill-m-b0a4), [#20260716T195953Z-consolidate-modules-github-direct-repo-m-27d3](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T195953Z-consolidate-modules-github-direct-repo-m-27d3), [#20260718T200109Z-extract-title-generation-and-simplify-ne-5661](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T200109Z-extract-title-generation-and-simplify-ne-5661), [#20260718T200109Z-refactor-subsessionregistry-extract-pers-e5ab](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T200109Z-refactor-subsessionregistry-extract-pers-e5ab), [#20260719T201052Z-agent-falsely-claimed-inability-to-merge-d1a3](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T201052Z-agent-falsely-claimed-inability-to-merge-d1a3), [#20260719T201052Z-missing-git-conflict-resolution-tools-b17a](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T201052Z-missing-git-conflict-resolution-tools-b17a), [#20260716T201232Z-copy-paste-2-file-clone-in-github-dedupl-e4cb](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T201232Z-copy-paste-2-file-clone-in-github-dedupl-e4cb), [#20260716T201232Z-copy-paste-2-file-clone-in-notification-a5f3](https://github.com/damien-robotsix/robotsix-chat/issues/20260716T201232Z-copy-paste-2-file-clone-in-notification-a5f3), [#20260717T201423Z-subsession-closure-summary-does-not-trig-a175](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T201423Z-subsession-closure-summary-does-not-trig-a175), [#20260719T201925Z-improve-accuracy-of-historical-ticket-st-11ec](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T201925Z-improve-accuracy-of-historical-ticket-st-11ec), [#20260718T202805Z-chat-ui-auto-scroll-conversation-to-bott-a0a8](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T202805Z-chat-ui-auto-scroll-conversation-to-bott-a0a8), [#20260718T210222Z-automatic-subsession-restart-recovery-wi-2320](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T210222Z-automatic-subsession-restart-recovery-wi-2320), [#20260718T210659Z-feedback-pipeline-fix-mill-ingest-payloa-7bdc](https://github.com/damien-robotsix/robotsix-chat/issues/20260718T210659Z-feedback-pipeline-fix-mill-ingest-payloa-7bdc), [#20260719T221457Z-avoid-stale-no-change-responses-to-monit-dad0](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T221457Z-avoid-stale-no-change-responses-to-monit-dad0), [#20260719T221458Z-prevent-creation-of-duplicate-monitors-f-8af3](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T221458Z-prevent-creation-of-duplicate-monitors-f-8af3), [#20260719T223500Z-add-feedback-repo-ids-config-or-dynamic-b9d1](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T223500Z-add-feedback-repo-ids-config-or-dynamic-b9d1), [#20260719T223500Z-instrument-feedback-pipeline-filing-with-ac4d](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T223500Z-instrument-feedback-pipeline-filing-with-ac4d), [#20260719T223500Z-prevent-duplicate-monitors-and-ticket-du-559e](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T223500Z-prevent-duplicate-monitors-and-ticket-du-559e), [#20260719T224754Z-add-an-http-uptime-render-probe-tool-so-8b03](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T224754Z-add-an-http-uptime-render-probe-tool-so-8b03), [#20260719T230210Z-pass-dedup-key-in-resume-periodic-entry-5f90](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T230210Z-pass-dedup-key-in-resume-periodic-entry-5f90), [#20260719T230325Z-pass-dedup-key-in-resume-periodic-entry-c091](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T230325Z-pass-dedup-key-in-resume-periodic-entry-c091), [#20260717T231658Z-session-sidebar-open-by-default-auto-ref-5102](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T231658Z-session-sidebar-open-by-default-auto-ref-5102), [#20260717T233626Z-subsession-closure-summary-must-trigger-9e6a](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T233626Z-subsession-closure-summary-must-trigger-9e6a), [#20260717T233816Z-chat-ui-llm-generated-session-titles-fix-1f97](https://github.com/damien-robotsix/robotsix-chat/issues/20260717T233816Z-chat-ui-llm-generated-session-titles-fix-1f97), [#20260719T234147Z-update-stale-comment-on-active-dedup-key-2a13](https://github.com/damien-robotsix/robotsix-chat/issues/20260719T234147Z-update-stale-comment-on-active-dedup-key-2a13)

## [0.3.0] - 2026-07-13

### Features

- The typing indicator now shows a `recall_memory` step while the agent searches prior conversation
  context, before the Claude SDK turn even starts. Memory recall runs first in every turn and has been
  observed taking 90+ seconds on its own — previously that whole phase showed nothing but blank dots,
  with no visible activity until the SDK subprocess itself started reporting tool calls. ([#activity-feedback-during-recall](https://github.com/damien-robotsix/robotsix-chat/issues/activity-feedback-during-recall))
- The main chat agent now publishes live tool-call, tool-result, and thinking activity from the
  claudeSDK backend as `activity` frames on the existing `GET /events` SSE channel (see
  `robotsix_llmio`'s new `activity_events()` context manager). The chat UI surfaces this as a caption
  inside the typing indicator (e.g. "🔧 search(...)") instead of only three static dots while a turn is
  in flight. ([#20260707T090000Z-live-claude-sdk-activity-feedback-4a17](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T090000Z-live-claude-sdk-activity-feedback-4a17))
- When a subsession closes (or reports a periodic run result) with the main chat as its parent, the
  main agent now runs a real reaction turn instead of silently stashing the raw summary into history —
  it can comment on, continue from, or acknowledge the outcome, and the reply is pushed live to a
  connected browser as a new `agent_message` SSE frame. Falls back to the old passive record if no
  agent is wired yet or the reaction turn itself fails, so the outcome is never lost.

  The subsessions panel also hides closed/failed/interrupted subsessions by default now (they piled up
  and crowded out running ones) — a "Show closed (N)" toggle in the panel header reveals them on
  demand. ([#20260707T091500Z-subsession-close-reaction-and-hide-toggle-8b2c](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T091500Z-subsession-close-reaction-and-hide-toggle-8b2c))

### Bugfixes

- Bump robotsix-llmio to pick up the claude_sdk binary-content fix: an attached image was stringified
  into a multi-megabyte escaped-byte prompt that stalled the CLI subprocess — sessions with images
  hung showing nothing. Images on the claude_sdk model levels now flatten to a compact placeholder
  (the model still cannot see them; use an OpenRouter vision level for that). ([#bump-llmio-image-stall-fix](https://github.com/damien-robotsix/robotsix-chat/issues/bump-llmio-image-stall-fix))
- Fix idle-timeout compaction splitting one conversation into many sessions: a message posted to an
  already-compacted session id is now rerouted to its continuation session instead of re-compacting
  (the runaway that minted a new session per message), the subsession tree is transferred to the
  continuation session so running work follows the conversation, and the SSE `done` frame now carries
  the effective `session_id` so the UI adopts the continuation immediately. ([#idle-compaction-session-continuity](https://github.com/damien-robotsix/robotsix-chat/issues/idle-compaction-session-continuity))
- Stabilize idle compaction: compact **in place** instead of minting a continuation session per idle
  gap. The session keeps its id and full visible transcript; only the agent-facing replay folds older
  turns into the summary. No more "New chat" husk sessions, no more subsession trees hopping between
  sessions, no client-side session adoption needed (legacy `compacted_into` chains still reroute).
  Compaction is also skipped for conversations with fewer than `compaction_min_turns` (default 3)
  fresh turns, so empty or tiny conversations never trigger the summary agent. Bumps robotsix-llmio
  for native image support on the claude_sdk path: attached images are now sent as base64 image blocks
  via SDK streaming input, so the agent can actually see them. ([#in-place-compaction-and-native-images](https://github.com/damien-robotsix/robotsix-chat/issues/in-place-compaction-and-native-images))
- Switched the cognee memory extraction LLM default from `deepseek-v4-flash` to `claude-haiku-4.5` —
  the DeepSeek model produced malformed JSON under instructor's structured-output prompting, causing
  multi-minute retry stalls after replies. ([#memory-llm-flaky-json](https://github.com/damien-robotsix/robotsix-chat/issues/memory-llm-flaky-json))
- Fix an in-flight chat message occasionally failing to persist a reply: `MessageCoalescer`'s
  background processor task was created via `asyncio.create_task()` without retaining a strong
  reference — the one place in the codebase that didn't follow the established pattern of storing the
  task in a long-lived set with a done-callback. An unreferenced task can be silently
  garbage-collected before it completes, aborting the agent run before the reply is ever recorded. ([#message-coalescer-task-gc](https://github.com/damien-robotsix/robotsix-chat/issues/message-coalescer-task-gc))
- Fix periodic subsessions losing all accumulated context on every chat restart. A subsession worker's
  conversation history (`history: list = []`) was reinitialized from scratch whenever its worker
  restarted — including when a long-running periodic subsession (e.g. a board-monitoring loop) was
  resumed after a deploy — so it had no memory of anything from prior runs. When such a subsession
  then spawned a nested subsession (for example to ask the operator a decision), it couldn't
  accurately convey what had already been asked or decided, forcing repeat questions and pushing the
  nested agent to lean on memory recall instead of real context. Each turn's (input, reply) pair is
  now persisted (`turn_history`, capped like the existing transcript) and replayed to seed the
  worker's history when a periodic subsession resumes, so it picks up where it left off instead of
  starting blank. ([#periodic-subsession-history-on-resume](https://github.com/damien-robotsix/robotsix-chat/issues/periodic-subsession-history-on-resume))
- Spawning a subsession (task, user_chat, or periodic) always crashed the new worker with
  `asyncio.run() cannot be called from a running event loop`. `create_agent_from_settings` calls
  `fetch_roster_sync`, which uses `asyncio.run()` internally — safe only when called before the
  server's event loop starts. `_subsession_worker` runs as a task on that already-running loop, so it
  now builds the agent in a worker thread instead of calling the factory directly. ([#subsession-agent-factory-asyncio-run](https://github.com/damien-robotsix/robotsix-chat/issues/subsession-agent-factory-asyncio-run))
- Fix the subsessions panel becoming unusable while a subsession is actively running: every
  `subsession_updated` SSE frame (fired frequently for in-flight work) fully wiped and rebuilt the
  entire panel, which reset the panel's scroll position and destroyed-and-recreated the reply textarea
  for any expanded subsession — stealing input focus mid-keystroke and making it impossible to type a
  continuous reply. The list now reconciles in place: each row's non-interactive header
  (status/meta/actions) is still rebuilt cheaply on every update, but the transcript and reply
  textarea are built once and never touched again by a refresh. ([#subsession-list-focus-scroll-thrash](https://github.com/damien-robotsix/robotsix-chat/issues/subsession-list-focus-scroll-thrash))
- The `POST /summary` agent was built exactly like the main chat agent — full tool suite,
  cross-session `ChatMemory` recall, roster/lifecycle instruction augmentation — for what should be a
  single bounded text-transformation call over an explicit transcript already in the prompt. In
  production, `ChatMemory.recall()` alone was observed taking 90+ seconds, dwarfing the actual
  (cheap-tier) model call. `create_agent_from_settings` gains a `bare` flag that skips all of it —
  `NullMemory`, no tools, no roster/lifecycle instructions — and the summary agent now uses it. ([#summary-agent-bare-no-memory](https://github.com/damien-robotsix/robotsix-chat/issues/summary-agent-bare-no-memory))
- `POST /summary` no longer forces a fixed 5-field JSON schema (purpose, pending_work,
  pending_questions, blockers, relevant_info). The cheap summary-tier model spent most of its turn
  trying to satisfy that schema and often ran past its token budget before producing valid JSON,
  making the summary panel slow or stuck on "Updating…". It now returns `{"summary": "<plain text>"}`
  — a few unconstrained sentences, no schema to fail. ([#summary-endpoint-free-text](https://github.com/damien-robotsix/robotsix-chat/issues/summary-endpoint-free-text))

### Deprecations and Removals

- Remove the local `GitHubClient` fallback and `GithubSettings` (skill.md, token, api_base_url) that
  intercepted `component_request(component_id="github", ...)` calls locally. GitHub access — Actions
  status plus repo read/update/create — now goes exclusively through central-deploy's `github` roster
  component, matching every other component and removing a second, drifting implementation of the same
  capability. ([#remove-duplicate-local-github-client](https://github.com/damien-robotsix/robotsix-chat/issues/remove-duplicate-local-github-client))

### Misc

- [#20260713T002850Z-chat-ui-option-to-cancel-queued-messages-c983](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T002850Z-chat-ui-option-to-cancel-queued-messages-c983), [#20260713T003026Z-durable-fixes-prevent-config-drift-requi-8a92](https://github.com/damien-robotsix/robotsix-chat/issues/20260713T003026Z-durable-fixes-prevent-config-drift-requi-8a92), [#20260709T003927Z-rebuild-and-wire-server-side-idle-compac-5dc1](https://github.com/damien-robotsix/robotsix-chat/issues/20260709T003927Z-rebuild-and-wire-server-side-idle-compac-5dc1), [#20260710T025626Z-add-a-maintenance-chat-tool-to-toggle-gi-3fd7](https://github.com/damien-robotsix/robotsix-chat/issues/20260710T025626Z-add-a-maintenance-chat-tool-to-toggle-gi-3fd7), [#20260708T065914Z-subsessions-lose-context-on-nesting-and-8587](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T065914Z-subsessions-lose-context-on-nesting-and-8587), [#20260709T090902Z-add-pagination-to-mill-board-list-endpoi-096b](https://github.com/damien-robotsix/robotsix-chat/issues/20260709T090902Z-add-pagination-to-mill-board-list-endpoi-096b), [#20260709T091802Z-ui-increase-subsession-panel-detail-text-7a5c](https://github.com/damien-robotsix/robotsix-chat/issues/20260709T091802Z-ui-increase-subsession-panel-detail-text-7a5c), [#20260706T093149Z-deactivate-all-periodic-mill-workflows-k-bc04](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T093149Z-deactivate-all-periodic-mill-workflows-k-bc04), [#20260706T095429Z-periodic-subsessions-must-not-spawn-thei-989c](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T095429Z-periodic-subsessions-must-not-spawn-thei-989c), [#20260707T101912Z-grant-robotsix-chat-agent-github-mainten-23a0](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T101912Z-grant-robotsix-chat-agent-github-mainten-23a0), [#20260706T102729Z-concatenate-queued-user-messages-into-a-d0e9](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T102729Z-concatenate-queued-user-messages-into-a-d0e9), [#20260706T102855Z-add-retry-with-backoff-wrapper-for-mill-0cf7](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T102855Z-add-retry-with-backoff-wrapper-for-mill-0cf7), [#20260710T104845Z-chat-ui-render-assistant-user-messages-a-ed96](https://github.com/damien-robotsix/robotsix-chat/issues/20260710T104845Z-chat-ui-render-assistant-user-messages-a-ed96), [#20260707T110839Z-grant-robotsix-chat-a-scoped-github-main-6f8c](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T110839Z-grant-robotsix-chat-a-scoped-github-main-6f8c), [#20260711T120413Z-formalize-autonomous-ticket-lifecycle-in-016a](https://github.com/damien-robotsix/robotsix-chat/issues/20260711T120413Z-formalize-autonomous-ticket-lifecycle-in-016a), [#20260712T120444Z-add-render-url-tool-headless-chromium-sc-d514](https://github.com/damien-robotsix/robotsix-chat/issues/20260712T120444Z-add-render-url-tool-headless-chromium-sc-d514), [#20260712T120655Z-chat-ui-suggested-answer-options-with-fr-ea92](https://github.com/damien-robotsix/robotsix-chat/issues/20260712T120655Z-chat-ui-suggested-answer-options-with-fr-ea92), [#20260706T121901Z-wire-compact-session-and-get-compacted-s-944d](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T121901Z-wire-compact-session-and-get-compacted-s-944d), [#20260708T122008Z-boilerplate-out-of-scope-ci-failure-tria-3bfb](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T122008Z-boilerplate-out-of-scope-ci-failure-tria-3bfb), [#20260708T122008Z-fast-path-add-env-doc-sync-to-determinis-ee38](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T122008Z-fast-path-add-env-doc-sync-to-determinis-ee38), [#20260707T125539Z-bump-llmio-pin-1h-timeout-a3f1](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T125539Z-bump-llmio-pin-1h-timeout-a3f1), [#20260708T125855Z-add-hypothesis-property-based-roundtrip-c698](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T125855Z-add-hypothesis-property-based-roundtrip-c698), [#20260708T130013Z-remove-dead-code-conversationstore-stats-3b2c](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T130013Z-remove-dead-code-conversationstore-stats-3b2c), [#20260708T131424Z-reorganize-module-config-align-docs-to-p-1350](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T131424Z-reorganize-module-config-align-docs-to-p-1350), [#20260708T131425Z-reorganize-module-chat-align-docs-to-per-4339](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T131425Z-reorganize-module-chat-align-docs-to-per-4339), [#20260708T131425Z-reorganize-module-llm-align-docs-to-per-c386](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T131425Z-reorganize-module-llm-align-docs-to-per-c386), [#20260708T131425Z-reorganize-module-memory-align-docs-to-p-e825](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T131425Z-reorganize-module-memory-align-docs-to-p-e825), [#20260705T133805Z-pin-the-conversation-summary-so-it-stays-942d](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T133805Z-pin-the-conversation-summary-so-it-stays-942d), [#20260710T134325Z-robotsix-chat-refactor-inline-ui-into-se-156d](https://github.com/damien-robotsix/robotsix-chat/issues/20260710T134325Z-robotsix-chat-refactor-inline-ui-into-se-156d), [#20260705T134639Z-idle-timeout-message-says-conversation-h-c2e1](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T134639Z-idle-timeout-message-says-conversation-h-c2e1), [#20260704T142108Z-tests-knowledge-missing-init-py-fe1d](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T142108Z-tests-knowledge-missing-init-py-fe1d), [#20260710T142252Z-add-a-github-repo-settings-toggle-tool-t-564d](https://github.com/damien-robotsix/robotsix-chat/issues/20260710T142252Z-add-a-github-repo-settings-toggle-tool-t-564d), [#20260709T142956Z-migrate-project-title-to-meta-tag-in-ind-66a9](https://github.com/damien-robotsix/robotsix-chat/issues/20260709T142956Z-migrate-project-title-to-meta-tag-in-ind-66a9), [#20260707T143822Z-github-virtual-component-serves-the-depl-20fd](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T143822Z-github-virtual-component-serves-the-depl-20fd), [#20260710T144748Z-self-improvement-feedback-run-at-compact-700f](https://github.com/damien-robotsix/robotsix-chat/issues/20260710T144748Z-self-improvement-feedback-run-at-compact-700f), [#20260710T145109Z-user-notification-channel-proactive-alert-f590](https://github.com/damien-robotsix/robotsix-chat/issues/20260710T145109Z-user-notification-channel-proactive-alert-f590), [#20260707T164056Z-chat-ui-top-toolbar-buttons-hidden-behin-2e66](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T164056Z-chat-ui-top-toolbar-buttons-hidden-behin-2e66), [#20260705T180136Z-remove-or-fix-orphaned-scripts-check-kin-f065](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T180136Z-remove-or-fix-orphaned-scripts-check-kin-f065), [#20260705T180137Z-extract-duplicated-owner-id-validation-i-124e](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T180137Z-extract-duplicated-owner-id-validation-i-124e), [#20260706T180615Z-add-subsessionkind-sync-check-or-js-cons-3579](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T180615Z-add-subsessionkind-sync-check-or-js-cons-3579), [#20260706T180616Z-consolidate-duplicated-jsonstorebase-sub-98be](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T180616Z-consolidate-duplicated-jsonstorebase-sub-98be), [#20260707T180951Z-consolidate-duplicated-get-post-patch-me-f423](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T180951Z-consolidate-duplicated-get-post-patch-me-f423), [#20260707T180952Z-add-sync-guard-for-create-app-run-server-278c](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T180952Z-add-sync-guard-for-create-app-run-server-278c), [#20260707T180952Z-add-sync-guard-for-duplicated-all-endpoi-8550](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T180952Z-add-sync-guard-for-duplicated-all-endpoi-8550), [#20260707T180952Z-expand-ruff-ruleset-to-catch-unused-args-e4d9](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T180952Z-expand-ruff-ruleset-to-catch-unused-args-e4d9), [#20260708T181326Z-add-zizmor-pre-commit-hook-for-github-ac-3453](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T181326Z-add-zizmor-pre-commit-hook-for-github-ac-3453), [#20260708T181326Z-extract-repeated-serializer-persist-guar-fcfc](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T181326Z-extract-repeated-serializer-persist-guar-fcfc), [#20260708T181326Z-follow-up-remove-orphaned-scripts-check-6f93](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T181326Z-follow-up-remove-orphaned-scripts-check-6f93), [#20260704T183942Z-test-gap-add-unit-tests-for-src-robotsix-d155](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T183942Z-test-gap-add-unit-tests-for-src-robotsix-d155), [#20260705T185420Z-governance-policy-requires-mirroring-age-439b](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T185420Z-governance-policy-requires-mirroring-age-439b), [#20260707T191342Z-subsessionssettings-default-model-level-0c68](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T191342Z-subsessionssettings-default-model-level-0c68), [#20260708T192830Z-validate-model-level-rejects-level-4-for-1c2b](https://github.com/damien-robotsix/robotsix-chat/issues/20260708T192830Z-validate-model-level-rejects-level-4-for-1c2b), [#20260706T201805Z-subsession-panels-still-pop-up-on-refres-6b64](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T201805Z-subsession-panels-still-pop-up-on-refres-6b64), [#20260706T201815Z-thicken-the-border-around-subsession-sum-5a04](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T201815Z-thicken-the-border-around-subsession-sum-5a04), [#20260707T212337Z-ci-fix-out-of-scope-ci-failure-ruf100-un-0772](https://github.com/damien-robotsix/robotsix-chat/issues/20260707T212337Z-ci-fix-out-of-scope-ci-failure-ruf100-un-0772), [#20260706T212405Z-robotsix-chat-remove-env-doc-sync-period-9d25](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T212405Z-robotsix-chat-remove-env-doc-sync-period-9d25), [#20260712T214025Z-add-one-subsession-per-subject-rule-to-s-efab](https://github.com/damien-robotsix/robotsix-chat/issues/20260712T214025Z-add-one-subsession-per-subject-rule-to-s-efab), [#20260705T221141Z-remove-dead-idle-reset-seconds-parameter-e125](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T221141Z-remove-dead-idle-reset-seconds-parameter-e125), [#20260706T223838Z-mirror-test-directory-structure-for-chat-81a0](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T223838Z-mirror-test-directory-structure-for-chat-81a0), [#20260706T223838Z-remove-dead-code-compact-session-get-com-4834](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T223838Z-remove-dead-code-compact-session-get-com-4834), [#20260705T230903Z-remove-redundant-actions-setup-python-st-2378](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T230903Z-remove-redundant-actions-setup-python-st-2378), [#20260706T233225Z-add-frozen-flag-to-docs-yml-uv-sync-for-6bb8](https://github.com/damien-robotsix/robotsix-chat/issues/20260706T233225Z-add-frozen-flag-to-docs-yml-uv-sync-for-6bb8)

## [0.2.1] - 2026-07-06

### Bugfixes

- Bump the pinned `robotsix-llmio` commit to pick up detection of usage-credit exhaustion when the
  Claude SDK collapses it into a raised exception instead of a clean `is_error=True` return (the
  `ClaudeSDKUsageExhaustedError` fallback added in the previous fix only covered the latter shape).
  Without this, the raw "Claude Code returned an error result: success" text could still leak to the
  main chat session instead of triggering the tier fallback. ([#bump-llmio-usage-exhausted-collapsed-fix](https://github.com/damien-robotsix/robotsix-chat/issues/bump-llmio-usage-exhausted-collapsed-fix))
- When a claudeSDK tier's Claude subscription usage credits are exhausted (e.g. level 4's
  `claude-fable-5`), the chat agent no longer surfaces the raw "You're out of usage credits" text as
  if it were a genuine reply. It now catches the new `ClaudeSDKUsageExhaustedError` from
  robotsix-llmio and retries the same turn at a fallback tier (level 3's `opus`) via robotsix-llmio's
  `acall_with_tier_fallback`, scoped to one promotion. ([#claude-sdk-usage-fallback](https://github.com/damien-robotsix/robotsix-chat/issues/claude-sdk-usage-fallback))
- `POST /summary` (regenerated after every assistant turn) reused the main conversation agent — often
  the most expensive configured tier — for a bounded JSON-extraction task. It now runs on a dedicated
  agent at a new `summary_model_level` setting (default level 1, the cheapest tier). Unlike
  `llmio_model_level`, a missing OpenRouter key for this level is not fatal: the server logs a warning
  and falls back to the keyless level 3 instead of failing to start. ([#summary-endpoint-cheap-model-level](https://github.com/damien-robotsix/robotsix-chat/issues/summary-endpoint-cheap-model-level))
- Main-agent Langfuse tracing: export `LANGFUSE_BASE_URL` (the name `robotsix-llmio` reads) alongside
  `LANGFUSE_HOST`. Without it the OTLP exporter fell back to Langfuse Cloud US and every span batch
  was rejected with 401, so the self-hosted project received no traces. ([#20260704T192500Z-langfuse-base-url-env](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T192500Z-langfuse-base-url-env))
- Recalled memory is now prepended to the current user turn instead of appended to the system prompt.
  Per-message recall text in the system prompt sat at the head of the provider's cacheable prefix,
  invalidating the prompt cache on every turn; the system prompt is now byte-stable across a
  conversation so the instruction, tools, and replayed transcript can be served from cache. ([#20260704T200500Z-memory-injection-cache-friendly](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T200500Z-memory-injection-cache-friendly))
- Reword the subsession `model_level` guidance in the default agent instruction and the
  `spawn_subsession` tool description: level 2 (cheap OpenRouter tier) is now the default choice for
  general work, and level 3 (keyless Claude Opus) is reserved for reasoning level 2 struggles with.
  Previously the guidance framed level 3 as "the default for general work", so subsessions almost
  always spawned at level 3 even when a cheaper tier would have been enough. ([#subsession-prefer-level-2-for-general-work](https://github.com/damien-robotsix/robotsix-chat/issues/subsession-prefer-level-2-for-general-work))

### Misc

- [#20260704T082645Z-config-not-durable-no-config-volume-robo-95ae](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T082645Z-config-not-durable-no-config-volume-robo-95ae), [#20260704T100051Z-ci-failure-release-image-on-main-55fd](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T100051Z-ci-failure-release-image-on-main-55fd), [#20260705T100556Z-agent-md-key-file-map-update-stale-refer-fea4](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T100556Z-agent-md-key-file-map-update-stale-refer-fea4), [#20260704T102723Z-ci-fix-out-of-scope-ci-failure-build-and-a7cd](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T102723Z-ci-fix-out-of-scope-ci-failure-build-and-a7cd), [#20260704T104029Z-persistence-path-defaults-point-at-unmou-b9de](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T104029Z-persistence-path-defaults-point-at-unmou-b9de), [#20260704T111500Z-release-commit-ci-fixes](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T111500Z-release-commit-ci-fixes), [#20260704T114500Z-fix-duplicate-config-volume-mount-point](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T114500Z-fix-duplicate-config-volume-mount-point), [#20260704T141344Z-periodic-subsession-lifecycle-bugs-dupli-7cd4](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T141344Z-periodic-subsession-lifecycle-bugs-dupli-7cd4), [#20260704T141855Z-eliminate-duplicated-fetch-roster-fetch-4b1e](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T141855Z-eliminate-duplicated-fetch-roster-fetch-4b1e), [#20260704T141855Z-split-chat-server-routes-py-850-lines-in-a45b](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T141855Z-split-chat-server-routes-py-850-lines-in-a45b), [#20260704T142126Z-register-the-deploy-lifecycle-api-as-a-s-a5d9](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T142126Z-register-the-deploy-lifecycle-api-as-a-s-a5d9), [#20260704T143423Z-bug-in-flight-assistant-response-lost-on-f753](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T143423Z-bug-in-flight-assistant-response-lost-on-f753), [#20260704T144024Z-message-subsession-close-subsession-fail-9671](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T144024Z-message-subsession-close-subsession-fail-9671), [#20260705T160000Z-component-request-roster-auth-metadata-cc01](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T160000Z-component-request-roster-auth-metadata-cc01), [#20260705T163000Z-roster-fetch-x-api-key-auth-dd02](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T163000Z-roster-fetch-x-api-key-auth-dd02), [#20260704T180112Z-add-serialize-deserialize-hooks-to-jsons-3b8a](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T180112Z-add-serialize-deserialize-hooks-to-jsons-3b8a), [#20260704T180112Z-pin-robotsix-config-git-dependency-to-fu-784d](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T180112Z-pin-robotsix-config-git-dependency-to-fu-784d), [#20260704T182301Z-show-a-conversation-summary-at-the-top-o-55d3](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T182301Z-show-a-conversation-summary-at-the-top-o-55d3), [#20260704T182446Z-default-prompt-promises-component-reques-cc62](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T182446Z-default-prompt-promises-component-reques-cc62), [#20260704T182446Z-knowledge-tool-names-in-system-prompt-do-4c24](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T182446Z-knowledge-tool-names-in-system-prompt-do-4c24), [#20260704T182453Z-ui-session-panel-should-shift-the-centra-b10a](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T182453Z-ui-session-panel-should-shift-the-centra-b10a), [#20260704T183942Z-test-gap-add-unit-tests-for-src-robotsix-64c1](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T183942Z-test-gap-add-unit-tests-for-src-robotsix-64c1), [#20260704T194304Z-add-step-security-harden-runner-for-ci-r-1d97](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T194304Z-add-step-security-harden-runner-for-ci-r-1d97), [#20260704T195122Z-memory-scope-cognee-session-guidance-per-ae55](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T195122Z-memory-scope-cognee-session-guidance-per-ae55), [#20260704T195125Z-component-access-do-not-cache-empty-rost-b233](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T195125Z-component-access-do-not-cache-empty-rost-b233), [#20260704T195133Z-conversation-py-remove-dead-self-idle-re-4afb](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T195133Z-conversation-py-remove-dead-self-idle-re-4afb), [#20260704T200308Z-refactor-subsession-worker-to-split-per-cc69](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T200308Z-refactor-subsession-worker-to-split-per-cc69), [#20260704T200308Z-remove-dead-chat-init-py-re-export-layer-67d0](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T200308Z-remove-dead-chat-init-py-re-export-layer-67d0), [#20260704T202829Z-consolidate-modules-direct-repo-repo-stu-42f2](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T202829Z-consolidate-modules-direct-repo-repo-stu-42f2), [#20260704T213735Z-ci-failure-docs-on-main-ff1d](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T213735Z-ci-failure-docs-on-main-ff1d), [#20260704T213817Z-ci-failure-release-image-on-main-3b09](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T213817Z-ci-failure-release-image-on-main-3b09), [#20260705T222454Z-add-unit-tests-for-shared-route-utilitie-3757](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T222454Z-add-unit-tests-for-shared-route-utilitie-3757), [#20260704T231504Z-ci-failure-release-image-on-main-13e4](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T231504Z-ci-failure-release-image-on-main-13e4), [#20260705T234250Z-robotsix-chat-add-repo-description-sync-a46a](https://github.com/damien-robotsix/robotsix-chat/issues/20260705T234250Z-robotsix-chat-add-repo-description-sync-a46a), [#20260704T234414Z-ensure-changelog-fragments-created-by-pi-1bd2](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T234414Z-ensure-changelog-fragments-created-by-pi-1bd2)

## [0.2.0] - 2026-07-04

### Features

- Implemented the embedded component-agent SDK responder in robotsix-chat — the reference
  implementation for per-component adoption (epic child #6).

  - Added `ComponentAgentSettings` (disabled-by-default) with broker connection fields, cross-field
    invariants, and env-var overrides.

  - Created `ComponentAgentResponder` that lazily imports the SDK `BrokeredResponder` behind an
    `importlib.util.find_spec` guard so the package stays importable without the `broker` extra.

  - Registered three request kinds:

    - `monitor` — genuine live telemetry: check-loop registry snapshot + running count,
      conversation/EventBus stats, and secret-redacted settings snapshot.
    - `config-get` — redacted config snapshot + settable-key metadata.
    - `config-set` — validated config update applied to the live `Settings` instance, returning an
      audit record; invalid updates are rejected with a framed `code`/`message`/`details` error and
      never mutate the live config.

  - Added read-only `ConversationStore.stats()`, `EventBus.subscriber_count()`, and
    `CheckLoopRegistry.snapshot()` accessors to feed genuine state into the monitor handler.

  - Wired responder start/stop into the Starlette lifespan, gated behind the disabled-by-default
    `component_agent.enabled` flag.

  ([#embed-sdk-responder](https://github.com/damien-robotsix/robotsix-chat/issues/embed-sdk-responder))
- Redesign the chat system around a unified **subsession** model: the main agent (now on llmio Level
  4, `claude-fable-5`) spawns background sub-agents of three kinds — one-shot `task`, recurring
  `periodic`, and user-facing `user_chat` side-chats — each at a model level (1–4) picked by task
  difficulty, with depth-limited nesting, mid-run steering messages, external close, and a summary
  delivered to the parent conversation on every close path. Replaces `delegate_task` background tasks,
  check loops, and the pending-questions thread system (endpoints, SSE events, tools, config, and UI
  panels removed); the browser UI gains a single Subsessions panel with live status, expandable
  transcripts, per-subsession chat for `user_chat`, and clearer labeled controls. ([#20260702T000000Z-unified-subsession-redesign](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T000000Z-unified-subsession-redesign))
- Route background sub-agents (delegate_task and check-loop workers) to a cheaper Claude model while
  staying on the Claude SDK subscription. The new `llmio.subagent_model` setting (default `"sonnet"`)
  controls which model the background sub-agents use — `"sonnet"`, `"haiku"`, or `null` (match
  foreground). Override is only applied when the foreground is on the keyless `claudeSDK` provider
  (level 3); OpenRouter levels are untouched. The foreground/interactive agent is unchanged. ([#20260624T023021Z-route-background-subagents-to-cheaper-claude-model](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T023021Z-route-background-subagents-to-cheaper-claude-model))
- Support llmio model level 4 (Claude Fable 5 frontier tier): bump the `robotsix-board-agent` pin
  (which carries `robotsix-llmio` past the level-4 addition), and derive the valid `model_level` set
  from llmio's `TierLevel` enum instead of hardcoding `[1, 2, 3]` — chat can no longer drift from the
  tiers llmio actually ships. `LLMIO_MODEL_LEVEL=4` now deploys. ([#20260703T080000Z-support-llmio-model-level-4](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T080000Z-support-llmio-model-level-4))
- Add `consult_mail` tool: broker-mediated access to the `robotsix-auto-mail` board agent, enabling
  the assistant to view and triage mail-agent tickets. ([#20260624T083007Z-give-the-assistant-access-to-the-mail-bo-95f3](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T083007Z-give-the-assistant-access-to-the-mail-bo-95f3))
- Board writes that fail due to broker unavailability are now automatically retried with exponential
  backoff (initial ~15 min, max 4 hr, ±20 % jitter); retry state is persisted to
  `.data/board_write_queue.json` and inspectable via the new `get_board_write_queue_status` tool. ([#20260624T083951Z-auto-retry-board-writes-when-the-board-m-3de0](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T083951Z-auto-retry-board-writes-when-the-board-m-3de0))
- New `repo_study` capability: the chat agent can fetch a temporary local snapshot of a GitHub
  repository (tarball download — no `git` binary in the image) and study it with read-only tools
  (`fetch_repo_for_study`, `list_repo_files`, `read_repo_file`, `search_repo_files`,
  `drop_repo_workspace`). Workspaces live under `/data/repo_study`, are capped in size, and expire
  automatically. Private repos authenticate through the existing `direct_repo` GitHub App credentials;
  public repos need no auth. Config-gated by `repo_study.enabled` (off by default). ([#20260704T100000Z-repo-study-tools](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T100000Z-repo-study-tools))
- Check-loop ticks now trigger a serialized foreground agent run. On each non-suppressed tick the
  agent answers the tick result and the reply is recorded into the owner's active session and streamed
  to the browser as a visible assistant bubble (`loop_reply` SSE frame). Tick results are also
  rendered inline in the chat as distinct "check-loop" bubbles. Runs are serialized per owner so a
  tick-triggered run cannot race a user message. The tick-triggered agent is built without check-loop
  tools, preventing infinite recursion. ([#20260624T103058Z-robotsix-chat-check-loop-ticks-must-disp-ceaa](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T103058Z-robotsix-chat-check-loop-ticks-must-disp-ceaa))
- Pending-questions threads now behave as embedded mini-chat windows: the thread input sends messages
  directly to the LLM (with merged main-chat + thread context), and the assistant's replies are
  rendered inline in the thread panel rather than in the main chat window. The separate "Answer" input
  has been removed — all interaction is through a single, always-available reply box. Multi-turn
  conversation (3+ back-and-forth turns) works without page reload. ([#20260627T104005Z-pending-questions-thread-embedded-mini-c-d7f8](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T104005Z-pending-questions-thread-embedded-mini-c-d7f8))
- Added a `delegate_task` tool that lets the foreground chat agent offload long-running work to a
  same-tier background sub-agent. The tool returns a task id immediately so the foreground reply is
  never blocked. Task lifecycle is tracked in the shared `TaskRegistry` and completion/failure frames
  are pushed through the injected `DeliveryChannel` (currently a no-op placeholder until the concrete
  SSE adapter lands). Sub-agents are built without the delegation tool, preventing infinite recursion.
  The `agent_instruction` now includes delegate-vs-inline guidance. ([#20260622T111357Z-add-agent-invocable-delegate-task-tool-5d9a](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T111357Z-add-agent-invokable-delegate-task-tool-5d9a))
- Browser UI now opens a persistent `/events` SSE channel on page load and renders task lifecycle
  notifications (`task_started`, `task_completed`, `task_failed`) as distinct in-chat bubbles. ([#20260622T111357Z-render-background-task-notifications-in-41ed](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T111357Z-render-background-task-notifications-in-41ed))
- Added the ability to **close a session**, which stops its background work. A new
  `DELETE /sessions/{session_id}` endpoint stops every check loop and cancels every in-flight
  background sub-agent task owned by the session (via `CheckLoopRegistry.stop_all_for_session` /
  `TaskRegistry.cancel_all_for_session`), deletes the session and its history (reassigning the owner's
  active session, or creating a fresh empty one when none remain), and returns the loop/task stop
  counts. The sessions panel gains a per-session delete (×) button. This completes the per-session
  lifecycle: a recurring check now survives restarts and runs until it is explicitly stopped **or its
  session is closed**. ([#20260625T120000Z-session-close-stops-loops-and-tasks](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T120000Z-session-close-stops-loops-and-tasks))
- Made `consult_mill` (and the other brokered agents) resilient to a slow or unreachable board
  manager. A fast pre-flight reachability check (authenticated `GET /agents` with a short timeout) now
  runs before each request, so a down broker or an offline recipient fails in a few seconds instead of
  hanging for the full request timeout. Because the board manager is a multi-turn LLM agent that
  legitimately takes tens of seconds — longer when its replies queue behind other mill work — the mill
  request timeout was raised from 120s to 300s (`MILL_TIMEOUT`). Net effect: genuine outages surface
  quickly, while a reachable-but-busy board manager is given room to finish instead of spuriously
  timing out. ([#20260625T130000Z-board-manager-preflight-and-longer-timeout](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T130000Z-board-manager-preflight-and-longer-timeout))
- Add a persistent `GET /events?client_id=...` SSE channel that streams background-task lifecycle
  frames (`task_started`/`task_completed`/`task_failed`) to the browser. ([#20260622T170000Z-add-persistent-sse-events-channel-aa11](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T170000Z-add-persistent-sse-events-channel-aa11))
- Add a validated config-get / config-set contract module
  (`src/robotsix_chat/component_agent/config_contract.py`) for the robotsix-chat component agent. The
  module defines a dotted-path-key allowlist (`SETTABLE_KEYS`) of genuinely live-mutable settings,
  secret-redacted snapshots (`get_config_snapshot`), machine-usable metadata (`describe_config`), and
  validate-before-apply logic (`validate_config_update` / `apply_config_update`) that enforces type
  checks, cross-field invariants (via `Settings.model_post_init`), and audit logging. On rejection the
  live `Settings` instance is left completely unchanged; on success an auditable INFO log entry and
  structured audit record are emitted. ([#20260623T205618Z-define-validated-config-get-set-contract-57c2](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T205618Z-define-validated-config-get-set-contract-57c2))
- Add a read-only recent-activity digest tool (self-review) exposing live cross-session conversation
  activity. ([#20260623T221042Z-implement-conversationstore-recent-active-3191](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T221042Z-implement-conversationstore-recent-activ-3191))

### Bugfixes

- Pending-question threads no longer die after the first reply: the background agent no longer
  auto-removes a question mid-thread, so follow-up messages keep getting responses (the question is
  only removed when the user explicitly dismisses it). ([#20260629T002450Z-pending-question-threads-persist-after-first-reply](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T002450Z-pending-question-threads-persist-after-first-reply))
- Fixed duplicate notification bubbles (a check-loop's `loop_started` / `loop_tick` message, or a
  background-task frame, rendered 2–3× in some chats). The `/events` SSE reconnect path scheduled a
  bare `setTimeout(openEventStream, …)` from each `onDone`/`error` callback, and `openEventStream()`
  created a fresh `AbortController` without aborting the previous stream — so stacked reconnects left
  multiple live `/events` fetches, each holding its own server-side EventBus subscription. Every frame
  was then fanned out (and rendered) once per leaked subscription, which is why the count varied per
  session ("not in all chats"). `openEventStream()` now aborts any prior stream before opening, and
  reconnects route through a single guarded timer so at most one stream/subscription exists per
  session. ([#20260626T010000Z-fix-sse-subscription-leak-duplicate-bubbles](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T010000Z-fix-sse-subscription-leak-duplicate-bubbles))
- Fix broker-skill tool generation hardcoding every parameter annotation to `str`; tool JSON schemas
  now reflect each parameter's real type (int/bool/list/str), so pydantic-ai builds correct schemas. ([#20260629T090000Z-fix-skill-tool-annotations-real-types-6272](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T090000Z-fix-skill-tool-annotations-real-types-6272))
- Fix GHCR image publish: use the built-in `GITHUB_TOKEN` for registry login instead of unset
  `GHCR_TOKEN`/`GHCR_USERNAME` secrets, unblocking the Release image workflow. ([#20260627T142832Z-fix-ghcr-login-github-token-2590](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T142832Z-fix-ghcr-login-github-token-2590))
- Fix a `PydanticSchemaGenerationError` ("Unable to generate pydantic-core schema for
  `CheckLoopRegistry`") that crashed the chat agent: the extracted check-loop tools were bound with
  `functools.partial`, whose signature still exposed the injected runtime state (`registry`,
  `settings`, `channel`, …), so the provider's tool-schema builder tried to JSON-schema the
  non-pydantic `CheckLoopRegistry`. The tools are now thin closures that capture state lexically,
  exposing only the model-facing parameters. ([#20260628T150000Z-check-loop-tools-partial-schema-crash](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T150000Z-check-loop-tools-partial-schema-crash))
- Check loops can now self-stop: the worker injects a loop-scoped `stop_check_loop` tool into each
  tick sub-agent, so a check that detects a terminal/condition-met state halts its own loop instead of
  re-reporting the same terminal status every interval until a human stops it. Restart-safe (rebuilt
  on resume) and recursion-safe (stop-only, no loop creation). ([#20260627T151429Z-check-loop-self-stop-tool-3f7a](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T151429Z-check-loop-self-stop-tool-3f7a))
- Fix cognee Langfuse tracing: register an explicitly-configured OTLP logger instance so cognee
  traffic reaches the dedicated project instead of defaulting to Langfuse US cloud with the main
  project's credentials. ([#20260703T170000Z-fix-cognee-langfuse-otel-endpoint-creds](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T170000Z-fix-cognee-langfuse-otel-endpoint-creds))
- Isolate cognee's Langfuse OTLP tracing from llmio's global tracer provider (`skip_set_global`):
  cognee spans were landing in the main robotsix-chat Langfuse project instead of
  robotsix-chat-cognee. ([#20260703T172500Z-isolate-cognee-otel-provider](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T172500Z-isolate-cognee-otel-provider))
- Fix the chat→calendar-agent broker integration: requests now send the prompt under the `instruction`
  key the calendar agent requires (it previously sent `message`, which the agent rejected with
  "Request body must contain an 'instruction' key"). Also corrects the default `calendar_agent_id` to
  the agent's real broker id `robotsix-calendar`. ([#20260623T193000Z-fix-calendar-broker-instruction-key](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T193000Z-fix-calendar-broker-instruction-key))

### Improved Documentation

- Add module manifest (docs/modules.yaml) mapping every importable namespace to its purpose and file
  paths. ([#20260622T135439Z-implement-persistent-server-to-browser-s-ecda](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T135439Z-implement-persistent-server-to-browser-s-ecda))
- Document the standard scope-triage boilerplate for changelog updates in docs/triage-boilerplate.md. ([#20260703T211754Z-boilerplate-scope-triage-expand-changelo-0dd8](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T211754Z-boilerplate-scope-triage-expand-changelo-0dd8))

### Misc

- [#20260703T000000Z-align-repo-with-robotsix-standards](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T000000Z-align-repo-with-robotsix-standards), [#20260704T001648Z-generic-component-access-roster-skills-c-690e](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T001648Z-generic-component-access-roster-skills-c-690e), [#20260629T002016Z-mail-direct-http-access-to-auto-mail-boa-f2e0](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T002016Z-mail-direct-http-access-to-auto-mail-boa-f2e0), [#20260702T004755Z-migrate-http-mocking-from-monkeypatch-se-48e4](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T004755Z-migrate-http-mocking-from-monkeypatch-se-48e4), [#20260703T010000Z-central-deploy-onboarding](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T010000Z-central-deploy-onboarding), [#20260702T010053Z-enable-changelog-autofill-periodic-workf-683b](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T010053Z-enable-changelog-autofill-periodic-workf-683b), [#20260630T010950Z-extract-tick-execution-and-stop-decision-182a](https://github.com/damien-robotsix/robotsix-chat/issues/20260630T010950Z-extract-tick-execution-and-stop-decision-182a), [#20260702T011231Z-remove-accidentally-committed-local-pkgs-df85](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T011231Z-remove-accidentally-committed-local-pkgs-df85), [#20260703T013406Z-add-hypothesis-property-based-testing-fo-1b41](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T013406Z-add-hypothesis-property-based-testing-fo-1b41), [#20260701T013501Z-remove-dead-code-terminal-result-in-chat-2ca8](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T013501Z-remove-dead-code-terminal-result-in-chat-2ca8), [#20260624T015501Z-agent-md-the-broker-src-submodule-vendor-da08](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T015501Z-agent-md-the-broker-src-submodule-vendor-da08), [#20260624T020652Z-robotsix-chat-give-the-assistant-direct-1628](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T020652Z-robotsix-chat-give-the-assistant-direct-1628), [#20260702T021947Z-dry-repetitive-builder-and-validation-bl-1547](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T021947Z-dry-repetitive-builder-and-validation-bl-1547), [#20260625T022859Z-refactor-robotsix-chat-http-client-dupli-bf1c](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T022859Z-refactor-robotsix-chat-http-client-dupli-bf1c), [#20260623T025119Z-remove-accidentally-committed-src-bin-id-7051](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T025119Z-remove-accidentally-committed-src-bin-id-7051), [#20260701T025754Z-add-security-posture-periodic-workflow-t-67c8](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T025754Z-add-security-posture-periodic-workflow-t-67c8), [#20260629T030000Z-enforce-pre-commit-in-ci-and-fix-violations-bf8a](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T030000Z-enforce-pre-commit-in-ci-and-fix-violations-bf8a), [#20260704T031459Z-ci-failure-release-image-on-main-3c02](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T031459Z-ci-failure-release-image-on-main-3c02), [#20260704T032500Z-fix-monotonic-roster-cache-test-flake](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T032500Z-fix-monotonic-roster-cache-test-flake), [#20260623T032957Z-add-server-max-background-tasks-to-examp-641b](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T032957Z-add-server-max-background-tasks-to-examp-641b), [#20260704T034608Z-migrate-to-structlog-based-json-logging-1cb6](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T034608Z-migrate-to-structlog-based-json-logging-1cb6), [#20260701T035053Z-add-actionlint-and-zizmor-to-ci-for-work-5bfd](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T035053Z-add-actionlint-and-zizmor-to-ci-for-work-5bfd), [#20260701T035053Z-add-dependency-review-action-to-block-pr-1526](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T035053Z-add-dependency-review-action-to-block-pr-1526), [#20260701T035053Z-add-openssf-scorecard-github-action-for-a225](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T035053Z-add-openssf-scorecard-github-action-for-a225), [#20260701T035053Z-generate-cyclonedx-sbom-at-build-time-fo-b03c](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T035053Z-generate-cyclonedx-sbom-at-build-time-fo-b03c), [#20260701T035053Z-migrate-dependabot-from-pip-to-uv-ecosys-9028](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T035053Z-migrate-dependabot-from-pip-to-uv-ecosys-9028), [#20260701T041224Z-ci-failure-openssf-scorecard-on-main-ed39](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T041224Z-ci-failure-openssf-scorecard-on-main-ed39), [#20260701T041226Z-ci-failure-release-image-on-main-77ee](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T041226Z-ci-failure-release-image-on-main-77ee), [#20260623T043413Z-migrate-robotsix-chat-to-consume-reply-t-e574](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T043413Z-migrate-robotsix-chat-to-consume-reply-t-e574), [#20260623T044834Z-agent-md-testing-conventions-when-testing-a1c3](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T044834Z-agent-md-testing-conventions-when-testin-a1c3), [#20260625T062818Z-enable-env-doc-sync-periodic-workflow-fo-083a](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T062818Z-enable-env-doc-sync-periodic-workflow-fo-083a), [#20260702T072620Z-replace-dead-data-dir-audit-yaml-with-da-e398](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T072620Z-replace-dead-data-dir-audit-yaml-with-da-e398), [#20260625T075004Z-env-doc-sync-missing-from-docs-conversat-af36](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T075004Z-env-doc-sync-missing-from-docs-conversat-af36), [#20260625T075004Z-env-doc-sync-missing-from-docs-llmio-sub-dcc3](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T075004Z-env-doc-sync-missing-from-docs-llmio-sub-dcc3), [#20260625T075004Z-env-doc-sync-missing-from-docs-max-check-b669](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T075004Z-env-doc-sync-missing-from-docs-max-check-b669), [#20260625T075004Z-env-doc-sync-missing-from-docs-min-check-4747](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T075004Z-env-doc-sync-missing-from-docs-min-check-4747), [#20260625T075004Z-env-doc-sync-missing-from-docs-version-c-8ffb](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T075004Z-env-doc-sync-missing-from-docs-version-c-8ffb), [#20260704T080208Z-extract-robotsix-chat-inline-codeql-job-d716](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T080208Z-extract-robotsix-chat-inline-codeql-job-d716), [#20260627T081738Z-agent-md-rule-when-adding-a-new-env-var-6b7b](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T081738Z-agent-md-rule-when-adding-a-new-env-var-6b7b), [#20260703T081846Z-dependabot-yml-add-pre-commit-and-docker-6c53](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T081846Z-dependabot-yml-add-pre-commit-and-docker-6c53), [#20260703T081846Z-deploy-compose-move-app-config-secrets-o-f5e3](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T081846Z-deploy-compose-move-app-config-secrets-o-f5e3), [#20260703T081846Z-dockerfile-adopt-canonical-uv-export-fro-3b68](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T081846Z-dockerfile-adopt-canonical-uv-export-fro-3b68), [#20260624T083007Z-give-the-assistant-access-to-the-mail-bo-95f3](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T083007Z-give-the-assistant-access-to-the-mail-bo-95f3), [#20260629T083154Z-refresh-llmio-identifier-and-extra-refs](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T083154Z-refresh-llmio-identifier-and-extra-refs), [#20260701T083636Z-migrate-chat-to-use-robotsix-agent-comm-f89c](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T083636Z-migrate-chat-to-use-robotsix-agent-comm-f89c), [#20260624T083951Z-auto-retry-board-writes-when-the-board-m-3de0](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T083951Z-auto-retry-board-writes-when-the-board-m-3de0), [#20260626T084244Z-env-doc-sync-default-mismatch-mail-broke-5d9b](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T084244Z-env-doc-sync-default-mismatch-mail-broke-5d9b), [#20260626T084244Z-env-doc-sync-missing-from-docs-board-rea-d982](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T084244Z-env-doc-sync-missing-from-docs-board-rea-d982), [#20260626T084244Z-env-doc-sync-missing-from-docs-component-e2d8](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T084244Z-env-doc-sync-missing-from-docs-component-e2d8), [#20260627T085858Z-pending-questions-thread-show-full-histo-a214](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T085858Z-pending-questions-thread-show-full-histo-a214), [#20260703T090000Z-drop-image-scan-gha-cache](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T090000Z-drop-image-scan-gha-cache), [#20260704T090000Z-standards-alignment-sweep](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T090000Z-standards-alignment-sweep), [#20260625T090440Z-implement-pending-questions-panel-fronte-0399](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T090440Z-implement-pending-questions-panel-fronte-0399), [#20260623T091745Z-create-docs-configuration-md-documenting-08a3](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T091745Z-create-docs-configuration-md-documenting-08a3), [#20260623T091745Z-factor-out-shared-basebrokeredclient-fro-8565](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T091745Z-factor-out-shared-basebrokeredclient-fro-8565), [#20260624T092141Z-robotsix-chat-fix-query-tasks-query-cale-85a2](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T092141Z-robotsix-chat-fix-query-tasks-query-cale-85a2), [#20260623T092901Z-add-a-side-panel-showing-spawned-sub-age-6457](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T092901Z-add-a-side-panel-showing-spawned-sub-age-6457), [#20260624T093100Z-robotsix-chat-add-a-self-version-check-t-3de1](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T093100Z-robotsix-chat-add-a-self-version-check-t-3de1), [#20260623T093207Z-delete-stale-docs-user-guide-configurati-34e6](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T093207Z-delete-stale-docs-user-guide-configurati-34e6), [#20260623T093449Z-retry-transient-upstream-llm-errors-in-a-6dcc](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T093449Z-retry-transient-upstream-llm-errors-in-a-6dcc), [#20260627T093804Z-env-doc-sync-default-mismatch-calendar-c-f60b](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T093804Z-env-doc-sync-default-mismatch-calendar-c-f60b), [#20260627T094356Z-sync-stale-calendar-agent-id-default-in-91bf](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T094356Z-sync-stale-calendar-agent-id-default-in-91bf), [#20260628T094546Z-env-doc-sync-default-mismatch-mail-broke-643a](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T094546Z-env-doc-sync-default-mismatch-mail-broke-643a), [#20260703T100000Z-remove-embedded-http-basic-auth](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T100000Z-remove-embedded-http-basic-auth), [#20260703T100452Z-ci-fix-out-of-scope-ci-failure-pre-commi-9338](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T100452Z-ci-fix-out-of-scope-ci-failure-pre-commi-9338), [#20260703T101219Z-standards-round-2-align-pre-commit-hooks-63c0](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T101219Z-standards-round-2-align-pre-commit-hooks-63c0), [#20260624T103058Z-robotsix-chat-check-loop-ticks-must-disp-ceaa](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T103058Z-robotsix-chat-check-loop-ticks-must-disp-ceaa), [#20260629T103510Z-env-doc-sync-missing-from-docs-diagnosti-05d0](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T103510Z-env-doc-sync-missing-from-docs-diagnosti-05d0), [#20260629T103510Z-env-doc-sync-missing-from-docs-llmio-che-9a95](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T103510Z-env-doc-sync-missing-from-docs-llmio-che-9a95), [#20260629T103510Z-env-doc-sync-missing-from-docs-skills-en-2784](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T103510Z-env-doc-sync-missing-from-docs-skills-en-2784), [#20260703T103634Z-rebake-image-at-app-uid-1000-per-revised-456b](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T103634Z-rebake-image-at-app-uid-1000-per-revised-456b), [#20260630T104034Z-env-doc-sync-stale-in-docs-missing-from-003b](https://github.com/damien-robotsix/robotsix-chat/issues/20260630T104034Z-env-doc-sync-stale-in-docs-missing-from-003b), [#20260623T104318Z-rehydrate-conversation-in-the-ui-on-page-3d55](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T104318Z-rehydrate-conversation-in-the-ui-on-page-3d55), [#20260623T104319Z-delegated-background-tasks-never-reach-t-a0f6](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T104319Z-delegated-background-tasks-never-reach-t-a0f6), [#20260704T104500Z-docs-pages-permissions](https://github.com/damien-robotsix/robotsix-chat/issues/20260704T104500Z-docs-pages-permissions), [#20260629T104746Z-robotsix-chat-update-componentagentclien-07fe](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T104746Z-robotsix-chat-update-componentagentclien-07fe), [#20260623T105122Z-tasks-side-panel-cannot-be-closed-add-a-8190](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T105122Z-tasks-side-panel-cannot-be-closed-add-a-8190), [#20260626T105858Z-increase-font-text-size-of-pending-quest-d940](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T105858Z-increase-font-text-size-of-pending-quest-d940), [#20260703T110437Z-ci-fix-out-of-scope-ci-failure-pre-commi-10e9](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T110437Z-ci-fix-out-of-scope-ci-failure-pre-commi-10e9), [#20260626T110740Z-pending-question-auto-closes-on-answer-k-81e0](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T110740Z-pending-question-auto-closes-on-answer-k-81e0), [#20260622T111357Z-add-agent-invocable-tool-to-delegate-tas-5d9a](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T111357Z-add-agent-invokable-tool-to-delegate-tas-5d9a), [#20260622T111357Z-add-sub-agent-runner-that-executes-deleg-ebc6](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T111357Z-add-sub-agent-runner-that-executes-deleg-ebc6), [#20260622T111357Z-render-background-task-notifications-in-41ed](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T111357Z-render-background-task-notifications-in-41ed), [#20260622T111358Z-add-config-settings-and-end-to-end-lifec-9c9a](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T111358Z-add-config-settings-and-end-to-end-lifec-9c9a), [#20260703T115023Z-move-persistent-data-mount-from-home-app-0d32](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T115023Z-move-persistent-data-mount-from-home-app-0d32), [#20260703T115023Z-remove-robotsix-agent-comm-broker-integr-d056](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T115023Z-remove-robotsix-agent-comm-broker-integr-d056), [#20260625T115916Z-enable-agent-check-periodic-workflow-for-2206](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T115916Z-enable-agent-check-periodic-workflow-for-2206), [#20260703T120000Z-remove-ui-settings-button](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T120000Z-remove-ui-settings-button), [#20260703T121014Z-ci-fix-out-of-scope-ci-failure-hadolint-d5fc](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T121014Z-ci-fix-out-of-scope-ci-failure-hadolint-d5fc), [#20260701T121309Z-track-external-pr-robotsix-chat-195-e5a7](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T121309Z-track-external-pr-robotsix-chat-195-e5a7), [#20260701T121309Z-track-external-pr-robotsix-chat-334-e96c](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T121309Z-track-external-pr-robotsix-chat-334-e96c), [#20260625T122325Z-close-a-session-and-clean-up-its-associa-3f18](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T122325Z-close-a-session-and-clean-up-its-associa-3f18), [#20260625T123055Z-system-prompt-contains-internal-python-i-d0a8](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T123055Z-system-prompt-contains-internal-python-i-d0a8), [#20260624T123208Z-robotsix-chat-add-coverage-threshold-fai-101e](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T123208Z-robotsix-chat-add-coverage-threshold-fai-101e), [#20260623T124317Z-robotsix-chat-add-persistent-cross-conve-6fbd](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T124317Z-robotsix-chat-add-persistent-cross-conve-6fbd), [#20260623T124825Z-robotsix-chat-add-calendar-personal-task-eaf7](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T124825Z-robotsix-chat-add-calendar-personal-task-eaf7), [#20260623T125227Z-add-checkloopregistry-loop-worker-persis-4c34](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T125227Z-add-checkloopregistry-loop-worker-persis-4c34), [#20260623T125227Z-add-loop-stop-http-endpoint-and-wire-che-2943](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T125227Z-add-loop-stop-http-endpoint-and-wire-che-2943), [#20260623T125227Z-add-loops-ui-section-to-chat-tasks-panel-a395](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T125227Z-add-loops-ui-section-to-chat-tasks-panel-a395), [#20260623T125227Z-add-start-check-loop-stop-check-loop-age-75ab](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T125227Z-add-start-check-loop-stop-check-loop-age-75ab), [#20260626T125406Z-docs-configuration-md-agent-instruction-dff8](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T125406Z-docs-configuration-md-agent-instruction-dff8), [#20260623T130339Z-make-the-background-task-pane-larger-res-4557](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T130339Z-make-the-background-task-pane-larger-res-4557), [#20260626T130813Z-update-assistant-system-prompt-to-act-mo-8b31](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T130813Z-update-assistant-system-prompt-to-act-mo-8b31), [#20260627T131052Z-agent-guard-at-agent-py-104-110-is-outsi-6dd4](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T131052Z-agent-guard-at-agent-py-104-110-is-outsi-6dd4), [#20260623T131325Z-enable-the-assistant-to-run-periodic-rec-3feb](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T131325Z-enable-the-assistant-to-run-periodic-rec-3feb), [#20260628T131727Z-prompt-board-rules-contradict-use-consul-328b](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T131727Z-prompt-board-rules-contradict-use-consul-328b), [#20260628T131727Z-prompt-falsely-claims-new-tickets-default-c44d](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T131727Z-prompt-falsely-claims-new-tickets-defaul-c44d), [#20260628T132438Z-enable-direct-repo-capabilities-push-bra-cc65](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T132438Z-enable-direct-repo-capabilities-push-bra-cc65), [#20260626T132702Z-ci-failure-release-image-on-main-cd01](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T132702Z-ci-failure-release-image-on-main-cd01), [#20260623T132732Z-add-max-check-loops-and-min-check-loop-i-2b3d](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T132732Z-add-max-check-loops-and-min-check-loop-i-2b3d), [#20260702T133649Z-add-robotsix-standards-reference-link-to-7a80](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T133649Z-add-robotsix-standards-reference-link-to-7a80), [#20260629T133754Z-governance-and-docs-reference-stale-conf-3a95](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T133754Z-governance-and-docs-reference-stale-conf-3a95), [#20260628T134239Z-chat-agent-runs-twice-on-one-message-nea-10b3](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T134239Z-chat-agent-runs-twice-on-one-message-nea-10b3), [#20260628T140000Z-rename-llmio-openrouter-extra-274](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T140000Z-rename-llmio-openrouter-extra-274), [#20260628T140116Z-add-direct-repo-config-section-to-docs-c-1b24](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T140116Z-add-direct-repo-config-section-to-docs-c-1b24), [#20260625T140306Z-enable-board-cleanup-periodic-workflow-f-f519](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T140306Z-enable-board-cleanup-periodic-workflow-f-f519), [#20260623T140358Z-wire-min-check-loop-interval-seconds-fro-cbb7](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T140358Z-wire-min-check-loop-interval-seconds-fro-cbb7), [#20260623T141949Z-add-test-coverage-for-broker-client-py-b-f3b0](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T141949Z-add-test-coverage-for-broker-client-py-b-f3b0), [#20260701T142913Z-test-gap-add-unit-tests-for-src-robotsix-1ca4](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T142913Z-test-gap-add-unit-tests-for-src-robotsix-1ca4), [#20260701T143226Z-track-external-pr-robotsix-chat-335-023a](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T143226Z-track-external-pr-robotsix-chat-335-023a), [#20260701T143226Z-track-external-pr-robotsix-chat-337-4858](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T143226Z-track-external-pr-robotsix-chat-337-4858), [#20260622T143542Z-adopt-towncrier-for-changelog-automation-c2a9](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T143542Z-adopt-towncrier-for-changelog-automation-c2a9), [#20260622T143958Z-queue-user-messages-while-the-chat-agent-6230](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T143958Z-queue-user-messages-while-the-chat-agent-6230), [#20260622T144001Z-give-the-chat-agent-access-to-the-user-s-6708](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T144001Z-give-the-chat-agent-access-to-the-user-s-6708), [#20260703T144325Z-trace-cognee-s-internal-llm-calls-cognif-45f6](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T144325Z-trace-cognee-s-internal-llm-calls-cognif-45f6), [#20260702T144526Z-prompt-references-spawn-subsession-but-a-c0bc](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T144526Z-prompt-references-spawn-subsession-but-a-c0bc), [#20260622T150026Z-robotsix-chat-enable-the-data-dir-audit-b3b5](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T150026Z-robotsix-chat-enable-the-data-dir-audit-b3b5), [#20260703T150417Z-docs-configuration-md-shows-llmio-model-a366](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T150417Z-docs-configuration-md-shows-llmio-model-a366), [#20260627T150616Z-pending-questions-thread-double-posts-ea-8922](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T150616Z-pending-questions-thread-double-posts-ea-8922), [#20260627T151446Z-design-and-implement-skill-capability-lo-f0dc](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T151446Z-design-and-implement-skill-capability-lo-f0dc), [#20260627T151456Z-direct-calendar-tasks-broker-access-via-9482](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T151456Z-direct-calendar-tasks-broker-access-via-9482), [#20260703T151622Z-test-gap-add-unit-tests-for-subsessions-711b](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T151622Z-test-gap-add-unit-tests-for-subsessions-711b), [#20260627T151759Z-child-diagnostics-capture-instrument-blo-ed74](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T151759Z-child-diagnostics-capture-instrument-blo-ed74), [#20260627T151759Z-child-diagnostics-categorize-bucket-bloc-d14a](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T151759Z-child-diagnostics-categorize-bucket-bloc-d14a), [#20260627T151759Z-child-diagnostics-closed-loop-measure-fi-5df1](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T151759Z-child-diagnostics-closed-loop-measure-fi-5df1), [#20260627T151759Z-child-diagnostics-systemic-fixes-surface-b306](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T151759Z-child-diagnostics-systemic-fixes-surface-b306), [#20260623T152127Z-add-pytest-xdist-dev-dependency-to-fix-c-ce27](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T152127Z-add-pytest-xdist-dev-dependency-to-fix-c-ce27), [#20260623T152548Z-classify-robotsix-chat-calendar-add-to-d-e0a4](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T152548Z-classify-robotsix-chat-calendar-add-to-d-e0a4), [#20260703T153121Z-config-migration-ci-schema-guard-deploy-26af](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T153121Z-config-migration-ci-schema-guard-deploy-26af), [#20260703T153121Z-core-config-migration-robotsix-config-js-8853](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T153121Z-core-config-migration-robotsix-config-js-8853), [#20260625T160838Z-add-weekly-container-vulnerability-resca-d313](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T160838Z-add-weekly-container-vulnerability-resca-d313), [#20260624T161410Z-classify-robotsix-chat-component-agent-a-a95f](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T161410Z-classify-robotsix-chat-component-agent-a-a95f), [#20260624T161410Z-classify-robotsix-chat-component-client-30ce](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T161410Z-classify-robotsix-chat-component-client-30ce), [#20260624T161410Z-classify-robotsix-chat-knowledge-add-as-2bbf](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T161410Z-classify-robotsix-chat-knowledge-add-as-2bbf), [#20260624T161411Z-classify-robotsix-chat-selfreview-add-as-3da7](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T161411Z-classify-robotsix-chat-selfreview-add-as-3da7), [#20260624T161411Z-classify-robotsix-chat-version-check-add-68e9](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T161411Z-classify-robotsix-chat-version-check-add-68e9), [#20260624T161411Z-register-version-check-module-in-manifest-f9a2](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T161411Z-register-version-check-module-in-manifest-f9a2), [#20260702T162931Z-migrate-robotsix-chat-to-consume-boardht-f450](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T162931Z-migrate-robotsix-chat-to-consume-boardht-f450), [#20260623T163048Z-preserve-chat-history-across-idle-timeou-d7ac](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T163048Z-preserve-chat-history-across-idle-timeou-d7ac), [#20260701T163404Z-track-external-pr-robotsix-chat-336-41e1](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T163404Z-track-external-pr-robotsix-chat-336-41e1), [#20260622T165124Z-extract-common-failingclient-helper-in-t-e157](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T165124Z-extract-common-failingclient-helper-in-t-e157), [#20260622T165124Z-extract-shared-ci-verify-script-from-dup-1c8e](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T165124Z-extract-shared-ci-verify-script-from-dup-1c8e), [#20260622T165125Z-add-editorconfig-for-consistent-editor-d-2ce5](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T165125Z-add-editorconfig-for-consistent-editor-d-2ce5), [#20260626T165506Z-add-missing-calendar-cache-ttl-env-var-o-49fb](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T165506Z-add-missing-calendar-cache-ttl-env-var-o-49fb), [#20260623T165841Z-add-uv-lock-check-to-ci-for-lockfile-free-a891](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T165841Z-add-uv-lock-check-to-ci-for-lockfile-fre-a891), [#20260623T165841Z-consolidate-stubagent-failingagent-in-te-fc4f](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T165841Z-consolidate-stubagent-failingagent-in-te-fc4f), [#20260623T165841Z-enable-furb-refurb-ruleset-in-ruff-confi-88cc](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T165841Z-enable-furb-refurb-ruleset-in-ruff-confi-88cc), [#20260623T165841Z-extract-three-way-install-fake-agent-com-08bc](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T165841Z-extract-three-way-install-fake-agent-com-08bc), [#20260623T165841Z-make-runner-py-frame-builders-reuse-even-da31](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T165841Z-make-runner-py-frame-builders-reuse-even-da31), [#20260624T170141Z-add-deptry-to-ci-pipeline-649f](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T170141Z-add-deptry-to-ci-pipeline-649f), [#20260624T170141Z-add-deptry-to-ci-pipeline-configured-but-649f](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T170141Z-add-deptry-to-ci-pipeline-configured-but-649f), [#20260624T170141Z-eliminate-internal-conftest-py-ct-suffix-015e](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T170141Z-eliminate-internal-conftest-py-ct-suffix-015e), [#20260624T170141Z-extract-fakecoro-3-way-test-helper-dupli-fea0](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T170141Z-extract-fakecoro-3-way-test-helper-dupli-fea0), [#20260626T170255Z-add-calendar-cache-ttl-env-var-override-04c3](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T170255Z-add-calendar-cache-ttl-env-var-override-04c3), [#20260625T170302Z-classify-4-chat-source-files-6-chat-test-3496](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T170302Z-classify-4-chat-source-files-6-chat-test-3496), [#20260625T170302Z-classify-src-robotsix-chat-board-reader-b471](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T170302Z-classify-src-robotsix-chat-board-reader-b471), [#20260625T170302Z-classify-src-robotsix-chat-mill-retry-qu-ae32](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T170302Z-classify-src-robotsix-chat-mill-retry-qu-ae32), [#20260625T170302Z-classify-tests-config-test-system-prompt-930a](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T170302Z-classify-tests-config-test-system-prompt-930a), [#20260627T170627Z-add-missing-pending-questions-enabled-en-88bc](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T170627Z-add-missing-pending-questions-enabled-en-88bc), [#20260627T170627Z-four-pending-question-frame-functions-in-3186](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T170627Z-four-pending-question-frame-functions-in-3186), [#20260629T171920Z-add-missing-sse-pending-question-answer-57e6](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T171920Z-add-missing-sse-pending-question-answere-57e6), [#20260623T172412Z-agent-md-testing-conventions-use-the-ins-1e7b](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T172412Z-agent-md-testing-conventions-use-the-ins-1e7b), [#20260622T173541Z-implement-persistent-server-to-browser-s-b004](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T173541Z-implement-persistent-server-to-browser-s-b004), [#20260703T173811Z-missing-subsessions-transcript-max-entri-13ca](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T173811Z-missing-subsessions-transcript-max-entri-13ca), [#20260626T175115Z-reorganize-module-robotsix-chat-broker-c-7fff](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T175115Z-reorganize-module-robotsix-chat-broker-c-7fff), [#20260703T180457Z-migrate-robotsix-chat-to-use-shared-scan-31b9](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T180457Z-migrate-robotsix-chat-to-use-shared-scan-31b9), [#20260625T181600Z-extract-duplicated-mockresponse-install-a997](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T181600Z-extract-duplicated-mockresponse-install-a997), [#20260625T181601Z-pin-reusable-workflow-main-references-to-12a9](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T181601Z-pin-reusable-workflow-main-references-to-12a9), [#20260625T181601Z-sha-pin-third-party-github-actions-acros-09f6](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T181601Z-sha-pin-third-party-github-actions-acros-09f6), [#20260624T181639Z-add-an-architecture-overview-document-to-75cc](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T181639Z-add-an-architecture-overview-document-to-75cc), [#20260624T181639Z-move-tests-test-version-check-py-into-a-3d5e](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T181639Z-move-tests-test-version-check-py-into-a-3d5e), [#20260624T181639Z-refactor-spawn-check-loop-in-chat-loops-a2a6](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T181639Z-refactor-spawn-check-loop-in-chat-loops-a2a6), [#20260623T181952Z-background-delegate-task-results-are-not-4637](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T181952Z-background-delegate-task-results-are-not-4637), [#20260626T182108Z-consolidate-duplicated-added-frame-update-353c](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T182108Z-consolidate-duplicated-added-frame-updat-353c), [#20260626T182108Z-extract-duplicated-install-fake-agent-co-0adc](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T182108Z-extract-duplicated-install-fake-agent-co-0adc), [#20260626T182108Z-move-install-mock-dual-client-from-test-fbba](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T182108Z-move-install-mock-dual-client-from-test-fbba), [#20260626T182108Z-split-src-robotsix-chat-config-py-1601-l-55a1](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T182108Z-split-src-robotsix-chat-config-py-1601-l-55a1), [#20260625T182426Z-agent-md-ci-workflow-conventions-all-thi-0588](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T182426Z-agent-md-ci-workflow-conventions-all-thi-0588), [#20260627T182649Z-extract-eventbus-setup-helper-from-tests-b956](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T182649Z-extract-eventbus-setup-helper-from-tests-b956), [#20260627T182649Z-split-src-robotsix-chat-chat-server-py-1-4507](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T182649Z-split-src-robotsix-chat-chat-server-py-1-4507), [#20260628T182945Z-extract-duplicated-blocked-scope-precond-436e](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T182945Z-extract-duplicated-blocked-scope-precond-436e), [#20260628T182945Z-extract-shared-request-validation-boiler-6dee](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T182945Z-extract-shared-request-validation-boiler-6dee), [#20260622T183327Z-ci-failure-release-image-on-main-3a35](https://github.com/damien-robotsix/robotsix-chat/issues/20260622T183327Z-ci-failure-release-image-on-main-3a35), [#20260625T183615Z-ci-failure-release-image-on-main-0fc3](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T183615Z-ci-failure-release-image-on-main-0fc3), [#20260627T183923Z-ci-fix-out-of-scope-ci-failure-pre-commi-41f5](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T183923Z-ci-fix-out-of-scope-ci-failure-pre-commi-41f5), [#20260629T183929Z-add-sse-frame-type-constant-synchronisat-2aa9](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T183929Z-add-sse-frame-type-constant-synchronisat-2aa9), [#20260629T183929Z-extract-shared-parse-int-parse-float-uti-4787](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T183929Z-extract-shared-parse-int-parse-float-uti-4787), [#20260629T183929Z-update-setup-uv-action-from-v6-8-0-to-la-d281](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T183929Z-update-setup-uv-action-from-v6-8-0-to-la-d281), [#20260628T184155Z-consolidate-modules-robotsix-chat-diagno-4ec7](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T184155Z-consolidate-modules-robotsix-chat-diagno-4ec7), [#20260625T185336Z-add-test-coverage-for-9-untested-setting-ef15](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T185336Z-add-test-coverage-for-9-untested-setting-ef15), [#20260625T185336Z-extract-image-validation-from-chat-endpo-2c4f](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T185336Z-extract-image-validation-from-chat-endpo-2c4f), [#20260625T185336Z-extract-inner-tool-closures-from-build-c-99ec](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T185336Z-extract-inner-tool-closures-from-build-c-99ec), [#20260623T190541Z-backend-accept-image-attachments-on-post-160e](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T190541Z-backend-accept-image-attachments-on-post-160e), [#20260623T190541Z-frontend-image-upload-attach-ui-in-the-c-9699](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T190541Z-frontend-image-upload-attach-ui-in-the-c-9699), [#20260630T190927Z-add-unit-tests-for-loop-reply-frame-buil-a5f6](https://github.com/damien-robotsix/robotsix-chat/issues/20260630T190927Z-add-unit-tests-for-loop-reply-frame-buil-a5f6), [#20260625T190927Z-enable-cost-reconciliation-periodic-work-175c](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T190927Z-enable-cost-reconciliation-periodic-work-175c), [#20260630T190927Z-extract-shared-jsonstorebase-t-from-4-js-0e48](https://github.com/damien-robotsix/robotsix-chat/issues/20260630T190927Z-extract-shared-jsonstorebase-t-from-4-js-0e48), [#20260630T190927Z-replace-bare-sse-event-type-string-liter-e349](https://github.com/damien-robotsix/robotsix-chat/issues/20260630T190927Z-replace-bare-sse-event-type-string-liter-e349), [#20260623T191241Z-add-a-stop-button-to-the-check-loop-ui-p-16db](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T191241Z-add-a-stop-button-to-the-check-loop-ui-p-16db), [#20260623T191241Z-add-stop-check-loop-and-list-check-loops-cc39](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T191241Z-add-stop-check-loop-and-list-check-loops-cc39), [#20260701T191856Z-extend-check-sse-event-types-py-to-scan-2751](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T191856Z-extend-check-sse-event-types-py-to-scan-2751), [#20260701T191856Z-replace-pip-audit-pre-commit-hook-with-u-933e](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T191856Z-replace-pip-audit-pre-commit-hook-with-u-933e), [#20260702T192110Z-extract-shared-dict-to-object-mapping-in-dcae](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T192110Z-extract-shared-dict-to-object-mapping-in-dcae), [#20260702T192110Z-extract-shared-terminal-close-tail-in-su-911b](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T192110Z-extract-shared-terminal-close-tail-in-su-911b), [#20260702T192110Z-extract-subsession-route-preamble-boiler-f4ae](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T192110Z-extract-subsession-route-preamble-boiler-f4ae), [#20260702T192110Z-remove-stale-pip-audit-references-from-p-ef38](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T192110Z-remove-stale-pip-audit-references-from-p-ef38), [#20260701T192306Z-ci-fix-out-of-scope-ci-failure-zizmor-de-cc7f](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T192306Z-ci-fix-out-of-scope-ci-failure-zizmor-de-cc7f), [#20260623T192631Z-pre-existing-mypy-errors-in-3-files-8-er-7d1f](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T192631Z-pre-existing-mypy-errors-in-3-files-8-er-7d1f), [#20260629T193142Z-classify-src-robotsix-chat-chat-server-i-74c3](https://github.com/damien-robotsix/robotsix-chat/issues/20260629T193142Z-classify-src-robotsix-chat-chat-server-i-74c3), [#20260701T193803Z-ci-fix-out-of-scope-ci-failure-zizmor-de-e75e](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T193803Z-ci-fix-out-of-scope-ci-failure-zizmor-de-e75e), [#20260623T194047Z-agent-md-testing-conventions-when-a-chat-633a](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T194047Z-agent-md-testing-conventions-when-a-chat-633a), [#20260623T194047Z-update-test-auth-py-mockagent-to-match-c-fcf0](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T194047Z-update-test-auth-py-mockagent-to-match-c-fcf0), [#20260623T194221Z-agent-md-when-a-chatagent-protocol-param-ebdd](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T194221Z-agent-md-when-a-chatagent-protocol-param-ebdd), [#20260703T194311Z-dry-componentagentresponder-construction-cf72](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T194311Z-dry-componentagentresponder-construction-cf72), [#20260703T194311Z-extract-duplicate-fetch-and-wrap-preambl-6311](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T194311Z-extract-duplicate-fetch-and-wrap-preambl-6311), [#20260703T194311Z-extract-persistence-layer-from-conversat-96f6](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T194311Z-extract-persistence-layer-from-conversat-96f6), [#20260703T194311Z-extract-repeated-env-set-closure-to-modu-31ac](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T194311Z-extract-repeated-env-set-closure-to-modu-31ac), [#20260624T194707Z-document-mail-configuration-in-example-c-8881](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T194707Z-document-mail-configuration-in-example-c-8881), [#20260623T195245Z-robotsix-chat-clean-up-stopped-check-loo-a9f6](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T195245Z-robotsix-chat-clean-up-stopped-check-loo-a9f6), [#20260623T201341Z-robotsix-chat-check-loop-per-tick-feedba-255a](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T201341Z-robotsix-chat-check-loop-per-tick-feedba-255a), [#20260630T201749Z-classify-16-unclassified-docs-files-assi-d5b7](https://github.com/damien-robotsix/robotsix-chat/issues/20260630T201749Z-classify-16-unclassified-docs-files-assi-d5b7), [#20260624T202755Z-enable-state-sync-periodic-workflow-for-adaf](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T202755Z-enable-state-sync-periodic-workflow-for-adaf), [#20260703T203034Z-enable-triage-boilerplate-periodic-workf-dcaf](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T203034Z-enable-triage-boilerplate-periodic-workf-dcaf), [#20260703T203034Z-migrate-robotsix-chat-to-use-shared-lint-0435](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T203034Z-migrate-robotsix-chat-to-use-shared-lint-0435), [#20260701T203156Z-track-external-pr-robotsix-chat-353-7f44](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T203156Z-track-external-pr-robotsix-chat-353-7f44), [#20260623T203706Z-broker-add-a-monitoring-ui-to-observe-re-4ca7](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T203706Z-broker-add-a-monitoring-ui-to-observe-re-4ca7), [#20260703T203801Z-ci-fix-out-of-scope-ci-failure-lint-work-8c83](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T203801Z-ci-fix-out-of-scope-ci-failure-lint-work-8c83), [#20260623T203856Z-robotsix-chat-update-the-assistant-s-own-838a](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T203856Z-robotsix-chat-update-the-assistant-s-own-838a), [#20260623T204239Z-robotsix-chat-give-the-assistant-a-writa-ff6c](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T204239Z-robotsix-chat-give-the-assistant-a-writa-ff6c), [#20260623T204251Z-robotsix-chat-governance-for-assistant-s-45f3](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T204251Z-robotsix-chat-governance-for-assistant-s-45f3), [#20260623T205618Z-add-discovery-inspect-configure-client-t-1220](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T205618Z-add-discovery-inspect-configure-client-t-1220), [#20260623T205618Z-define-validated-config-get-set-contract-57c2](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T205618Z-define-validated-config-get-set-contract-57c2), [#20260623T205618Z-embed-the-self-monitoring-self-configuri-ddc1](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T205618Z-embed-the-self-monitoring-self-configuri-ddc1), [#20260703T210105Z-ci-fix-out-of-scope-ci-failure-lint-work-de62](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T210105Z-ci-fix-out-of-scope-ci-failure-lint-work-de62), [#20260625T210152Z-echo-the-original-pending-question-text-9027](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T210152Z-echo-the-original-pending-question-text-9027), [#20260701T210704Z-cleanup-module-robotsix-chat-diagnostics-9060](https://github.com/damien-robotsix/robotsix-chat/issues/20260701T210704Z-cleanup-module-robotsix-chat-diagnostics-9060), [#20260623T210918Z-gate-sub-agent-status-output-behind-a-ma-e2f0](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T210918Z-gate-sub-agent-status-output-behind-a-ma-e2f0), [#20260623T210922Z-right-size-model-tier-route-trivial-poll-7bae](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T210922Z-right-size-model-tier-route-trivial-poll-7bae), [#20260623T210926Z-provide-a-synchronous-create-ticket-tool-179e](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T210926Z-provide-a-synchronous-create-ticket-tool-179e), [#20260623T210933Z-tighten-sub-agent-prompt-efficiency-check-5a52](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T210933Z-tighten-sub-agent-prompt-efficiency-chec-5a52), [#20260625T211215Z-add-list-read-tools-for-current-pending-f934](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T211215Z-add-list-read-tools-for-current-pending-f934), [#20260703T211754Z-boilerplate-deterministic-source-auto-ap-a9b0](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T211754Z-boilerplate-deterministic-source-auto-ap-a9b0), [#20260624T212653Z-chat-agent-hard-block-delegate-task-for-0619](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T212653Z-chat-agent-hard-block-delegate-task-for-0619), [#20260624T212656Z-chat-agent-reduce-consult-mill-timeout-a-214b](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T212656Z-chat-agent-reduce-consult-mill-timeout-a-214b), [#20260624T212659Z-chat-agent-prevent-duplicate-parallel-ch-f2d4](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T212659Z-chat-agent-prevent-duplicate-parallel-ch-f2d4), [#20260624T212702Z-chat-agent-dedup-ticket-filing-before-su-6ed5](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T212702Z-chat-agent-dedup-ticket-filing-before-su-6ed5), [#20260624T212705Z-chat-claude-sdk-agent-cache-board-state-2662](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T212705Z-chat-claude-sdk-agent-cache-board-state-2662), [#20260624T212708Z-chat-agents-enforce-the-three-sentences-236a](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T212708Z-chat-agents-enforce-the-three-sentences-236a), [#20260624T212711Z-chat-agent-stop-redundant-tool-loading-n-a0f3](https://github.com/damien-robotsix/robotsix-chat/issues/20260624T212711Z-chat-agent-stop-redundant-tool-loading-n-a0f3), [#20260625T213304Z-add-a-makefile-wrapping-common-uv-run-co-5750](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T213304Z-add-a-makefile-wrapping-common-uv-run-co-5750), [#20260625T213420Z-check-loop-emit-only-delta-changed-state-eaeb](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T213420Z-check-loop-emit-only-delta-changed-state-eaeb), [#20260625T213438Z-route-monitoring-status-check-check-loop-8776](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T213438Z-route-monitoring-status-check-check-loop-8776), [#20260625T213443Z-check-loop-skip-forced-board-calendar-re-a1aa](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T213443Z-check-loop-skip-forced-board-calendar-re-a1aa), [#20260626T215106Z-chat-check-loop-make-the-monitor-statefu-13c8](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T215106Z-chat-check-loop-make-the-monitor-statefu-13c8), [#20260626T215108Z-chat-per-tick-board-read-cache-forbid-re-0258](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T215108Z-chat-per-tick-board-read-cache-forbid-re-0258), [#20260623T215525Z-backend-multi-session-conversation-store-c1bf](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T215525Z-backend-multi-session-conversation-store-c1bf), [#20260623T215525Z-ui-session-list-new-chat-and-switcher-in-8727](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T215525Z-ui-session-list-new-chat-and-switcher-in-8727), [#20260627T220821Z-chat-check-loop-stop-zombie-ticks-after-7835](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T220821Z-chat-check-loop-stop-zombie-ticks-after-7835), [#20260627T220834Z-chat-enforce-tool-call-first-for-board-t-fc6b](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T220834Z-chat-enforce-tool-call-first-for-board-t-fc6b), [#20260702T221042Z-classify-tests-common-subsession-fakes-p-cc9b](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T221042Z-classify-tests-common-subsession-fakes-p-cc9b), [#20260702T221042Z-cleanup-module-robotsix-chat-common-path-4de1](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T221042Z-cleanup-module-robotsix-chat-common-path-4de1), [#20260623T221042Z-implement-conversationstore-recent-active-3191](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T221042Z-implement-conversationstore-recent-activ-3191), [#20260702T221042Z-reorganize-module-robotsix-chat-board-al-b76e](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T221042Z-reorganize-module-robotsix-chat-board-al-b76e), [#20260623T221243Z-agent-md-submodule-layout-the-broker-src-d974](https://github.com/damien-robotsix/robotsix-chat/issues/20260623T221243Z-agent-md-submodule-layout-the-broker-src-d974), [#20260626T221635Z-add-a-pull-request-template-md-with-a-co-75e4](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T221635Z-add-a-pull-request-template-md-with-a-co-75e4), [#20260627T222000Z-add-markdownlint-cli2-and-mdformat-to-pr-6111](https://github.com/damien-robotsix/robotsix-chat/issues/20260627T222000Z-add-markdownlint-cli2-and-mdformat-to-pr-6111), [#20260628T222249Z-add-cov-cov-report-term-missing-to-make-e23b](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T222249Z-add-cov-cov-report-term-missing-to-make-e23b), [#20260625T230529Z-raise-board-manager-consult-mill-respons-335f](https://github.com/damien-robotsix/robotsix-chat/issues/20260625T230529Z-raise-board-manager-consult-mill-respons-335f), [#20260626T231533Z-threaded-conversations-for-pending-quest-dcfe](https://github.com/damien-robotsix/robotsix-chat/issues/20260626T231533Z-threaded-conversations-for-pending-quest-dcfe), [#20260702T231607Z-add-dependabot-auto-merge-caller-workflo-9b4b](https://github.com/damien-robotsix/robotsix-chat/issues/20260702T231607Z-add-dependabot-auto-merge-caller-workflo-9b4b), [#20260630T235602Z-add-spell-checking-with-crate-ci-typos-i-04e5](https://github.com/damien-robotsix/robotsix-chat/issues/20260630T235602Z-add-spell-checking-with-crate-ci-typos-i-04e5), [#20260628T235755Z-remove-orphaned-robotsix-mill-periodic-l-21b2](https://github.com/damien-robotsix/robotsix-chat/issues/20260628T235755Z-remove-orphaned-robotsix-mill-periodic-l-21b2)

### Breaking Changes

- BREAKING: Config migrated from YAML cascade to JSON (`robotsix-config`).

  The `config/chat.local.yaml` config file and all env-var config overrides (`LLMIO_*`, `MEMORY_*`,
  `LANGFUSE_*`, `MILL_*`, `CALENDAR_*`, etc.) are no longer read by the app. Only
  `ROBOTSIX_CONFIG_FILE` (file locator) is consumed from env.

  OPS CUTOVER — required before redeployment: Transcribe the following from central-deploy's env store
  into `/home/app/config/config.json` on the deploy host BEFORE restarting:

  | Env var (old)                   | JSON path                      | Known / Notes                         |
  | ------------------------------- | ------------------------------ | ------------------------------------- |
  | LLMIO_MODEL_LEVEL               | llmio_model_level              | 4                                     |
  | LLMIO_API_KEY                   | llmio_api_key                  | from env store                        |
  | MEMORY_ENABLED                  | memory.enabled                 | true                                  |
  | MEMORY_LLM_API_KEY              | memory.llm.api_key             | OpenRouter key                        |
  | MEMORY_EMBEDDING_ENDPOINT       | memory.embedding.endpoint      | <https://embed.robotsix.net/v1>       |
  | MEMORY_EMBEDDING_API_KEY        | memory.embedding.api_key       | bearer token                          |
  | LANGFUSE_PUBLIC_KEY             | langfuse.public_key            | main project key                      |
  | LANGFUSE_SECRET_KEY             | langfuse.secret_key            | main project secret                   |
  | LANGFUSE_HOST (if set)          | langfuse.host                  | custom host or omit for cloud default |
  | MEMORY_LANGFUSE_PUBLIC_KEY      | memory.langfuse.public_key     | robotsix-chat-cognee project key      |
  | MEMORY_LANGFUSE_SECRET_KEY      | memory.langfuse.secret_key     | robotsix-chat-cognee project secret   |
  | MILL_ENABLED / MILL\_\*         | mill.enabled / mill.\*         | from env store                        |
  | MILL_BROKER_TOKEN               | mill.broker_token              | from env store                        |
  | CALENDAR_ENABLED / CALENDAR\_\* | calendar.enabled / calendar.\* | from env store                        |
  | CALENDAR_BROKER_TOKEN           | calendar.broker_token          | from env store                        |
  | AUTH\_\* (gateway-only)         | N/A — central-deploy gateway   | no change needed                      |

  WARNING: The 2026-07-03 deployment previously lost env values during a restart. Verify all env store
  values before cutover. ([#20260703T153121Z-config-json-migration](https://github.com/damien-robotsix/robotsix-chat/issues/20260703T153121Z-config-json-migration))

## [Unreleased]

- Registered 16 unclassified docs files in `docs/modules.yaml`: 10 project-wide docs under
  `robotsix_chat`, plus 6 mkdocstrings API stubs under their respective modules
  (`robotsix_chat.llm`, `robotsix_chat.chat`, `robotsix_chat.config`, `robotsix_chat.memory`,
  `robotsix_chat.mill`).

- Extracted `JsonStoreBase[T]` generic base class for JSON-persisted dataclass stores, eliminating
  ~100 lines of duplicated persistence boilerplate across `DiagnosticStore`, `KnowledgeStore`,
  `FixProposalStore`, and `EffectivenessStore`.

- Updated Mail section docs to reflect direct HTTP API (no broker), with correct env vars
  `MAIL_API_BASE_URL`/`MAIL_API_TOKEN` and correct `MAIL_TIMEOUT` default of `30.0`. Removed stale
  broker-based entries.

- Refactored `_check_loop_worker` in `loops.py`: extracted `_build_tick_prompt`,
  `_run_tick_iteration`, and `_evaluate_stop_conditions` helpers to flatten the monolithic
  while-loop body and consolidate stop-decision logic into a single decision point.

- Added `scripts/check_sse_event_types.py` CI gate to verify that SSE event-type string constants in
  `src/robotsix_chat/chat/events.py` stay in sync with the browser UI
  (`src/robotsix_chat/ui/index.html`). Added `make check-sse-types` target and integrated into the
  `ci.yml` workflow.

- Extracted `_parse_int` and `_parse_float` utility functions in
  `src/robotsix_chat/config/constants.py`, replacing ~20 repetitive inline try/except blocks across
  `env_builders.py` and `settings.py` with centralized parsing helpers.

- Added `SSE_PENDING_QUESTION_ANSWERED_TYPE` constant to `src/robotsix_chat/chat/events.py` and used
  it in `store.py` and `test_store.py` in place of the raw string literal.

- Updated stale references to `src/robotsix_chat/config.py` (moved into `config/` package) across
  governance docs, AGENT.md, SECURITY.md, and config_contract.py docstring to reflect the split:
  `config/settings.py` for `Settings`/`SYSTEM_PROMPT_VERSION`/`agent_instruction`,
  `config/constants.py` for `_YAML_PATH_TO_FIELD`, and `config/` for the package table in
  architecture docs.

- Documented `DIAGNOSTICS_*` environment variables in `docs/configuration.md`.

- Documented `LLMIO_CHECK_LOOP_MODEL` / `llmio.check_loop_model` in `docs/configuration.md`
  top-level settings table.

- Refactored shared request-validation boilerplate in route handlers (`_parse_json_body`,
  `_get_session_id`, `_cleanup_session` helpers), eliminating 9 internal clone pairs. No behaviour
  changes.

- Documented Skills subsystem env vars (`SKILLS_ENABLED`, `SKILLS_MANIFESTS_DIR`) in
  `docs/configuration.md`.

- Migrated mail integration from agent-comm broker to direct HTTP. Replaced
  `MailClient(BaseBrokeredClient)` with a direct HTTP client calling the auto-mail board server
  (`GET /board-content`, `GET /email/{id}/status`, `POST /move`, `POST /delete`, `POST /archive`,
  `POST /run-triage`). Replaced the single `consult_mail` NL tool with six discrete tools
  (`get_mail_board`, `get_mail_email_status`, `move_mail_email`, `delete_mail_email`,
  `archive_mail_email`, `run_mail_triage`). Removed all broker fields from `MailSettings` (now uses
  `api_base_url` and `api_token`). Added `content` and `follow_redirects` parameters to
  `safe_http_request`. No broker dependency remains in the mail module.

- Migrated component client from agent-comm broker to direct HTTP. Replaced `ComponentAgentClient`
  (broker-based, using `BrokeredRequester`) with a direct HTTP client calling each component agent's
  `/api/component-agent/monitor` and `/api/component-agent/config` endpoints. Changed
  `ComponentTarget.agent_id` to `ComponentTarget.base_url`. Removed all broker fields
  (`broker_host`, `broker_port`, `broker_scheme`, `broker_token`, `agent_id`) from
  `ComponentClientSettings`. Removed the `robotsix_agent_comm` availability check from
  `build_component_tools`. No broker dependency remains in the component_client module.

- Added `--cov --cov-report=term-missing` to the `test` Makefile target so local test runs collect
  and report coverage automatically.

- `consult_mill` now caches board-read results within a single turn/tick, avoiding redundant broker
  round-trips when the LLM re-reads the same board data. The cache is keyed by the exact request
  string and is reset at the start of each agent `stream()` invocation.

- Added skill/capability loading system (`robotsix_chat.skills`): a declarative mechanism that
  discovers broker capabilities from YAML manifests (`config/skills/*.skill.yaml`) and surfaces each
  capability as an LLM-callable tool with proper parameter schemas, per-capability scoping, and
  graceful error handling. Gated behind `skills.enabled` (default `false`) with `SKILLS_*` env-var
  overrides. This is the foundational piece for per-broker migration tickets that will replace the
  hardcoded `build_*_tools()` pattern.

- Check loops now auto-halt when the result text indicates a terminal state
  (`closed`/`done`/`resolved`/`completed`) via the `stop_when` predicate, preventing zombie ticks
  after the monitored item reaches its terminal state. Also fixed a latent bug where the injected
  `stop_check_loop` tool was silently missing when the tick agent had no other tools configured.

- Board narrative hallucination guard: agent responses that describe board/ticket state without a
  prior `list_board_tickets` / `read_board_ticket` tool call in the same turn are now blocked and
  replaced with a prompt to read the board first. Uses a `contextvars.ContextVar` tracker set by
  every board-reader tool and a keyword/pattern heuristic on the response text.

- Diagnostics subsystem: failure-category enum and deterministic keyword/regex categorizer for
  BLOCKED-ticket diagnostic bundles. Includes `CLONE_TARGET`, `CI_FAILURE`, `DEPENDENCY`,
  `REFINEMENT`, and `OTHER` categories — `categorize_record()` runs inline during capture, and
  `recategorize_blocked_event()` is the agent tool for manual overrides.

- Added blocked-ticket diagnostics capture (`diagnostics`): a new module that automatically records
  diagnostic bundles when tickets transition to BLOCKED state. Includes `DiagnosticStore` (JSON
  persistence), `DiagnosticCapture` (poll-based BLOCKED detection via `BoardReader`), and a
  `list_diagnostic_records` agent tool. Config is gated behind `diagnostics.enabled` (default
  `false`) with `DIAGNOSTICS_*` env-var overrides.

- Added diagnostics module (`robotsix_chat.diagnostics`) with systemic fix surfacing: captures
  diagnostic bundles, detects recurring failure categories (configurable recurrence threshold and
  window), and auto-generates fix proposals from curated category→template mappings. Proposals are
  surfaced for agent/human review and explicitly applied or rejected — never auto-applied.

- Added agent tools: `list_diagnostic_events`, `check_recurring_categories`, `list_fix_proposals`,
  `apply_fix`, `reject_fix`.

- Check-loop worker now auto-pauses (stops) after two consecutive unchanged (NO_CHANGE) ticks,
  preventing silent indefinite polling on stuck/idle monitored items. The loop is stopped with a
  descriptive reason (`auto_paused: N consecutive unchanged ticks`) published as a `loop_stopped`
  frame so the user receives a single clear notification. Configured via the new
  `auto_pause_unchanged_ticks` parameter (default 2; set to 0 to disable).

- Check loops started via `start_check_loop` now carry a built-in terminal-state predicate
  (`_terminal_state_result`) that self-stops the loop immediately when the tick result indicates a
  terminal ticket/thread state (e.g. "is now closed", "has been done"), rather than waiting for the
  auto-pause threshold. A new system-prompt rule instructs the tick sub-agent to call
  `stop_check_loop` explicitly as the primary mechanism, with the programmatic predicate as a
  belt-and-suspenders backup. `SYSTEM_PROMPT_VERSION` bumped to 14.

- System-prompt guidance (v14): tick sub-agents must call `stop_check_loop` when the monitored item
  reaches a terminal state; pending decision questions must be asked once and not repeated on
  subsequent unchanged ticks.

- Calendar/tasks tools now use `BrokeredAgent.send_request()` directly instead of the deprecated
  `BrokeredRequester` (removed from `robotsix_agent_comm`). The `CalendarClient` no longer extends
  `BaseBrokeredClient`; TTL query caching is preserved. Broker- unreachability detection now
  recognises the SDK's native `AgentNotFoundError`, `DeliveryError`, and `TransportTimeoutError`
  exception types in addition to message-fragment heuristics.

- When replying to a Pending Question, the chat transcript now shows a recall line referencing the
  original question text alongside the submitted answer ("Re: '<question>' — <answer>"). This is
  display-only context and does not alter the agent payload.

- Improved Pending Questions panel readability: higher contrast text and slightly larger font sizes
  across the panel.

- Added direct-repository-capability (`direct_repo`): the chat agent can now push branches and open
  PRs against repos in the robotsix-mill GitHub App installation scope, authenticating as the app
  (JWT → short-lived installation token). Actions are gated behind a BLOCKED-state precondition and
  the repo set is resolved dynamically from the installation at action time. PRs are opened in a
  reviewable state with no auto-merge; no merge capability exists on this path.

- Added `check_loop_model` config (default `"haiku"`, env `LLMIO_CHECK_LOOP_MODEL`) so recurring
  monitoring / status-check check-loop ticks run on the cheapest subscription tier, independently of
  the `subagent_model` used for delegation tasks. Escalation to the foreground model (Opus) is
  automatic via tick-triggered foreground agent runs when a tick detects a substantive change.

  - Documented `direct_repo` configuration in `docs/configuration.md` (table section and YAML
    example).

### Changed

- Added an "Autonomy" section to the assistant system prompt instructing it to proactively perform
  safe, reversible actions without waiting for explicit human validation, while gating
  risky/irreversible actions behind human approval. Includes a concrete rule: check-loop sub-agents
  must call `stop_check_loop` when a verified terminal/completion state is reached instead of
  emitting repeated COMPLETED/NO_CHANGE reports. `SYSTEM_PROMPT_VERSION` bumped to 14.

- Extracted the three inner tool closures from `build_check_loop_tools` in `delegation.py` to
  module-level async functions (`_start_check_loop_tool`, `_stop_check_loop_tool`,
  `_list_check_loops_tool`) that take captured state as explicit keyword arguments, reducing nesting
  and making each tool independently testable.

- Scoped the "new tickets default to robotsix-mill" system-prompt claim to `consult_mill`
  specifically, replacing a false universal statement ("regardless of source") with accurate
  board-manager-default wording. `SYSTEM_PROMPT_VERSION` bumped to 12.

- Split `src/robotsix_chat/chat/server.py` (1656 lines) into a `server/` package with four modules
  (`routes.py`, `app.py`, `cli.py`, `__init__.py`) for improved maintainability. All public symbols
  are re-exported from `__init__.py` preserving backward compatibility.

- Folded the runtime `_AGENT_GUARD` hardening layer into the version-governed `agent_instruction`
  default so guard changes are tracked by `SYSTEM_PROMPT_VERSION`, the system prompt changelog,
  SHA256, and CI enforcement.

- Pending questions now support threaded conversations: users and the assistant can exchange
  multiple messages per question, visible inline in the Pending Questions panel.

- Pre-commit CI fixes: resolved ruff UP038 violations, vulture dead-code warnings, detect-secrets
  false positives, and missing EOF newlines across the codebase to satisfy the newly added
  pre-commit CI gate.

- Background-tasks side panel now has a close button (×) and responds to the Escape key; the
  tasks-toggle button acts as a true toggle (open/close). Closing the panel preserves in-memory task
  history.

- Extracted shared `BaseBrokeredClient` base class from `MillClient` and `CalendarClient`,
  eliminating ~40 lines of duplicated boilerplate.

### Fixed

- Pending-questions thread: each assistant reply is now posted exactly once per user submit, fixing
  a bug where identical assistant messages were double-posted in the thread when the agent's
  `append_to_pending_question_thread` tool and the background thread-processing task both appended
  the same reply.

- Corrected stale `calendar_agent_id` default from `calendar-agent-robotsix` to `robotsix-calendar`
  in `.env.example` and `config/chat.local.example.yaml` to match the code default in `config.py`.

- Check-loop worker now skips the LLM invocation when the previous tick's result matched the
  no-change predicate, reusing the prior result instead of re-sending the full prompt for a foregone
  NO_CHANGE reply. Saves ~80% of monitoring-loop input tokens on static/unchanged items.

- Consolidated duplicated `_added_frame`, `_updated_frame`, and `_answered_frame` builders into a
  single `_frame_for` helper in `src/robotsix_chat/pending_questions/store.py`, eliminating ~30
  lines of near-identical dict literal construction.

- Split `src/robotsix_chat/config.py` into a `config/` package (`constants`, `models`, `settings`,
  `env_builders`) with backward-compatible re-exports from `config/__init__.py`.

- Reorganized `tests/test_broker_client.py` into per-module subdirectory `tests/broker_client/`,
  aligning with the convention used by all other modules.

- Add `CALENDAR_CACHE_TTL` env-var override for `CalendarSettings.cache_ttl`, matching the existing
  `BOARD_READER_CACHE_TTL` and `VERSION_CHECK_CACHE_TTL` sibling patterns.

- Add `PENDING_QUESTIONS_ENABLED` env-var override for `PendingQuestionsSettings.enabled`, following
  the same pattern as `KNOWLEDGE_ENABLED` and other sibling `*_ENABLED` toggles.

- Fixed documented default for `calendar.calendar_agent_id` in `docs/configuration.md` to match code
  default `"robotsix-calendar"` (was `"calendar-agent-robotsix"`).

- Sync `agent.instruction` row in `docs/configuration.md` with the live `Settings.agent_instruction`
  default (add missing v9 enforcement sentence and a missing newline before the Efficiency section).

- Add CI enforcement test verifying `docs/configuration.md` mirrors the `agent.instruction` field
  default verbatim.

- Increase font sizes in the Pending Questions panel for improved readability.

- Document `board_reader.cache_ttl` / `BOARD_READER_CACHE_TTL` in the Board Reader section of
  `docs/configuration.md`.

- Fix docs: change `mail.broker_port` default from quoted `"443"` to unquoted `443` to match the
  actual code default and other `broker_port` entries.

- Add `Makefile` with phony targets (`install`, `test`, `lint`, `format`, `format-check`,
  `typecheck`, `security`, `clean`, `all`) wrapping common `uv run` developer commands.

- Add `list_pending_questions` and `get_pending_question` agent tools for reading the Pending
  Questions panel state (complementing the existing add/update/remove tools).

- Add cost reconciliation periodic work (`.robotsix-mill/periodic/cost_reconciliation.yaml`) for
  automated LLM cost tracking and reconciliation.

- Extracted image validation from `chat_endpoint` into a module-level `_parse_and_validate_images`
  helper in `chat/server.py`.

- Pinned all reusable workflow references to immutable commit SHAs and added CI workflow pinning
  conventions to `AGENT.md`.

- Optimised check-loop tool-use policy: when a tick includes a previous board-verified result, the
  guardrail no longer forces a redundant `consult_mill` call. Calendar/task query results are now
  cached per-session (TTL-driven, invalidated by mutations), eliminating repeated broker round-trips
  on steady-state ticks.

- Extracted duplicated `_MockResponse` / `_install_mock_client` mock helpers from four test files
  into a shared `tests/common/mock_helpers.py` module.

- Enabled periodic `board_cleanup` workflow to expire stale retry-queue entries, detect cache
  inconsistencies, and flag abandoned board duplicates.

- Added `POST /sessions/{session_id}/close` endpoint that marks a session as closed, stops all its
  check loops, and cancels all its in-flight background tasks. The response reports counts of
  stopped loops and cancelled tasks.

- Closed sessions are prevented from spawning new background work: `delegate_task` and
  `start_check_loop` tools refuse to operate when the session is marked closed.

- The `closed` flag is persisted across restarts and visible in the session list metadata.

- Added Pending Questions panel above the chat input: the agent can raise structured questions via
  `add_pending_question` / `update_pending_question` / `remove_pending_question` tools, the user
  sees them in real time over the existing SSE channel, and inline answers are fed back into the
  conversation.

- Documented `LLMIO_SUBAGENT_MODEL` env var in `docs/configuration.md`.

- Documented `server.min_check_loop_interval_seconds` / `MIN_CHECK_LOOP_INTERVAL_SECONDS` in
  configuration table.

- Documented `CONVERSATION_PERSIST_PATH` / `conversation.persist_path` in configuration reference.

- Added `server.max_check_loops` / `MAX_CHECK_LOOPS` to docs.

- Enabled `env_doc_sync` periodic workflow via `.robotsix-mill/periodic/env_doc_sync.yaml` presence
  file.

- Documented `VERSION_CHECK_*` env vars (6 vars) in `docs/configuration.md` under a new "Version
  Check" section.

- Documented `COMPONENT_CLIENT_*` env vars (7 vars) in `docs/configuration.md` under a new
  "Component Client" section.

- Removed 123 vendored `.local-deps/` files (anyio, starlette, idna, asgi_correlation_id) that were
  incorrectly committed; `.gitignore` already covers `local-deps/` and `*-deps/` patterns.

- Extracted shared `safe_http_request` helper to `robotsix_chat.common.http`, consolidating the
  duplicated 3-way `except (HTTPStatusError, TimeoutException, Exception)` cascade that appeared
  verbatim in `board_reader`, `refdocs`, and `version_check` HTTP clients. Callers now import
  `safe_http_request` and inspect the returned `HttpResult` instead of writing their own
  error-formatting boilerplate (~40 lines eliminated).

- Strengthened the `agent_instruction` Efficiency bullet to name prohibited output shapes (multi-row
  markdown tables, timeline/audit dumps, recap lists) and forbid repeating content already shown in
  the same conversation.

- Raised `mill.timeout` default from 300 s to 600 s (10 min); the board manager's synthesis
  legitimately exceeds 5 minutes in many cases, so the previous 5‑minute timeout caused spurious
  failures and client retries.

- Added request trimming to the mill retry queue: `BoardWriteRetryQueue` now drops the middle of
  over-long requests before persistence and resend (head+tail preservation with an omission marker),
  cutting ~4–5k-token broker retry calls down to ~1k tokens. Configurable via a new
  `max_request_chars` constructor parameter (default 4000).

- Documented `mail` configuration in `config/chat.local.example.yaml` and `MAIL_*` environment
  variables in `.env.example`.

- Refactored `spawn_check_loop` in `robotsix_chat.chat.loops`: extracted the 147-line nested
  `_worker` coroutine into a top-level `_check_loop_worker` and the board-read gate setup into
  `_setup_board_read_gate`, reducing nesting depth from 6 to 3.

- Added `docs/architecture.md` — system architecture overview covering the start-up flow, request
  lifecycle, subpackage inventory, and configuration cascade.

- Registered `robotsix_chat.version_check` package in `docs/modules.yaml` module manifest.

- Registered `robotsix_chat.component_agent` package in `docs/modules.yaml` module manifest.

- Registered `robotsix_chat.knowledge` module in `docs/modules.yaml`.

- Registered `robotsix_chat.component_client` module in `docs/modules.yaml`.

- Added `fail_under = 88` coverage threshold to `pyproject.toml` (`[tool.coverage.report]`) to
  ratchet-floor coverage and block regressions in CI.

- Documented the `broker_src/` submodule convention in `AGENT.md`: broker features must be developed
  in the upstream `robotsix-agent-comm` repo, not directly inside `broker_src/`.

- Added `create_board_ticket` tool to the board reader: a direct synchronous (inline) tool that
  creates tickets via `POST /tickets` on the board API, avoiding the token waste of spawning a
  background sub-agent via `delegate_task` for simple ticket filing.

- Added multi-session support to the chat UI: a sessions sidebar with "New chat" button and session
  list (title + last-active timestamp), click-to-switch with independent conversation state per
  session (DOM cleared, history and events stream re-keyed on session_id). All /chat, /history,
  /events, /loops calls now send session_id + owner_id. Page load auto-selects the server's active
  session (falling back to newest or locally stored).

- Added sub-agent efficiency rules to the agent system prompt: check tool availability before
  describing a plan and state missing tools in one sentence; answer in three sentences or fewer
  unless elaboration is requested; load tools once per session with a single capability check before
  branching.

- Added multi-session support to the conversation store: conversations are now addressable by
  `session_id` and grouped under `owner_id`, with per-owner session metadata (title, last-active
  timestamp, turn count) and an active session pointer. Sessions are persistent — history is never
  wiped on idle timeout. New `GET /sessions` and `POST /sessions` HTTP endpoints enable listing and
  creating sessions. Existing endpoints (`POST /chat`, `GET /history`, `GET /events`, `GET /loops`)
  accept `session_id` with backward-compatible `client_id` fallback. Persistence uses the same
  `.data/conversations.json` mechanism (legacy format auto-migrated on load). Added `persist_path`
  to `ConversationSettings` (configurable via `CONVERSATION_PERSIST_PATH`).

- Added `board_reader` module with `list_board_tickets` and `read_board_ticket` tools that query the
  SAME HTTP board API endpoint the user's browser UI consumes, giving the assistant read parity with
  the user. Uses bearer-token auth (configurable via `BOARD_READER_API_TOKEN`) and is disabled by
  default; independent of the broker-based mill integration.

- Added `include_previous_result` and `suppress_when` parameters to `spawn_check_loop`, enabling
  change-detection periodic checks where the sub-agent can compare against the prior iteration's
  result and suppress no-change tick notifications (no SSE frame, no conversation turn). The
  `start_check_loop` tool now accepts `include_previous_result` and automatically suppresses ticks
  whose result is the `NO_CHANGE` sentinel — so users are only notified when something actually
  changed.

- Added `docs/periodic-checks.md` documenting how the assistant sets up, lists, and cancels periodic
  board checks, including the recommended prompt pattern for change-detection with automatic
  no-change suppression.

- Increased default width of the background tasks slide-in panel from 340px to 420px to improve
  readability of task names and status text. Added a drag-to-resize handle on the left edge of the
  panel so users can adjust the width between 260px and 90vw to suit their needs.

- Added persistent, human-readable task tracking under `tasks/`: `tasks/TASKS.md` (active),
  `tasks/ARCHIVE.md` (completed), and `tasks/README.md` (format & workflow reference). Referenced
  from `AGENT.md` and `README.md` for cross-conversation discoverability.

- Fixed pre-existing mypy errors in `broker_client.py` (lazy import type-ignore),
  `test_broker_client.py` (mock function signature), and `test_auth.py` (missing `client_id`
  parameter in `_MockAgent.stream`).

- Added `stop_check_loop` and `list_check_loops` tools so the assistant agent can stop and inspect
  its own running check loops; both tools are scoped to the calling client for cross-session
  isolation.

- Added a Stop button to the Check Loops panel in the chat UI for cancelling running check loops via
  the existing `/loops/{loop_id}/stop` endpoint.

- Redesigned the check-loop panel to declutter and compact displayed rows: stopped/failed loops are
  now hidden (only running loops remain visible); each row shows an optional short `reason` (or
  truncated prompt), a fire-count + interval meta line, and a timestamped, truncated latest-feedback
  summary (never the full prompt or full result text). Added `reason` and `last_result_at` fields to
  `LoopInfo`, threaded through the SSE event frames, `GET /loops`, and the `start_check_loop` tool;
  persisted with backward-compat defaults so existing `.data/check_loops.json` files load cleanly.

- Added image attachment UI to the chat: file-picker button, clipboard paste, and drag-and-drop
  support for attaching PNG/JPEG/GIF/WebP images with a preview tray, per-thumbnail remove controls,
  and inline validation errors for unsupported types, oversized files, and max-count limits. Sent
  user bubbles now render attached image thumbnails.

- Added support for image attachments on `POST /chat`. Clients can now send an optional `images`
  array of `{"media_type": "<image/png|image/jpeg|image/gif|image/webp>", "data": "<base64>"}`
  objects alongside or instead of text. Images are forwarded as multimodal content to a
  vision-capable LLM (requires OpenRouter model level 1 or 2; the default level-3 claude_sdk path
  drops image content — see `docs/configuration.md`). New settings `max_images_per_message` (default
  8), `max_image_bytes` (default 5 MiB), and `allowed_image_media_types` control limits.

- Enabled Ruff's `FURB` (Refurb) ruleset to catch future idiomatic-Python anti-patterns.

- Replaced hardcoded frame-type strings in `runner.py`'s frame builders (`task_started_frame`,
  `task_completed_frame`, `task_failed_frame`) with the shared `SSE_TASK_*_TYPE` constants from
  `events.py`, so frame types stay consistent across the codebase.

- Conversation history is now persisted to `.data/conversations.json` (JSON, one write per completed
  exchange) so chat history survives a Docker container restart when the `.data` directory is on a
  persistent volume mount. The in-memory store loads saved conversations on startup.

- The per-conversation history cap was raised from 20 to 50 turns (most recent messages), matching
  the acceptance criterion for conversation retention across UI reloads and container restarts.

- The idle-timeout UI behaviour was changed: instead of clearing the entire chat area
  (`chatEl.innerHTML = ""`), an inline italic notice is now appended while all previous message
  bubbles remain visible — so the user can still scroll back through the conversation after
  returning from idle.

- Registered `robotsix_chat.calendar` in `docs/modules.yaml` (was a fully-fledged module but absent
  from the module manifest).

- Registered `robotsix_chat.selfreview` in `docs/modules.yaml` — a read-only digest of live
  conversation activity via `build_recent_activity_tools()` that exposes a `read_recent_activity`
  tool backed by the in-process `ConversationStore`.

- Added `pytest-xdist[psutil]` to the `dev` dependency group so the CI reusable workflow's `-n auto`
  flag works without `unrecognized arguments` errors.

- Fixed `spawn_check_loop` and `resume_check_loops` to use
  `settings.min_check_loop_interval_seconds` instead of the hardcoded module constant, so the
  configured value actually takes effect. Removed the now-unused `MIN_CHECK_LOOP_INTERVAL_SECONDS`
  module constant.

### Added

- Documented the `broker_src/` submodule layout convention in `AGENT.md`: broker features must be
  developed upstream in `damien-robotsix/robotsix-agent-comm` and pinned here as a commit update,
  not developed directly inside the submodule.

- `max_check_loops` and `min_check_loop_interval_seconds` configuration fields for check-loop
  registry limits, with env var overrides `MAX_CHECK_LOOPS` and `MIN_CHECK_LOOP_INTERVAL_SECONDS`.

- Comprehensive `docs/configuration.md` documenting all ~30 environment variables across server,
  auth, memory, mill, calendar, conversation, and refdocs settings.

- `query_tasks` and `query_calendar` tools now send domain-specific instruction strings
  (`"list tasks: …"` and `"list calendar events: …"`) so the upstream `robotsix-calendar` intent
  classifier correctly routes them to `list_tasks` and `list_events` respectively. Fixes
  `query_tasks` returning VEVENT calendar entries and `query_calendar` returning "No events found"
  for real events.

### Removed

- Stale `docs/user-guide/configuration.md` superseded by `docs/configuration.md`.

- Deleted four orphaned `pending_question_*_frame()` functions from `chat/events.py`
  (`pending_question_added_frame`, `pending_question_updated_frame`,
  `pending_question_removed_frame`, `pending_question_thread_message_frame`) — never called
  anywhere; `pending_questions/store.py` builds its own frames with additional
  `answer`/`answered_at` fields.

## [0.1.0] - Unreleased

### Added

- Initial release of robotsix-chat: a browser + SSE chat server exposing an LLM agent to human
  users.
- `robotsix-chat` CLI entry point.
- CI workflow with linting, type checking, tests, and security audit.
- Documentation site workflow.
