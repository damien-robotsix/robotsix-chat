Autonomous presets no longer go dormant after a restart. Two stores recorded the
schedule — the per-preset scheduler state and an older `next_fire` map — and on an
install upgraded across the introduction of the former they disagreed: startup read
the missing state as "never run, fire now", then the stale map vetoed that fire and
nothing was rescheduled. A daily preset like `cost-review` could sit idle
indefinitely. Startup now seeds the scheduler state from `next_fire`, keeping each
preset's place in the schedule, and the callers that own firing no longer re-check
the due-ness they just computed.
