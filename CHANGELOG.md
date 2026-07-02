## 0.0.0 (unreleased)

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
