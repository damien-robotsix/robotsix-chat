Switched the cognee memory extraction LLM default from `deepseek-v4-flash` to `claude-haiku-4.5` —
the DeepSeek model produced malformed JSON under instructor's structured-output prompting, causing
multi-minute retry stalls after replies.
