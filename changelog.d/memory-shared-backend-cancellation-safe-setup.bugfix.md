Share one cognee memory backend across all agents and make its setup
cancellation-safe. `build_memory` now memoizes per configuration, so the main
agent, background agents, and runtime-spawned subsessions all use the single
backend the startup warm-up already primed — no agent pays cognee's 47-105 s
cold start inside a live recall anymore. Setup itself runs as a shielded task
on a worker thread: a recall cancelled at its deadline abandons the wait, not
the configuration (previously the half-done setup was thrown away, so an
unwarmed instance re-paid and re-lost the cold start on every recall, staying
memory-less forever), and the heavy cognee import no longer blocks the event
loop during warm-up.
