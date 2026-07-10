Bump robotsix-llmio to pick up the claude_sdk binary-content fix: an attached image was stringified
into a multi-megabyte escaped-byte prompt that stalled the CLI subprocess — sessions with images
hung showing nothing. Images on the claude_sdk model levels now flatten to a compact placeholder
(the model still cannot see them; use an OpenRouter vision level for that).
