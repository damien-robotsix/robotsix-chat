# Periodic sessions

A periodic session is an **ordinary chat session** that the server starts on a schedule. Each
configured preset fires on its interval: the scheduler creates a fresh session under the `periodic`
owner, titles it `<preset> — <date>`, and posts the preset's `initial_prompt` through the exact same
code path as an operator message. The turn runs, the agent finishes with a report, and that is the
whole lifecycle.

There is no execution state machine, no self-scheduled continuation, and no restart-resume: a server
restart mid-turn fails that turn like it would fail yours, and the next scheduled firing starts a
fresh session. You can open any periodic session in the sidebar (`[PERIODIC]` prefix) and simply
talk to it — it behaves like any other session.

## Configuration

Presets live under `periodic.sessions` in the config:

```json
{
  "periodic": {
    "sessions": [
      {
        "name": "calendar-agenda",
        "initial_prompt": "Produce today's calendar agenda for the current UTC day. List the day's scheduled items in chronological order; if the day is empty, say so.",
        "schedule_interval_seconds": 86400,
        "anchor_utc": "2026-09-03T06:00:00Z",
        "model_level": null,
        "enabled": true
      },
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

- `initial_prompt` — the one message the session receives. Write it as a complete task brief: task,
  scope, hard constraints, expected report. The scheduler prepends a short shared preamble stating
  the periodic contract (finish in this turn, report at the end, no continuations).
- `schedule_interval_seconds` — spacing between firings (min 300, default one day). A never-fired
  preset fires promptly after startup.
- `anchor_utc` — optional fixed UTC instant (ISO 8601, e.g. `2026-09-03T06:00:00Z`) that anchors the
  schedule. When set, the preset fires at this instant and then every `schedule_interval_seconds`
  thereafter, so `every 24h from <ts>` fires daily at the anchor's UTC time-of-day. An anchored
  preset never fires off its cadence: if it is registered after the anchor has passed, the first
  run waits for the next occurrence on/after the current time. Omit it (`null`) to keep the legacy
  cadence — first run promptly after startup, then spaced by the interval from the last firing.
- `model_level` — optional llmio level override (1–3); `null` follows the global model-level
  resolution, like an operator session.

Anchoring a daily digest (e.g. the calendar-agenda job above) at a morning UTC instant makes it fire
at the start of the UTC day and report that day's agenda before the day ends, instead of at an
arbitrary end-of-day time.

If a preset comes due while its previous session is still processing a turn, that firing is skipped
(logged, not queued).

## Shipped presets

### `dependabot-drain`

The committed `config/config.json` ships one preset, `dependabot-drain`, which keeps the repository's
dependency-update pull requests from piling up. On each firing it enumerates the open
Dependabot/Renovate PRs (`list_open_prs`), judges each one's impact (`inspect_pr_diff`,
`verify_pr_ci_status`), merges the safe non-breaking bumps, and files a migration ticket (`POST
/tickets/ingest`) for every breaking change. It complements — never duplicates — any CI-level
auto-merge: PRs already armed to auto-merge are skipped. It finishes with a report of the PRs
merged, migration tickets filed, and PRs skipped.

- **Cadence** — weekly, anchored to Monday 06:00 UTC (`schedule_interval_seconds: 604800`,
  `anchor_utc: "2026-09-07T06:00:00Z"`). It runs at `model_level: 3`.
- **Ships disabled** — the preset ships with `"enabled": false` per the feature-flag convention, so
  it never fires on a fresh checkout.
- **Activation** — set `"enabled": true` on the `dependabot-drain` entry under `periodic.sessions`
  in the deployment's config, then redeploy. To prove it live, fire it once with `POST
  /periodic/definitions/dependabot-drain/run` and read the report, and confirm it appears enabled in
  `GET /periodic/definitions`.

## Endpoints

- `GET /periodic/definitions` — presets with their firing state (`last_fired_at`, `last_session_id`,
  `runs`) and configuration (`schedule_interval_seconds`, `anchor_utc` as an ISO 8601 string when
  set, `model_level`, `enabled`).
- `POST /periodic/definitions/{name}/run` — fire a preset now (409 while its previous session is
  mid-turn).
