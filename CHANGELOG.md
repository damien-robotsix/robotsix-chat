# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

<!-- towncrier release notes start -->

## [Unreleased]

- Replaced hardcoded frame-type strings in `runner.py`'s frame builders
  (`task_started_frame`, `task_completed_frame`, `task_failed_frame`) with
  the shared `SSE_TASK_*_TYPE` constants from `events.py`, so frame types
  stay consistent across the codebase.

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
