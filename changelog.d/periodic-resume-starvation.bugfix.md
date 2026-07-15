Periodic subsessions resume with their run counter restored: previously a restart reset the counter
to 0 while the run guard remembered every executed run, so the worker slept one full interval per
historical run number ("skipping duplicate") and — with regular restarts — never executed again.
Duplicate-run collisions now fast-forward instantly, and `max_runs` carries over unchanged instead
of being shrunk by phantom skips on every restart.
