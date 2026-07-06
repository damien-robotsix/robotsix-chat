Bump the pinned `robotsix-llmio` commit to pick up detection of usage-credit exhaustion when the
Claude SDK collapses it into a raised exception instead of a clean `is_error=True` return (the
`ClaudeSDKUsageExhaustedError` fallback added in the previous fix only covered the latter shape).
Without this, the raw "Claude Code returned an error result: success" text could still leak to the
main chat session instead of triggering the tier fallback.
