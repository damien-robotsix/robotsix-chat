Fixed duplicate notification bubbles (a check-loop's `loop_started` / `loop_tick` message, or a
background-task frame, rendered 2–3× in some chats). The `/events` SSE reconnect path scheduled a
bare `setTimeout(openEventStream, …)` from each `onDone`/`error` callback, and `openEventStream()`
created a fresh `AbortController` without aborting the previous stream — so stacked reconnects left
multiple live `/events` fetches, each holding its own server-side EventBus subscription. Every frame
was then fanned out (and rendered) once per leaked subscription, which is why the count varied per
session ("not in all chats"). `openEventStream()` now aborts any prior stream before opening, and
reconnects route through a single guarded timer so at most one stream/subscription exists per
session.
