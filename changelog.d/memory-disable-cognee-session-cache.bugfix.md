Disable cognee 1.4's session memory (`CACHING=false`): chat never reads the
session SQL cache back, and its per-turn session-context writes stalled every
recall ~30 s in a sqlite "database is locked" busy-wait once the cache WAL
grew un-checkpointable (626 MB observed) on the shared-HDD deploy host (#1201).
