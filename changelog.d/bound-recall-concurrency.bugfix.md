Bound how many memory recalls run inside cognee at once (new
`memory.recall_max_concurrency`, default 4). Recalls were deliberately
unbounded, but cognee serialises internally on its SQLite metadata store, so a
burst of sessions resuming at boot put every caller on the same contended
resource and they all hit the timeout together — 31 recall timeouts in one
observed day, arriving in three herds, while every recall that ran uncontended
returned in 0.5-1.3 s. Queued recalls now wait inside the caller's existing
timeout, so a backlog still degrades to "no memory" on schedule.
