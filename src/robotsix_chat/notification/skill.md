# notify_user skill

The `notify_user` tool publishes a notification event that reaches the user's connected browser (or
mobile app in future) over the existing SSE channel. It is the agent's only channel for proactive,
out-of-band communication — the user receives the notification as a browser-native alert when they
are connected to the session.

**Delivery & persistence:** notifications are published live over SSE to clients that are currently
connected. When no browser is listening for the session at publish time, the live SSE event is
dropped but the notification is still persisted to the notification store (chat-data
`/data/notifications.json`) so it survives a disconnected browser and can be replayed to the next
connecting client. Records whose live publish reached a connected browser are marked `delivered` and
are never replayed; undelivered records are marked `delivered=false` and `read=false` until
replayed.

## Fallback agent_message delivery for background task failures

In addition to the `notify_user` tool, the system has built-in fallback notification for subsession
outcomes. When a periodic monitor or background task auto-stops or fails, the main agent runs a
reaction turn to inform you. If that LLM call itself fails (e.g. API unreachable), the system
publishes a fallback `agent_message` frame directly — the connected browser renders it as a normal
chat bubble, so you still see the outcome without the LLM needing to be available.

## Built-in SSE notifications for monitor state changes

The system also publishes **proactive SSE notifications** (type `notification`) for periodic monitor
state transitions, regardless of the reaction turn:

- **Monitor auto-paused.** When a periodic monitor enters `PAUSED` after `max_idle_runs` consecutive
  no-change replies, a notification with title `"Monitor auto-paused: {title}"` is published to
  connected browsers.
- **Monitor auto-stopped.** When a periodic monitor closes after `auto_stop_no_change_runs`
  consecutive no-change replies, a notification with title `"Monitor auto-stopped: {title}"` is
  published to connected browsers.

These notifications carry the tracked ticket id, the current state, and a summary, so the UI can
show the status immediately without waiting for the agent's reaction turn to complete.

## Allowed operations

| Tool          | Description                                       |
| ------------- | ------------------------------------------------- |
| `notify_user` | Push a one-line alert with optional link/urgency. |

## Trigger points

Only call `notify_user` for these three trigger classes, or when the user explicitly requests it:

1. **Subsession chat opens** — a `user_chat` subsession was spawned and is waiting for the user's
   input (e.g. a decision escalation).
1. **Subsession completes or raises something** — a task or periodic subsession finished, was
   blocked, or surfaced a condition the user must be informed of (e.g. "ticket approved and merged",
   "monitor found a failure", "decision needed").
1. **State/result requiring user awareness** — anything blocking coherence or needing explicit user
   action (blocked subsession, capability gap filed as ticket, missing context).

## Safety

- **No spam.** Do NOT call `notify_user` for routine completions or as a status log. Use the
  `urgency` field to distinguish routine from attention-required alerts:
  - `"low"` — a routine completion the user may want to know about but is not urgent.
  - `"default"` — standard notification.
  - `"high"` — genuinely urgent attention required (blocker, decision needed).
- **Concise.** Messages must be a one-line summary + optional link/reference (ticket id, PR URL,
  subsession id). No full-history dumps, no multi-paragraph reports.
- **No repetition.** If a notification was already sent for a given event, do not resend it.
- **Safe in subsessions.** The tool is available in subsessions and operates identically.
