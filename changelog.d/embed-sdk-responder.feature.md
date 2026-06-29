Implemented the embedded component-agent SDK responder in robotsix-chat — the reference
implementation for per-component adoption (epic child #6).

- Added `ComponentAgentSettings` (disabled-by-default) with broker connection fields, cross-field
  invariants, and env-var overrides.

- Created `ComponentAgentResponder` that lazily imports the SDK `BrokeredResponder` behind an
  `importlib.util.find_spec` guard so the package stays importable without the `broker` extra.

- Registered three request kinds:

  - `monitor` — genuine live telemetry: check-loop registry snapshot + running count,
    conversation/EventBus stats, and secret-redacted settings snapshot.
  - `config-get` — redacted config snapshot + settable-key metadata.
  - `config-set` — validated config update applied to the live `Settings` instance, returning an
    audit record; invalid updates are rejected with a framed `code`/`message`/`details` error and
    never mutate the live config.

- Added read-only `ConversationStore.stats()`, `EventBus.subscriber_count()`, and
  `CheckLoopRegistry.snapshot()` accessors to feed genuine state into the monitor handler.

- Wired responder start/stop into the Starlette lifespan, gated behind the disabled-by-default
  `component_agent.enabled` flag.
