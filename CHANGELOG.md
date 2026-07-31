## 0.0.0 (unreleased)

- Lower ``subsessions.max_idle_runs`` default from 5 to 3 so periodic monitors auto-pause sooner (before auto-stop), and improve the auto-pause summary text to include what to do next.
- Extract shared `_retry_with_kuzu_heal` helper in `CogneeMemory`, deduplicating the 13-line retry-with-self-heal pattern from `_recall_core` and `_remember_core`.
- Add `merge_pr` tool to `DirectRepoClient` and the direct-repo agent tools,
  allowing the agent to merge PRs via the GitHub API (PUT merge endpoint).
  The tool is gated on the same BLOCKED-ticket precondition as other
  direct-repo tools and supports merge, squash, and rebase methods.
- Extract shared `_format_entries` helper from duplicated entry-formatting
  loops in `list_knowledge_notes` and `search_knowledge_notes`.
- Wire `load_render_url_skill()` into the agent instruction pipeline so the
  render_url skill markdown is injected alongside other component skills.
  Previously `skill.md` existed but was never loaded — the LLM had no
  guidance on when or how to use the render_url tool.
- Improved subsession outcome formatting: added FILTERING RULE to reaction prompt templates instructing the agent to strip internal technical details (block IDs, event numbers, state machine transitions, spawn counters) from subsession outcomes before presenting to the user. Added user-facing summary formatting guidance to periodic monitor prompts and the `complete_subsession` tool docstring.) (mill: Extract shared _format_entries helper from duplicated formatting loop in list_knowledge_notes and search_knowledge_notes (20260729T165917Z-extract-shared-format-entries-helper-fro-3daa))
- Document `### SFTP` settings section in `docs/configuration.md` covering all 9 fields: `enabled`, `host`, `port`, `username`, `password`, `private_key`, `private_key_passphrase`, `known_hosts`, `remote_root`.
- Dockerfile: fix hadolint warnings — use numeric UID for USER (DL3066) and
  consolidate HEALTHCHECK CMD onto a single line (DL3025)
- System prompt v73: add ticket ID fidelity rule — always use exact board-issued ticket IDs in API calls, never abbreviate or reconstruct from narrative memory.  Added 404 warning logs in ``ticket_poll`` and ``worker_mill`` to flag narrative-derived ticket IDs that fail to match on the board.
- **Monitor auto-resume on PR merge**: The background watcher now polls GitHub for PR merge status in addition to polling the mill for ticket state changes. When a paused periodic monitor's checkpoint records a tracked PR (`pr_number` + `repo_full_name`), the watcher checks whether that PR has been merged and auto-resumes the monitor. This catches merges that the board ticket API may not immediately reflect. Also fixed a gap where monitors closed with `pre_authorized_approval` were never eligible for auto-resume.
- `direct_fix` and `patch_direct_repo_file` now use the component-request roster path for implement-cycle counting when `component_request` is available, matching the fallback already used for ticket-state verification.  Fixes failures where the direct board API was unreachable but the roster-based path worked.
- Fix hadolint violations in Dockerfile: use numeric UID (`USER 1000` instead of `USER app` for DL3066) and JSON form for HEALTHCHECK CMD (DL3025).
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
