Cognee memory now detects a frozen store (the recurring orphaned
LanceDB/sqlite lock — recall timing out, writes failing) and recovers instead
of silently degrading to "no memory" until a human restarts the container. The
freeze is surfaced loudly — an ``ERROR`` log and a ``degraded`` flag on
``GET /health`` (which stays ``status: ok`` so liveness is unaffected) — and,
once it persists past ``memory.frozen_store_recovery_minutes`` (default 15), a
guarded self-restart is triggered (the proven remedy), rate-limited by
``memory.recovery_cooldown_minutes`` (default 30) so it cannot restart-loop.
Auto-recovery is on by default and can be disabled via
``memory.auto_recovery_enabled``.
