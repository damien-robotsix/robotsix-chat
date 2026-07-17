Guard cognee `recall()`/`remember()` with hard timeouts (60s/300s, configurable) — a wedged cognee
backend (orphaned LanceDB adapter lock) used to hang instead of raise, freezing every subsession
worker at status "running" until a container restart.
