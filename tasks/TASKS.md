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
