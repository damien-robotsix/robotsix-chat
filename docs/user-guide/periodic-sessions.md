# Periodic sessions

A periodic session is an **ordinary chat session** that the server starts on
a schedule. Each configured preset fires on its interval: the scheduler
creates a fresh session under the `periodic` owner, titles it
`<preset> — <date>`, and posts the preset's `initial_prompt` through the
exact same code path as an operator message. The turn runs, the agent
finishes with a report, and that is the whole lifecycle.

There is no execution state machine, no self-scheduled continuation, and no
restart-resume: a server restart mid-turn fails that turn like it would fail
yours, and the next scheduled firing starts a fresh session. You can open
any periodic session in the sidebar (`[PERIODIC]` prefix) and simply talk to
it — it behaves like any other session.

## Configuration

Presets live under `periodic.sessions` in the config:

```json
{
  "periodic": {
    "sessions": [
      {
        "name": "mail-triage",
        "initial_prompt": "Review today's mail triage decisions. READ-ONLY: never move, archive, delete, or send anything. Finish with a concise report.",
        "schedule_interval_seconds": 86400,
        "model_level": null,
        "enabled": true
      }
    ]
  }
}
```

- `initial_prompt` — the one message the session receives. Write it as a
  complete task brief: task, scope, hard constraints, expected report. The
  scheduler prepends a short shared preamble stating the periodic contract
  (finish in this turn, report at the end, no continuations).
- `schedule_interval_seconds` — spacing between firings (min 300, default
  one day). A never-fired preset fires promptly after startup.
- `model_level` — optional llmio level override (1–3); `null` follows the
  global model-level resolution, like an operator session.

If a preset comes due while its previous session is still processing a
turn, that firing is skipped (logged, not queued).

## Endpoints

- `GET /periodic/definitions` — presets with their firing state
  (`last_fired_at`, `last_session_id`, `runs`).
- `POST /periodic/definitions/{name}/run` — fire a preset now (409 while
  its previous session is mid-turn).
