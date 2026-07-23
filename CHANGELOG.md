## 0.0.0 (unreleased)

- System prompt v46: add "Repo creation bootstrap" guidance — proactively seed an initial commit during repo creation to prevent tool-chain deadlocks with empty repos.
- System prompt v46: added conciseness rule for periodic subsession terminal-state
  notifications — report outcome in one sentence instead of echoing full run history.
- System prompt v46: added two deduplication rules to prevent redundant subsession creation — periodic subsessions must not spawn task children to perform their own monitoring work, and `list_subsessions` must be checked for existing periodic monitors before spawning a task subsession for the same ticket.
- Added `search_knowledge_notes` tool to the knowledge base — the agent can now query
  prior diagnostic notes, deployment statuses, and other key facts by content substring
  match, without needing to recall exact note IDs. Results are ranked by relevance
  (exact topic match > topic contains > content contains).
- Remove the "New autonomous" button from the UI (button element in index.html,
  handler + function + config-display toggle in chat.js). The single-session
  model makes manual creation unnecessary and dangerous.
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
- `repo_study`: fix private-repo fetch by preserving the GitHub App installation token across the API→codeload redirect (httpx was stripping it). Token-exchange failures and 403 scope errors now raise loud, specific errors instead of silently falling back to unauthenticated access.
- System prompt v43: add "Deploy preflight" gate requiring the assistant to retrieve deploy/docker-compose.yml, check the chat_agent_deployable_components allowlist, and verify endpoint capabilities before any deploy call — prevents guessing at deploy endpoint support for multi-service components.
- Migrate in-container GitHub App minting to the shared ``robotsix-github-auth`` library.  Removes 120 lines of JWT creation, caching, and token-exchange logic from ``src/robotsix_chat/repo/direct/client.py``.  ``DirectRepoClient``, ``WorkspaceManager``, ``RefDocsClient``, and ``VersionCheckClient`` now all mint installation tokens through ``robotsix_github_auth.mint_installation_token``.
- Remove ``github_token`` PAT fields from ``RefDocsSettings`` and ``VersionCheckSettings``.  Both doc-fetch and version-check read access now authenticate via the GitHub App installation token (falling back to unauthenticated when the App is not configured), matching the pattern already used by ``RepoStudySettings``.
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
- Removed dead `ConfigError` exception class — `robotsix_config.load_config()` already wraps all errors in its own `InvalidConfigError(ConfigError)`, making the local class redundant.
- Periodic ticket monitor: when reporting terminal state (done/closed), the agent is now instructed to check ticket events/history for PR merge status rather than relying solely on the `pr_url` field, avoiding misleading "no PR URL" reports for auto-merged PRs.
- One-shot (`task`) subsessions are now re-enqueued automatically after a server restart instead of being lost. The task's checkpoint (if any) is preserved so the agent can pick up where it left off.
- Mark 30 expert-only config settings as `advanced: true` in the committed schema so the central-deploy Configure UI hides them behind the "Show advanced settings" toggle. Common settings (`llmio_model_level`, `llmio_api_key`, `idle_timeout_minutes`, `log_level`, `log_json_format`, `langfuse`, `knowledge`) remain always visible.
- Add `robotsix.deploy.chat-agent-mutatable: "true"` label to the production
  deploy compose file so the chat agent can mutate its own service config
  (restart, config-write, config-rollback) via central-deploy endpoints.
- Add prompt-level instructions for the assistant to automatically track unresolved operator prerequisites. When a ticket completes but a human-only action (e.g. provisioning a credential or token) is still required, the agent now files a follow-up tracking ticket and surfaces the prerequisite in session summaries and autonomous closure steps.
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
