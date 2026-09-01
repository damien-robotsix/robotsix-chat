# Prompt Style

The canonical reply-style directive now ships **inside the package** at
[`src/robotsix_chat/chat/server/prompt-style.md`](https://github.com/damien-robotsix/robotsix-chat/blob/main/src/robotsix_chat/chat/server/prompt-style.md),
so the deployed image actually contains it (the old `docs/` location was never shipped, and
production silently ran without a style directive).

It is read at agent construction time and injected into every system prompt build — edit the
packaged file; changes take effect on the next deploy without code changes.
