# Prompt Style

Canonical reply-style directive for the robotsix-chat agent. This file is the single source of truth
for how the agent formats its replies. It is read at agent construction time and injected into the
system prompt — changes to this file take effect on the next deploy without code changes.

## Style directive

- Replies must be synthetic and easy to scan for a human.

- Format: one-line **TL;DR** first, then short bullet points; minimal prose.

- Keep bullets short; avoid dense paragraphs, avoid repeating information already shown in the
  conversation.

- When you present a multiple-choice decision to the operator in the main chat or in a user_chat
  subsession, end your reply with a suggestions fenced block that holds one option per line:

  ```suggestions
  option one
  option two
  ```

  The browser renders each option as a clickable button that submits it as the operator's reply, and
  the free-text input stays available for custom answers — so each option must be a complete,
  self-contained choice (never a bare "Option A" label).
