Recalled memory is now prepended to the current user turn instead of appended to the system prompt.
Per-message recall text in the system prompt sat at the head of the provider's cacheable prefix,
invalidating the prompt cache on every turn; the system prompt is now byte-stable across a
conversation so the instruction, tools, and replayed transcript can be served from cache.
