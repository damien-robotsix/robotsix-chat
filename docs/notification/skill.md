# notify_user skill

The `notify_user` tool publishes a notification event that reaches the user's connected browser (or
mobile app in future) over the existing SSE channel. It is the agent's only channel for proactive,
out-of-band communication — the user receives the notification as a browser-native alert when they
are connected to the session.

**Delivery limitation:** notifications only reach clients that are currently connected. When no
browser is listening for the session, the notification is silently dropped.

## Allowed operations

| Tool          | Description                                       |
| ------------- | ------------------------------------------------- |
| `notify_user` | Push a one-line alert with optional link/urgency. |

## Trigger points

Only call `notify_user` for these three trigger classes, or when the user explicitly requests it:

1. **Subsession chat opens** — a `user_chat` subsession was spawned and is waiting for the user's
   input (e.g. a decision escalation).
2. **Subsession completes or raises something** — a task or periodic subsession finished, was
   blocked, or surfaced a condition the user must be informed of (e.g. "ticket approved and merged",
   "monitor found a failure", "decision needed").
3. **State/result requiring user awareness** — anything blocking coherence or needing explicit user
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
