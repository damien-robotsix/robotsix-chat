When a claudeSDK tier's Claude subscription usage credits are exhausted (e.g. level 4's
`claude-fable-5`), the chat agent no longer surfaces the raw "You're out of usage credits" text as
if it were a genuine reply. It now catches the new `ClaudeSDKUsageExhaustedError` from
robotsix-llmio and retries the same turn at a fallback tier (level 3's `opus`) via
robotsix-llmio's `acall_with_tier_fallback`, scoped to one promotion.
