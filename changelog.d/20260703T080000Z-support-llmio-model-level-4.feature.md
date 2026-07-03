Support llmio model level 4 (Claude Fable 5 frontier tier): bump the `robotsix-board-agent` pin
(which carries `robotsix-llmio` past the level-4 addition), and derive the valid `model_level` set
from llmio's `TierLevel` enum instead of hardcoding `[1, 2, 3]` — chat can no longer drift from the
tiers llmio actually ships. `LLMIO_MODEL_LEVEL=4` now deploys.
