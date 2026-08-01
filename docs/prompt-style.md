# Prompt Style

Canonical reply-style directive for the robotsix-chat agent. This file is the single source of truth
for how the agent formats its replies. It is read at agent construction time and injected into the
system prompt — changes to this file take effect on the next deploy without code changes.

## Style directive

- Replies must be synthetic and easy to scan for a human.
- Format: one-line **TL;DR** first, then short bullet points; minimal prose.
- Keep bullets short; avoid dense paragraphs, avoid repeating information already shown in the
  conversation.
