# Chat Interface Features

The browser chat UI provides several interactive features that enhance the operator's ability to
communicate with the agent and respond to decisions.

## Suggestion Chips — Clickable Answer Options

When the agent presents you with a **discrete multiple-choice decision** (approve/reject, pick one
of several options, yes/no), the options appear as clickable buttons below the agent's message.

### How to use

1. **Click a suggestion chip** to submit that option as your reply. The chip text is sent as a
   normal user message.
1. **Type a custom answer** in the text input at the bottom — the suggestion chips remain available
   and you can still click them after typing.
1. **Chips are one-time use** — once you submit a reply (by clicking a chip or pressing Enter after
   typing), the chips from that decision are disabled and cannot be re-submitted. If the agent asks
   a new question, new chips will appear.

### Example

The agent might ask:

> Should I approve ticket `#73f3` and merge the pull request?
>
> `[Approve ticket 73f3 and merge]` `[Reject and request changes]` `[Ask for more details]`

Click any chip to submit that option immediately, or type a custom response like "Wait, let me check
the test results first."

### Availability

Suggestion chips appear in:

- **Main chat** — decisions posed by the agent in the primary conversation
- **Side-chat panels** (`user_chat` subsessions) — when the agent asks for a decision in a focused
  side conversation

If the agent does not emit suggestion chips for a decision, you can still type your response freely
in the text input.

## Compacted (Summarised) Sessions

Long-running sessions are periodically summarised and their leading turns trimmed. When you reload
the page, the UI fetches `/history`; for a compacted session the response includes compaction
metadata (see the [deployment guide](deployment.md#compacted-sessions-and-the-missing-summary-flag)
for examples):

- `compacted_summary` — the summary text of the covered leading turns.
- `compacted_turn_index` — how many leading `turns` the summary covers.
- `compacted_summary_missing` — `true` when the session advanced past compaction but no usable
  summary is available.

### How the UI handles a compacted session

When `compacted_summary` is a non-empty string, the UI opens the session on a **"Summary of the
earlier conversation"** card at the top of the transcript, with a toggle that shows/hides the
covered turns. The covered turns are still loaded and rendered — they are just collapsed behind the
toggle so the transcript starts from the summary.

### Handling when the summary is missing (`compacted_summary_missing: true`)

When the server reports `compacted_summary_missing: true`, there is no summary text to show in a
card. Clients must not render an empty summary card. Recommended handling patterns:

- **Graceful degradation (default):** render the covered turns inline like any other turns — the
  transcript is complete, just without a summary card.
- **Banner/notice:** if you want to surface the situation, show a small inline notice (e.g. "Earlier
  messages are available below") in place of the summary card.
- Always check `compacted_summary_missing` before deciding whether a summary card can be built; a
  bare compacted session with no explanation would otherwise confuse the reader.

## In-App Notification Toasts

When the agent sends you a notification, it appears as a transient **toast** notification in the
upper-right corner of the browser window. This happens **regardless of whether you have granted the
browser's notification permission** — in-app toasts ensure every alert is visibly surfaced even
when desktop notifications are unavailable.

### Where Toasts Appear

Toasts stack vertically in the top-right corner of the screen, each showing:

- **Title** — the notification subject
- **Body** — the notification message content
- **Link** (optional) — a reference URL or ticket ID if included

### Auto-Dismiss & Interactivity

- **Low and default urgency** — toasts auto-dismiss after 8 seconds
- **High urgency** — toasts persist until you click them to dismiss
- **Any urgency** — you can click a toast at any time to dismiss it immediately

### Urgency Levels

Toasts are color-coded by urgency level (visible as a left border):

- **Low** — routine completions (gray border)
- **Default** — standard notifications (blue border)
- **High** — urgent attention required (red border)

### Native Desktop Notifications (Additional Channel)

When you have **granted browser notification permission**, the agent also sends native desktop
notifications alongside the toast. Native notifications are an additional channel — the in-app toast
ensures you never miss an alert even if desktop permission was never granted.

> **Tip:** If you want desktop notifications, watch for the browser's permission prompt on your
> first click. Grant it to enable native alerts as a backup to the in-app toasts.

### System Alerts — Service Faults

In addition to agent-generated notifications, the system sends **high-urgency red toasts** when a
critical backend service fault is detected and automatic recovery cannot safely repair it. These
are rare but important:

- **"Memory store down (graph segfault)"** — The long-term memory service encountered a persistent
  fault that auto-recovery could not heal. The memory service is temporarily offline, so the agent
  will continue without access to your conversation history. **What to do:** check the system logs
  or contact support if the memory service does not recover within a few minutes. The fault
  diagnosis is included in the notification body.

These system alerts persist on screen (red border, high urgency) until you dismiss them.

## Missed Notifications Badge & Panel

When the agent sends you **missed notifications** (alerts and reminders you weren't connected to
receive), they are stored on the server with an unread state. The chat header displays a badge
showing how many notifications you have not yet viewed.

### Badge

The "🔔 Alerts" button in the header shows an unread-notification count badge. The badge is hidden
when the count is zero.

### Opening the Notifications Panel

**Click the "🔔 Alerts" button** to open the missed-notifications panel. The panel slides in from
the right side of the screen and displays:

- **Title** — the notification subject
- **Body** — the notification message content
- **Timestamp** — when the notification was sent
- **Source session** — which session issued the notification

### Marking Notifications as Read

When you open the notifications panel, all notifications displayed are automatically marked as read.
The badge clears immediately and will stay at zero on page refresh — those notifications are no
longer unread.

## Desktop Notifications for Conversation Messages

In addition to the agent-sent notifications above, new **conversation messages** can also raise
native desktop notifications when you have granted browser notification permission — but only when
you are not actively viewing the target.

This covers two live update paths driven by the existing Server-Sent Events (SSE) channel:

- **New main-conversation messages** — a completed or re-attached agent turn in the active chat.
- **New `user_chat` side-chat messages** — when the agent asks the operator something in a focused
  side conversation (a subsession the agent starts to get a decision).

### De-duplication (when you won't be notified)

To avoid an intrusive notification for something that is already on screen, notifications are
suppressed when **the browser tab is visible** **and** the target is the one you are actively
viewing:

- A **main-conversation** message does not notify while you are looking at the main chat (no
  subsession is in focus mode).
- A **`user_chat` side-chat** message does not notify while that specific subsession is either in
  **focus mode** (fills the screen) or has its row **expanded** with the side-chat panel visible —
  the same on-screen signal the unread-badge logic uses.

When the tab is in the background, or you are viewing a different target, a new message raises a
desktop notification titled with the conversation (`New message in …`) or "Chat request" so you can
tell main-chat from side-chat updates at a glance.

> **Tip:** These notifications are gated by the same browser permission as the agent-sent
> notifications above — grant the permission prompt to enable them. De-duplication is automatic; no
> configuration is needed.

### Clicking a Notification to Navigate

When you click a **desktop notification** for a conversation message, the chat app automatically
navigates to the target and brings the window/tab to the foreground — even if the browser was
backgrounded at the time.

- **Clicking a main-conversation notification** — opens or focuses the active main chat and the
  session that message belongs to (if a different session is currently active, it switches to the
  correct one). Any subsession focus mode is exited so you see the main conversation.
- **Clicking a `user_chat` side-chat notification** — opens or focuses the specific subsession that
  sent the message. The subsessions panel slides open and the target subsession appears (either
  expanded in the panel or in full-screen focus mode if that was active).

The notification is then closed so you don't see it again — no duplicate or stale notifications
remain after the click is handled.

### Replayed Missed Notifications

When you reconnect to the chat (on page reload or after a network disconnect), any notifications
you missed while offline are replayed. Each replayed notification:

- Appears as an **in-app toast** in the corner (the same transient alert you see for live
  notifications)
- Shows up in the **unread-notifications badge** so you can review the full history
- Triggers a **native desktop notification** if permission was granted

You can then open the notifications panel to view the full list and mark them as read.

### Closing the Panel

Click the **×** button in the notifications panel header or click outside the panel to close it.
The marked-as-read state is preserved — the panel can be reopened if you want to view the
notification history again in a later session.
