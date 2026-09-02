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
- **Banner/notice:** if you want to surface the situation, show a small inline notice (e.g.
  "Earlier messages are available below") in place of the summary card.
- Always check `compacted_summary_missing` before deciding whether a summary card can be built; a
  bare compacted session with no explanation would otherwise confuse the reader.
