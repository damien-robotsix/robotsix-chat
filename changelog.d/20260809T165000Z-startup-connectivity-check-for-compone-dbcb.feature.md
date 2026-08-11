Add startup connectivity check for component roster entries

- New `check_component_connectivity()` in `startup_checks.py` iterates the component roster and probes `GET /health` for each entry at container startup
- Logs a `WARNING`-level message per unreachable component so stale roster entries surface immediately
- Check is non-fatal (startup continues even if all components are unreachable)
- Wired into the `_resume_autonomous` lifespan hook in `cli.py`, running before session resume and memory warmup
- Registered the new module in `docs/modules.yaml`
- Documented the startup check in the architecture start-up flow diagram
