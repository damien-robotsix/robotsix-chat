Bumped robotsix-llmio past the `task_budget` fixes. The pinned build clamped a
below-floor `max_tokens` **up** to the API's 20,000 minimum and sent it as an
advisory budget, so agents were told they had 20,000 tokens for an entire task
and wrapped up before doing any work — surfacing as "I exhausted my processing
budget before fetching any data". Below-floor values now send no budget at all,
and a model that rejects the parameter is retried without it.
