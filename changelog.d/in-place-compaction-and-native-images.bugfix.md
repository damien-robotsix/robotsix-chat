Stabilize idle compaction: compact **in place** instead of minting a continuation session per idle
gap. The session keeps its id and full visible transcript; only the agent-facing replay folds older
turns into the summary. No more "New chat" husk sessions, no more subsession trees hopping between
sessions, no client-side session adoption needed (legacy `compacted_into` chains still reroute).
Compaction is also skipped for conversations with fewer than `compaction_min_turns` (default 3)
fresh turns, so empty or tiny conversations never trigger the summary agent. Bumps robotsix-llmio
for native image support on the claude_sdk path: attached images are now sent as base64 image blocks
via SDK streaming input, so the agent can actually see them.
