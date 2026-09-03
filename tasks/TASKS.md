# Active Tasks

Tasks that are pending, in-progress, or blocked.

<!--
  Format (see tasks/README.md):
  ## T-NNNN — Short title
  - status: pending | in-progress | blocked
  - created: YYYY-MM-DDTHH:MM:SSZ
  - updated: YYYY-MM-DDTHH:MM:SSZ
  - notes: free-form Markdown
-->

## T-0001 — Idle-timeout duplicates issue

- status: pending
- created: 2026-06-23T12:40:00Z
- updated: 2026-06-23T12:40:00Z
- notes: After the 50-message history cap was added (20260623T163048Z), investigate whether
  duplicate messages still appear when the browser reconnects after an idle timeout.

## T-0002 — Terminal-filter ticket

- status: pending
- created: 2026-06-23T12:40:00Z
- updated: 2026-06-23T12:40:00Z
- notes: Track the terminal-filter feature request referenced in prior conversations. Add details as
  they become available.

## T-0005 — Evergoing session: cross-session awareness agent tools

- status: pending
- created: 2026-08-29T07:47:00Z
- updated: 2026-08-29T07:47:00Z
- notes: Ticket 20260829T074717Z-wire-up-the-evergoing-session-v2-activat wired the core evergoing
  feature (activation on boot behind `evergoing.enabled`, the periodic subject-aware trim scheduler
  with the new-input gate, cheap-tier trim decision, UI marker). The remaining acceptance-criterion
  sub-item — surfacing agent-facing tools to the evergoing session so the agent can *enumerate other
  sessions* and *spawn a new independent session / close an existing one* — is a separate subsystem
  (in-process function tools wired into `create_agent_from_settings` + `skill.md` docs, mirroring
  `repo/direct`). The HTTP endpoints already exist (`GET/POST /sessions`, `DELETE /sessions/{id}`,
  `POST /sessions/{id}/close`); this task is only the agent-tool wrapper layer. Split out per the
  repo scope-split convention to keep the wiring ticket shippable.

## T-0003 — Deliver queued user messages between tool calls (blocked on robotsix_llmio)

- status: blocked
- created: 2026-08-29T00:00:00Z
- updated: 2026-08-29T00:00:00Z
- notes: Ticket 20260828T123000Z-deliver-queued-user-messages-between-too asks to inject queued user
  messages at inter-tool step boundaries (after a tool_result, before the next model call). That
  boundary is owned entirely by the Claude Agent SDK subprocess inside `robotsix_llmio`, not by
  robotsix-chat. A chat/subsession turn is a single static `handle.run(prompt, message_history=...)`
  call (`src/robotsix_chat/llm/agent.py`), and the model→tool_use→tool_result→model loop runs inside
  `robotsix_llmio.claude_sdk._stream._stream_query`. The only hook robotsix_llmio exposes into that
  loop is `activity_events(on_event)` — read-only observability that cannot inject, steer, or
  interrupt. **Prerequisite (upstream):** robotsix_llmio must add a live async-iterable
  streaming-input prompt OR a per-tool-result injection callback before this ticket is implementable
  in robotsix-chat. Once that lands, re-open and wire the existing queue (main chat:
  `MessageCoalescer._batches` in `chat/server/routes/chat.py`; subsessions: `SubsessionRegistry`
  inbox deque in `subsessions/registry.py`) to drain at each step boundary instead of only at turn
  boundaries. Blocker also recorded as a comment on the ticket.

## T-0004 — Browser / form-filling capability (Playwright MCP + Vaultwarden) — split required

- status: blocked
- created: 2026-08-29T00:00:00Z
- updated: 2026-08-29T00:00:00Z
- notes: Ticket 20260828T232023Z-add-interactive-browser-form-filling-cap-f67d asks for an
  interactive browser / form-filling capability (Playwright MCP), Vaultwarden credential injection,
  a submit-gate + 2FA-pause, a chat skill doc, and a live OVH case CS16584956 submission — in one
  pass. This spans ≥3 independent subsystems and depends on hard prerequisites outside this repo's
  implement stage, so it cannot ship as a single robotsix-chat code change (see AGENT.md "Ticket
  scoping"). Findings verified against the current clone: (1) **No external-MCP-server support in
  this repo.** The chat agent's tools are in-process Python function tools passed to
  `provider.build_agent(..., tools=...)` (`src/robotsix_chat/llm/agent.py:685`); there is no
  `mcp_servers` parameter. External services are reached only via the central-deploy roster
  (`src/robotsix_chat/component_access/roster.py`) + the generic HTTP `component_request` tool
  against robotsix HTTP components — there are no MCP connectors today. **Prerequisite (upstream):**
  either robotsix_llmio's `build_agent` must accept and forward `mcp_servers` to
  `ClaudeAgentOptions`, OR a new deployable HTTP component that wraps Playwright must be built
  (separate repo/ticket). (2) **Fleet deployment + roster registration live in
  robotsix-central-deploy**, not this repo. (3) **No Vaultwarden / Bitwarden CLI / EnvStore
  integration exists**. New external secret client + operator-provisioned scoped single- collection
  service account + `BW_SESSION`/API key in EnvStore — its own ticket. (4) **The live OVH
  submission** needs a running browser, real credentials, and PII documents — out of scope for the
  implement stage. Recommended split (dependency-ordered): (a) upstream robotsix_llmio MCP-server
  plumbing OR a Playwright-wrapping HTTP component + central-deploy roster registration; (b)
  Vaultwarden secret client with zero-leakage injection + tests, EnvStore wiring; (c) submit-gate /
  2FA-pause agent wiring + chat skill.md doc; (d) the live OVH CS16584956 submission
  (operator-driven). Blocker also recorded as comment id=1647 on the ticket.

## T-0006 — Register the browser service in the callable component roster (form-fill API contract)

- status: pending

- created: 2026-08-31T13:18:34Z

- updated: 2026-08-31T13:18:34Z

- notes: Ticket 20260831T081117Z-wire-the-browser-service-s-chat-skill-en (session_end prompt,
  session a41f952923194ea0a89be06d0c061811). At session end robotsix-browser was fully deployed and
  registered as a live managed component in central-deploy, but the chat assistant could not drive
  an OVH billing-address form-fill because robotsix-browser is not in its callable component roster
  with a documented API skill — a concrete capability gap that blocks any future use of deployed
  automation components.

  **Architecture context (verified in this clone).** The chat agent has NO per-component tools; it
  reaches every deployed component through ONE generic `component_request` tool
  (`src/robotsix_chat/component_access/tools.py`) against the roster fetched at session start from
  `GET {central_deploy.url}/chat/components` (`component_access/roster.py::fetch_roster`). Each
  roster entry carries `{id, base_url, skill, auth?}`; the `skill` doc is loaded into the agent
  system prompt so the LLM knows the component's API contract. Therefore "registering"
  robotsix-browser is primarily a central-deploy + browser-repo change, NOT a robotsix-chat code
  change — once the service appears in `/chat/components` with a non-empty `skill`,
  `component_request` can already invoke it. This chat repo's only levers are: (a)
  `central_deploy.component_fallbacks` (`config/deploy_models.py::CentralDeploySettings`) — a
  baked-in `{id: base_url}` fallback, but its roster entries have an EMPTY skill (no documented
  API), so it does not by itself satisfy "documented API skill"; and (b)
  `central_deploy.component_credentials` if the browser service requires auth.

  **Work items (dependency-ordered, mostly cross-repo).**

  1. robotsix-browser repo: expose a `GET /chat-skill` (or `/skill`) endpoint returning a SKILL.md
     that documents the form-fill job contract (see below). Mirror this repo's own
     `chat/server/routes/chat_skill.py`.
  1. robotsix-central-deploy: include robotsix-browser in the `GET /chat/components` roster with its
     `base_url`, the fetched `skill`, and any `auth` metadata.
  1. robotsix-chat (this repo, only if the above cannot cover a gap): add robotsix-browser to
     `central_deploy.component_fallbacks` in the deployed config for roster resilience, and a
     `component_credentials` entry if auth is required. No new tool is needed — `component_request`
     already covers it.
  1. Live-proof: from a chat session, call `component_request` with
     `component_id="robotsix-browser"` to submit the OVH billing-address form-fill and confirm the
     result frame.

  **Proposed form-fill job API contract** (to be finalised in the browser service's chat-skill doc):
  `POST /form-fill` with body
  `{url, fields: [{selector, value}], submit: {selector} | null, wait_for?: selector, screenshot?: bool}`
  returning `{status, final_url, submitted, fields_filled, errors?, screenshot?}`. 2FA /
  confirmation-gated submissions should pause and report a resumable job id rather than blocking.

  **Preconditions / edge cases:** unknown selector → return per-field error, do not submit; target
  URL unreachable → status=failed with reason; submit gate pending (2FA) → status=paused + job id.
  **User-facing feedback:** the agent surfaces the returned status/result via the normal SSE reply.
  **Definition of done:** a chat session successfully drives a form-fill through `component_request`
  → robotsix-browser and reports the outcome. Related to (and narrower than) T-0004.

## T-0007 — Activate the `dependabot-drain` periodic preset on the live deployment

- status: pending
- created: 2026-09-03T00:00:00Z
- updated: 2026-09-03T00:00:00Z
- notes: Ticket 20260903T113001Z / follow-up 20260903T114337Z shipped the `dependabot-drain`
  periodic-session preset into `config/config.json` under `periodic.sessions`, documented in
  `docs/user-guide/periodic-sessions.md`. It ships `"enabled": false` per the feature-flag
  convention, so it is inert until an operator turns it on.

  **Activation config change (operator, live deployment):** set `"enabled": true` on the
  `dependabot-drain` entry under `periodic.sessions` in the deployment's merged config (the
  central-deploy config-target `/home/app/config/config.json`), then redeploy. All other fields are
  already correct (weekly, `anchor_utc: "2026-09-07T06:00:00Z"` = Monday 06:00 UTC, `model_level: 3`).

  **Live-proof step:** after activation, fire it once out-of-band with
  `POST /periodic/definitions/dependabot-drain/run` and read the returned session's report (PRs
  merged, migration tickets filed, PRs skipped); confirm the preset shows `enabled: true` in
  `GET /periodic/definitions`.

  **Post-deploy follow-up:** after the first anchored Monday 06:00 UTC firing, re-check
  `GET /periodic/definitions` and confirm `last_fired_at`/`runs` advanced on schedule — i.e. the
  preset actually turned on and fired in production. Archive this task once that is confirmed.
