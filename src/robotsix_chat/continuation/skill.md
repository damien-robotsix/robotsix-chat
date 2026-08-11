## Continuation (post-restart auto-resume)

You have tools to schedule a **continuation** — a stored prompt that fires
automatically after the next server restart, so work-in-progress resumes
without human intervention.

### Tools

- **`schedule_continuation(session_id, prompt)`** — arm a continuation.
  Call this BEFORE `self_restart` so the current work resumes after the
  restart.  The prompt is injected into the conversation as if the operator
  had sent it.  Only ONE continuation can be pending at a time — calling
  this again overwrites any previously scheduled one.

- **`cancel_continuation()`** — cancel the pending continuation.  Use when
  the work that was going to be continued is no longer needed.

- **`get_continuation_status()`** — check whether a continuation is pending,
  which session it targets, and the current guardrail state.

### When to use

The primary use case is a **self-restart to pick up a newly-deployed
capability**: before calling `self_restart`, schedule a continuation so the
agent picks up right where it left off after the restart.

### Guardrails

- **One-shot**: a continuation fires once and is consumed — a restart loop
  cannot re-trigger it.
- **Consecutive limit**: after `max_consecutive` (default 3) consecutive
  auto-continuations, the guardrail blocks further automatic firing.  The
  operator must manually interact to reset the counter.  This prevents a
  restart→continue→restart→continue loop from running indefinitely.
- **Audit trail**: every arm, fire, cancel, and guardrail event is logged to
  the continuation store's audit log.
