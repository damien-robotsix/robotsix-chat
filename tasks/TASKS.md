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
