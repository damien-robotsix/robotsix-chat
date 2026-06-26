# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- towncrier release notes start -->

## [Unreleased]

- Add `Makefile` with phony targets (`install`, `test`, `lint`, `format`,
  `format-check`, `typecheck`, `security`, `clean`, `all`) wrapping common
  `uv run` developer commands.

- Add `list_pending_questions` and `get_pending_question` agent tools for reading the Pending Questions panel state (complementing the existing add/update/remove tools).

- Add cost reconciliation periodic work (`.robotsix-mill/periodic/cost_reconciliation.yaml`) for automated LLM cost tracking and reconciliation.

- Extracted image validation from ``chat_endpoint`` into a module-level
  ``_parse_and_validate_images`` helper in ``chat/server.py``.
- Pinned all reusable workflow references to immutable commit SHAs and added CI workflow pinning conventions to ``AGENT.md``.

- Optimised check-loop tool-use policy: when a tick includes a previous
  board-verified result, the guardrail no longer forces a redundant
  ``consult_mill`` call.  Calendar/task query results are now cached
  per-session (TTL-driven, invalidated by mutations), eliminating
  repeated broker round-trips on steady-state ticks.
- Extracted duplicated ``_MockResponse`` / ``_install_mock_client`` mock helpers
  from four test files into a shared ``tests/common/mock_helpers.py`` module.

- Enabled periodic ``board_cleanup`` workflow to expire stale retry-queue
  entries, detect cache inconsistencies, and flag abandoned board duplicates.
- Added ``POST /sessions/{session_id}/close`` endpoint that marks a session as
  closed, stops all its check loops, and cancels all its in-flight background
  tasks.  The response reports counts of stopped loops and cancelled tasks.
- Closed sessions are prevented from spawning new background work:
  ``delegate_task`` and ``start_check_loop`` tools refuse to operate when the
  session is marked closed.
- The ``closed`` flag is persisted across restarts and visible in the session
  list metadata.

- Added Pending Questions panel above the chat input: the agent can raise
  structured questions via ``add_pending_question`` / ``update_pending_question`` /
  ``remove_pending_question`` tools, the user sees them in real time over the
  existing SSE channel, and inline answers are fed back into the conversation.
- Documented ``LLMIO_SUBAGENT_MODEL`` env var in ``docs/configuration.md``.
- Documented `server.min_check_loop_interval_seconds` / `MIN_CHECK_LOOP_INTERVAL_SECONDS` in configuration table.
- Documented ``CONVERSATION_PERSIST_PATH`` / ``conversation.persist_path`` in
  configuration reference.
- Added ``server.max_check_loops`` / ``MAX_CHECK_LOOPS`` to docs.
- Enabled ``env_doc_sync`` periodic workflow via
  ``.robotsix-mill/periodic/env_doc_sync.yaml`` presence file.
- Documented ``VERSION_CHECK_*`` env vars (6 vars) in ``docs/configuration.md``
  under a new "Version Check" section.
- Removed 123 vendored ``.local-deps/`` files (anyio, starlette, idna,
  asgi_correlation_id) that were incorrectly committed; ``.gitignore`` already
  covers ``local-deps/`` and ``*-deps/`` patterns.
- Extracted shared ``safe_http_request`` helper to ``robotsix_chat.common.http``,
  consolidating the duplicated 3-way ``except (HTTPStatusError, TimeoutException,
  Exception)`` cascade that appeared verbatim in ``board_reader``, ``refdocs``,
  and ``version_check`` HTTP clients.  Callers now import ``safe_http_request``
  and inspect the returned ``HttpResult`` instead of writing their own
  error-formatting boilerplate (~40 lines eliminated).
- Strengthened the ``agent_instruction`` Efficiency bullet to name prohibited
  output shapes (multi-row markdown tables, timeline/audit dumps, recap lists)
  and forbid repeating content already shown in the same conversation.
- Reduced ``mill.timeout`` default from 240 s to 120 s; still generous for an
  LLM board-manager reply but avoids doubled wasted-wait on stuck audits.
- Added request trimming to the mill retry queue: ``BoardWriteRetryQueue``
  now drops the middle of over-long requests before persistence and resend
  (head+tail preservation with an omission marker), cutting ~4–5k-token
  broker retry calls down to ~1k tokens. Configurable via a new
  ``max_request_chars`` constructor parameter (default 4000).
- Documented ``mail`` configuration in ``config/chat.local.example.yaml`` and
  ``MAIL_*`` environment variables in ``.env.example``.
- Refactored ``spawn_check_loop`` in ``robotsix_chat.chat.loops``: extracted
  the 147-line nested ``_worker`` coroutine into a top-level
  ``_check_loop_worker`` and the board-read gate setup into
  ``_setup_board_read_gate``, reducing nesting depth from 6 to 3.
- Added ``docs/architecture.md`` — system architecture overview covering the
  start-up flow, request lifecycle, subpackage inventory, and configuration
  cascade.

- Registered ``robotsix_chat.version_check`` package in ``docs/modules.yaml``
  module manifest.

- Registered ``robotsix_chat.component_agent`` package in ``docs/modules.yaml``
  module manifest.
- Registered ``robotsix_chat.knowledge`` module in ``docs/modules.yaml``.

- Registered ``robotsix_chat.component_client`` module in ``docs/modules.yaml``.
- Added ``fail_under = 88`` coverage threshold to ``pyproject.toml``
  (``[tool.coverage.report]``) to ratchet-floor coverage and block regressions
  in CI.

- Documented the ``broker_src/`` submodule convention in ``AGENT.md``: broker
  features must be developed in the upstream ``robotsix-agent-comm`` repo, not
  directly inside ``broker_src/``.
- Added ``create_board_ticket`` tool to the board reader: a direct synchronous
  (inline) tool that creates tickets via ``POST /tickets`` on the board API,
  avoiding the token waste of spawning a background sub-agent via
  ``delegate_task`` for simple ticket filing.

- Added multi-session support to the chat UI: a sessions sidebar with "New
  chat" button and session list (title + last-active timestamp), click-to-switch
  with independent conversation state per session (DOM cleared, history and
  events stream re-keyed on session_id).  All /chat, /history, /events, /loops
  calls now send session_id + owner_id.  Page load auto-selects the server's
  active session (falling back to newest or locally stored).

- Added sub-agent efficiency rules to the agent system prompt: check tool
  availability before describing a plan and state missing tools in one
  sentence; answer in three sentences or fewer unless elaboration is
  requested; load tools once per session with a single capability check
  before branching.

- Added multi-session support to the conversation store: conversations are now
  addressable by ``session_id`` and grouped under ``owner_id``, with per-owner
  session metadata (title, last-active timestamp, turn count) and an active
  session pointer.  Sessions are persistent — history is never wiped on idle
  timeout.  New ``GET /sessions`` and ``POST /sessions`` HTTP endpoints enable
  listing and creating sessions.  Existing endpoints (``POST /chat``,
  ``GET /history``, ``GET /events``, ``GET /loops``) accept ``session_id`` with
  backward-compatible ``client_id`` fallback.  Persistence uses the same
  ``.data/conversations.json`` mechanism (legacy format auto-migrated on load).
  Added ``persist_path`` to ``ConversationSettings`` (configurable via
  ``CONVERSATION_PERSIST_PATH``).

- Added `board_reader` module with `list_board_tickets` and `read_board_ticket`
  tools that query the SAME HTTP board API endpoint the user's browser UI
  consumes, giving the assistant read parity with the user.  Uses bearer-token
  auth (configurable via `BOARD_READER_API_TOKEN`) and is disabled by default;
  independent of the broker-based mill integration.

- Added `include_previous_result` and `suppress_when` parameters to
  `spawn_check_loop`, enabling change-detection periodic checks where
  the sub-agent can compare against the prior iteration's result and
  suppress no-change tick notifications (no SSE frame, no conversation
  turn).  The `start_check_loop` tool now accepts `include_previous_result`
  and automatically suppresses ticks whose result is the `NO_CHANGE`
  sentinel — so users are only notified when something actually changed.

- Added `docs/periodic-checks.md` documenting how the assistant sets up,
  lists, and cancels periodic board checks, including the recommended
  prompt pattern for change-detection with automatic no-change suppression.

- Increased default width of the background tasks slide-in panel from
  340px to 420px to improve readability of task names and status text.
  Added a drag-to-resize handle on the left edge of the panel so users
  can adjust the width between 260px and 90vw to suit their needs.

- Added persistent, human-readable task tracking under `tasks/`:
  `tasks/TASKS.md` (active), `tasks/ARCHIVE.md` (completed), and
  `tasks/README.md` (format & workflow reference).  Referenced from
  `AGENT.md` and `README.md` for cross-conversation discoverability.

- Fixed pre-existing mypy errors in `broker_client.py` (lazy import type-ignore),
  `test_broker_client.py` (mock function signature), and `test_auth.py` (missing
  `client_id` parameter in `_MockAgent.stream`).

- Added `stop_check_loop` and `list_check_loops` tools so the assistant agent
  can stop and inspect its own running check loops; both tools are scoped to
  the calling client for cross-session isolation.

- Added a Stop button to the Check Loops panel in the chat UI for cancelling
  running check loops via the existing `/loops/{loop_id}/stop` endpoint.
- Redesigned the check-loop panel to declutter and compact displayed rows:
  stopped/failed loops are now hidden (only running loops remain visible); each
  row shows an optional short `reason` (or truncated prompt), a fire-count +
  interval meta line, and a timestamped, truncated latest-feedback summary
  (never the full prompt or full result text). Added `reason` and
  `last_result_at` fields to `LoopInfo`, threaded through the SSE event frames,
  `GET /loops`, and the `start_check_loop` tool; persisted with backward-compat
  defaults so existing `.data/check_loops.json` files load cleanly.
- Added image attachment UI to the chat: file-picker button, clipboard paste,
  and drag-and-drop support for attaching PNG/JPEG/GIF/WebP images with a
  preview tray, per-thumbnail remove controls, and inline validation errors
  for unsupported types, oversized files, and max-count limits. Sent user
  bubbles now render attached image thumbnails.
- Added support for image attachments on `POST /chat`. Clients can now send an
  optional `images` array of `{"media_type": "<image/png|image/jpeg|image/gif|image/webp>", "data": "<base64>"}`
  objects alongside or instead of text. Images are forwarded as multimodal
  content to a vision-capable LLM (requires OpenRouter model level 1 or 2;
  the default level-3 claude_sdk path drops image content — see
  `docs/configuration.md`). New settings `max_images_per_message` (default 8),
  `max_image_bytes` (default 5 MiB), and `allowed_image_media_types` control
  limits.

- Enabled Ruff's `FURB` (Refurb) ruleset to catch future idiomatic-Python
  anti-patterns.

- Replaced hardcoded frame-type strings in `runner.py`'s frame builders
  (`task_started_frame`, `task_completed_frame`, `task_failed_frame`) with
  the shared `SSE_TASK_*_TYPE` constants from `events.py`, so frame types
  stay consistent across the codebase.

- Conversation history is now persisted to `.data/conversations.json` (JSON,
  one write per completed exchange) so chat history survives a Docker container
  restart when the `.data` directory is on a persistent volume mount. The
  in-memory store loads saved conversations on startup.

- The per-conversation history cap was raised from 20 to 50 turns (most recent
  messages), matching the acceptance criterion for conversation retention
  across UI reloads and container restarts.

- The idle-timeout UI behaviour was changed: instead of clearing the entire
  chat area (`chatEl.innerHTML = ""`), an inline italic notice is now appended
  while all previous message bubbles remain visible — so the user can still
  scroll back through the conversation after returning from idle.

- Registered `robotsix_chat.calendar` in `docs/modules.yaml` (was a
  fully-fledged module but absent from the module manifest).

- Registered `robotsix_chat.selfreview` in `docs/modules.yaml` — a read-only
  digest of live conversation activity via `build_recent_activity_tools()`
  that exposes a `read_recent_activity` tool backed by the in-process
  `ConversationStore`.

- Added `pytest-xdist[psutil]` to the `dev` dependency group so the CI
  reusable workflow's `-n auto` flag works without `unrecognized arguments`
  errors.

- Fixed `spawn_check_loop` and `resume_check_loops` to use `settings.min_check_loop_interval_seconds` instead of the hardcoded module constant, so the configured value actually takes effect. Removed the now-unused `MIN_CHECK_LOOP_INTERVAL_SECONDS` module constant.

### Added

- Documented the ``broker_src/`` submodule layout convention in ``AGENT.md``:
  broker features must be developed upstream in
  ``damien-robotsix/robotsix-agent-comm`` and pinned here as a commit update,
  not developed directly inside the submodule.

- `max_check_loops` and `min_check_loop_interval_seconds` configuration
  fields for check-loop registry limits, with env var overrides
  `MAX_CHECK_LOOPS` and `MIN_CHECK_LOOP_INTERVAL_SECONDS`.

- Comprehensive `docs/configuration.md` documenting all ~30 environment
  variables across server, auth, memory, mill, calendar, conversation,
  and refdocs settings.

### Fixed

- `query_tasks` and `query_calendar` tools now send domain-specific instruction
  strings (`"list tasks: …"` and `"list calendar events: …"`) so the upstream
  `robotsix-calendar` intent classifier correctly routes them to `list_tasks`
  and `list_events` respectively. Fixes `query_tasks` returning VEVENT calendar
  entries and `query_calendar` returning "No events found" for real events.

### Removed

- Stale `docs/user-guide/configuration.md` superseded by
  `docs/configuration.md`.

### Changed

- Background-tasks side panel now has a close button (×) and responds to
  the Escape key; the tasks-toggle button acts as a true toggle
  (open/close). Closing the panel preserves in-memory task history.
- Extracted shared `BaseBrokeredClient` base class from `MillClient` and
  `CalendarClient`, eliminating ~40 lines of duplicated boilerplate.

## [0.1.0] - Unreleased

### Added

- Initial release of robotsix-chat: a browser + SSE chat server
  exposing an LLM agent to human users.
- `robotsix-chat` CLI entry point.
- CI workflow with linting, type checking, tests, and security audit.
- Documentation site workflow.
