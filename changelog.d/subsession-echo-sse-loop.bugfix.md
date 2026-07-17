Fixed user replies to subsessions (notably ones spawned by periodic workflows) not appearing until a
window reload: the subsession view now re-syncs its transcript from the server on send instead of
depending solely on the SSE echo frame, the `/events` stream no longer enters a permanent 5s
abort/reconnect loop after a session switch (stale-stream callbacks are now generation-guarded), and
a 20s read-liveness watchdog recovers zombie `/events` connections after network changes or laptop
sleep.
