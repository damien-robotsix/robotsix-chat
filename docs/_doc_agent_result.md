# Doc agent memory ledger — robotsix-chat

## Layout
- `docs/` — markdown docs. `docs/modules.yaml` is the module manifest maintained by module_curator; it lists every importable namespace with its paths. Non-Python resources (e.g. `ui/*`) and tests are listed under the owning module. When new static/test files are added, they MUST be registered in the `ui` module's paths in `docs/modules.yaml`.
- `docs/user-guide/` — user-facing feature docs: `chat-interface.md`, `deployment.md`, `periodic-sessions.md`, `settings-ui.md`.
- `docs/chat/server/` — server route docs.

## Conventions
- User-facing behavioral changes to the browser chat UI belong in `docs/user-guide/chat-interface.md`.
- Docs are markdownlint-checked and CI-gated (`docs/modules.yaml` and the baseline-check workflow).

## chat-interface.md sections
- Suggestion Chips
- Compacted (Summarised) Sessions
- In-App Notification Toasts (including "Native Desktop Notifications (Additional Channel)")
- Desktop Notifications for Conversation Messages (added for SSE-driven live chat message notifications, focus/visibility de-duplication)
- Missed Notifications Badge & Panel

## UI static files
`src/robotsix_chat/ui/static/`: chat.js (main IIFE), notify.js (Notification API wrapper, never throws), notify-gating.js (pure focus/visibility gating helpers unit-tested in Node), sse-parser.js, memory-banner.js, message-queue.js, reconnect-guard.js, drain-draft.js, suggestions.js. Tests under `tests/js/`.
