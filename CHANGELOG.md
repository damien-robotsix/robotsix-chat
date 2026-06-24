# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- towncrier release notes start -->

## [Unreleased]

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

- Added `pytest-xdist[psutil]` to the `dev` dependency group so the CI
  reusable workflow's `-n auto` flag works without `unrecognized arguments`
  errors.

- Fixed `spawn_check_loop` and `resume_check_loops` to use `settings.min_check_loop_interval_seconds` instead of the hardcoded module constant, so the configured value actually takes effect. Removed the now-unused `MIN_CHECK_LOOP_INTERVAL_SECONDS` module constant.

### Added

- `max_check_loops` and `min_check_loop_interval_seconds` configuration
  fields for check-loop registry limits, with env var overrides
  `MAX_CHECK_LOOPS` and `MIN_CHECK_LOOP_INTERVAL_SECONDS`.

- Comprehensive `docs/configuration.md` documenting all ~30 environment
  variables across server, auth, memory, mill, calendar, conversation,
  and refdocs settings.

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
