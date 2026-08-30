# Chat Interface Features

The browser chat UI provides several interactive features that enhance the operator's ability to communicate with the agent and respond to decisions.

## Suggestion Chips — Clickable Answer Options

When the agent presents you with a **discrete multiple-choice decision** (approve/reject, pick one of several options, yes/no), the options appear as clickable buttons below the agent's message.

### How to use

1. **Click a suggestion chip** to submit that option as your reply. The chip text is sent as a normal user message.
2. **Type a custom answer** in the text input at the bottom — the suggestion chips remain available and you can still click them after typing.
3. **Chips are one-time use** — once you submit a reply (by clicking a chip or pressing Enter after typing), the chips from that decision are disabled and cannot be re-submitted. If the agent asks a new question, new chips will appear.

### Example

The agent might ask:

> Should I approve ticket `#73f3` and merge the pull request?
>
> [Approve ticket 73f3 and merge] [Reject and request changes] [Ask for more details]

Click any chip to submit that option immediately, or type a custom response like "Wait, let me check the test results first."

### Availability

Suggestion chips appear in:

- **Main chat** — decisions posed by the agent in the primary conversation
- **Side-chat panels** (`user_chat` subsessions) — when the agent asks for a decision in a focused side conversation

If the agent does not emit suggestion chips for a decision, you can still type your response freely in the text input.
