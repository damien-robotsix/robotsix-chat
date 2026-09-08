## Continuation (post-restart auto-resume)

Interrupted sessions **auto-continue** after a server restart — a session that was mid-turn when the
process exited resumes automatically on the next boot, with no explicit arming call. There is no
longer an explicit arming tool; the continuation is created for you automatically.

### Tools

- **`cancel_continuation()`** — cancel the pending continuation. Use when the work that was going to
  be continued is no longer needed.

- **`get_continuation_status()`** — check whether a continuation is pending, which session it
  targets, and the current guardrail state.

### When to use

Continuation is automatic, so you normally do nothing. Reach for these tools only to **inspect** a
pending continuation (`get_continuation_status`) or to **cancel** one whose work is no longer needed
(`cancel_continuation`).

### Guardrails

- **One-shot**: a continuation fires once and is consumed — a restart loop cannot re-trigger it.
- **Consecutive limit**: after `max_consecutive` (default 3) consecutive auto-continuations, the
  guardrail blocks further automatic firing. The operator must manually interact to reset the
  counter. This prevents a restart→continue→restart→continue loop from running indefinitely.
- **Audit trail**: every arm, fire, cancel, and guardrail event is logged to the continuation
  store's audit log.
