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
  preset never fires off its cadence: if it is registered after the anchor has passed, the first run
  waits for the next occurrence on/after the current time. Omit it (`null`) to keep the legacy
  cadence — first run promptly after startup, then spaced by the interval from the last firing.
- `model_level` — optional llmio level override (1–3); `null` follows the global model-level
  resolution, like an operator session.

Anchoring a daily digest (e.g. the calendar-agenda job above) at a morning UTC instant makes it fire
at the start of the UTC day and report that day's agenda before the day ends, instead of at an
arbitrary end-of-day time.

If a preset comes due while its previous session is still processing a turn, that firing is skipped
(logged, not queued).

A firing supersedes the preset's previous run: once the new session exists, the previous run's
session is closed through the same path as `POST /sessions/{id}/close` (its subsessions are closed,
a `session_end` feedback run is scheduled, the memory component receives the final summary). Only
the latest run of each preset is ever open, so periodic runs never pile up as sessions to close by
hand. A failure to close the previous run is logged and never blocks the new firing.

## Writing effective periodic tasks

When you write a periodic task prompt, especially one that retrieves or validates external data,
follow these practices to ensure the operator can verify that data is real, current, and actually
retrieved from live services:

### Require explicit tool invocation logs

When your task involves fetching data (mail, calendar items, board status, etc.), **instruct the
agent to log or report each tool call and its result**. Do not let the agent narrate the intended
action and then synthesize results without showing the actual API responses.

**Bad example:**
```
Review today's unread mail and summarize it for the operator.
```
This allows the agent to say "I'll fetch your mail" and then present made-up summaries without
showing any tool calls.

**Good example:**
```
Review today's unread mail and summarize it for the operator. For each step below, log the tool you
called and the API response you received (or the error, if the call failed).

Steps:
1. Fetch today's unread mail using the mail component's list API, filtering on the date range
   2026-09-20 00:00:00 to 23:59:59 UTC. Log the tool call and the list response.
2. For each message, extract the sender, subject, and a 1-2 sentence summary from the body.
3. Finish with your summary, then append the full tool-call log showing each API interaction.
```

### Fail explicitly when data sources are unavailable

If a required API is unreachable or returns an error, **report the failure clearly instead of
falling back to synthesis or stale cached data**. The operator needs to know whether the report is
based on real current data or a failure.

**Bad example:** "I couldn't reach the mail service, but based on my last knowledge I estimate you
have about 5 unread messages."

**Good example:** "FAILED: The mail service is unreachable (connection timeout after 30s). I cannot
retrieve your unread mail. Please check the mail component status and re-run this task once the
service is available."

### Add validation checkpoints

Include an explicit validation step where the agent **confirms it received real service responses**
before presenting data to the operator. This prevents silent data synthesis.

**Example validation step:**
```
After fetching all data, validate that you received actual API responses (not synthesized data):
- Did each tool call return a structured response object (not empty/null)?
- Does the response contain timestamps, IDs, or other concrete details that prove it came from a
  live service?
If validation fails, report the specific issue and do not present the data as 'fetched'.
```

### Concrete example: mail-review preset

Here is a complete periodic task prompt for a mail-review preset that follows all three practices:

```json
{
  "name": "mail-review",
  "initial_prompt": "Review today's unread mail across all configured mail accounts and summarize it for the operator.

Steps:
1. FETCH: Call the mail component's API to list unread messages in today's date range (today 00:00:00 to 23:59:59 UTC). Include all accounts. Log the tool call name, parameters, and the complete API response (including message count, headers, and any errors).
2. VALIDATE: Confirm the mail API returned a real response with actual message data (timestamps, sender addresses, message IDs). If the API returned an error or empty response, stop here and report the failure — do not synthesize results.
3. SUMMARIZE: For each unread message, extract:
   - Sender address
   - Subject
   - Date received
   - 1-2 sentence body summary
4. REPORT: Finish with your findings formatted as:
   - Total unread count
   - Messages grouped by sender
   - One paragraph per message with sender, subject, date, and summary
   - Timestamp of the API call (from the response)
   - Full tool-invocation log (tool name, parameters, response summary)

If the mail API is unavailable at any step, report the failure and stop — never present data as 'fetched' if you did not actually retrieve it.",
  "schedule_interval_seconds": 86400,
  "anchor_utc": "2026-09-20T09:00:00Z",
  "model_level": 2,
  "enabled": false
}
```

### Patterns to avoid

- ❌ **Narrating without showing work:** "I'll fetch your data and summarize it" without logging tool calls.
- ❌ **Synthesizing when tools fail:** Presenting old cached results or estimates as current data.
- ❌ **Hiding API failures in subsessions:** Reporting success in the main conversation while burying
  failures in subsession metadata.
- ❌ **No data validation:** Presenting results without confirming the source was real and current.

## Shipped presets

### `dependabot-drain`

The committed `config/config.json` ships one preset, `dependabot-drain`, which keeps the
repository's dependency-update pull requests from piling up. On each firing it enumerates the open
Dependabot/Renovate PRs (`list_open_prs`), judges each one's impact (`inspect_pr_diff`,
`verify_pr_ci_status`), merges the safe non-breaking bumps, and files a migration ticket
(`POST /tickets/ingest`) for every breaking change. It complements — never duplicates — any CI-level
auto-merge: PRs already armed to auto-merge are skipped. It finishes with a report of the PRs
merged, migration tickets filed, and PRs skipped.

- **Cadence** — weekly, anchored to Monday 06:00 UTC (`schedule_interval_seconds: 604800`,
  `anchor_utc: "2026-09-07T06:00:00Z"`). It runs at `model_level: 3`.
- **Ships disabled** — the preset ships with `"enabled": false` per the feature-flag convention, so
  it never fires on a fresh checkout.
- **Activation** — set `"enabled": true` on the `dependabot-drain` entry under `periodic.sessions`
  in the deployment's config, then redeploy. To prove it live, fire it once with
  `POST /periodic/definitions/dependabot-drain/run` and read the report, and confirm it appears
  enabled in `GET /periodic/definitions`.

### `gate-drain`

The committed `config/config.json` also ships a `gate-drain` preset, which keeps the operator
informed of production-blocking issues and human-gated/blocked tickets across boards. On each firing
it scans the main branch for any failing workflows (CI failures are production-blocking and take
absolute priority), reports those first in the main conversation with clear ALERT flags and
recommended fixes, and then enumerates every ticket in the gated states — `human_issue_approval`,
`human_mr_approval`, `awaiting_user_reply`, and `blocked` — across all boards. Its **first output is
a scope report emitted in the main conversation**: each current-main CI failure (one ALERT line per
failure with recommended fix PR and recommendation), the total count of gated tickets discovered, a
per-state count summary, and an explicit flag when it will only analyze a subset (the
discovered-vs-analyzed mismatch). Only after that scope report does it begin detailed per-ticket
processing.

- **Production CI failures are front-loaded.** The operator's primary goal is keeping main green.
  Current-main CI failures are production-blocking and must be reported at the TOP of the main
  conversation before the gated-ticket enumeration, with a clear ALERT flag, recommended fix PR, and
  recommendation for each failure. Never bury a CI failure in subsession metadata that a
  main-thread-only reader would miss.
- **Scope reporting belongs in the primary conversation.** Subsession summaries capture follow-up
  work items, not top-line scope. The CI failures, total discovered count, the per-state summary,
  and any discovered-vs-analyzed mismatch must appear in the main conversation where the operator
  sees them immediately — never buried in a closed subsession summary. This preset exists because a
  2026-09-09 gate-drain run reported ~14 tickets in the main conversation while a subsession had
  actually discovered 47 across multiple boards; the 3× scope expansion never reached the operator.
  And a 2026-09-19 incident identified that a broken robotsix-mill Docs workflow
  (production-blocking CI failure) existed only in subsession metadata and never reached the
  operator's main conversation.
- **Cadence** — every four hours (`schedule_interval_seconds: 14400`), at `model_level: 2`.
- **Ships disabled** — `"enabled": false` per the feature-flag convention, so it never fires on a
  fresh checkout.
- **Activation** — set `"enabled": true` on the `gate-drain` entry under `periodic.sessions` in the
  deployment's config, then redeploy. To prove it live, fire it once with
  `POST /periodic/definitions/gate-drain/run` and confirm the main-conversation report leads with
  any current-main CI failures (one ALERT line per failure with recommended fix PR) and then the
  total discovered count and the state-by-state summary (with no scope mismatch left silent), and
  that it appears enabled in `GET /periodic/definitions`.

## Session-end contract and interrupted reports

Every periodic session has an **unconditional obligation to report at the end** — even if it is
interrupted by timeout, error, resource exhaustion, or early termination. A session that ends
without a report leaves the next scheduled run blind to what was attempted, done, escalated, or
blocked.

### Guaranteed reports

The scheduler prepends a preamble to every preset's initial prompt that instructs the agent to
prioritize the report as the **first deliverable**, not the last.

The agent **cannot introspect its own token consumption or context-window usage** — there is no
budget field or tool that tells it how close it is to the limit, and the limit can be reached
without warning mid-operation. Because "sensing" exhaustion is not actionable, the preamble instead
makes the report resilient to sudden termination by having the agent **report as it goes**:

1. **Report at natural breakpoints, not just at the end.** After each atomic unit of work (each PR
   merged, each ticket processed or drained, each subsession opened, each investigation closed), the
   agent outputs an updated running `PARTIAL REPORT` capturing everything done so far, restating the
   full report each time — the last complete report the transcript contains is what survives.
1. **Reserve headroom for the report.** The agent does not pack the turn end-to-end with operational
   work; once a batch of major operations is done, it prefers stopping and emitting a final report
   over starting another expensive operation that could be cut off before it reports it. A smaller
   batch that is fully reported beats a larger batch that terminates unreported.
1. **Report immediately on error.** If the agent hits an error mid-execution, it stops the remaining
   work and outputs the report while it still can.

### PARTIAL REPORT (the expected outcome for interrupted work)

`PARTIAL REPORT` is the **expected outcome** for any interrupted or incomplete periodic session, not
an emergency fallback. Whenever a periodic task cannot finish everything — and at every breakpoint
above — it outputs a report titled **`PARTIAL REPORT`** with three sections, in this order:

1. **Done** — items completed and the ROUTINE actions taken (e.g. tickets drained, PRs merged or
   filed), each named. For gate-drain: any tickets processed and their disposition. For
   dependabot-drain: any PRs merged and migration tickets filed.

1. **Escalations** — subsession IDs opened and the decision each one needs from the operator. If the
   agent opened a subsession for a per-ticket merge decision or investigation that is still pending,
   record it here so the operator knows it exists and awaits their input.

1. **Held for next run** — items not reached or deliberately deferred, each with a one-line reason
   (e.g. "47 remaining tickets need prioritization order"; "PR #123 blocked on feedback").

A PARTIAL REPORT is always better than silence. An interrupted session that reported what it got to
gives the next scheduled run the context to resume. One that reported nothing leaves no path
forward.

### Example: dependabot-drain interrupted

If dependabot-drain times out after processing 3 of 10 Dependabot PRs:

```text
PARTIAL REPORT

Done:
  - PR #456 (lodash): merged (green CI, non-breaking bump)
  - PR #457 (eslint): filed migration ticket #ticket-123 (breaking API change)
  - PR #458 (typescript): skipped (already auto-merging)

Escalations:
  - Subsession #sub-789: awaiting operator approval for PR #459 schema migration

Held for next run:
  - 7 remaining Dependabot PRs (session token budget exhausted)
```

The next `dependabot-drain` run will see these results and skip the 3 already-processed PRs,
resuming with PR #460.

### Example: gate-drain interrupted

If gate-drain times out after reporting current-main CI failures and scope:

```text
PARTIAL REPORT

Current-main CI:
  ⚠️ ALERT — main CI failing: robotsix-mill: Docs workflow (conclusion: failure);
  recommended fix: PR #723; recommendation: merge and re-run

Done:
  - 12 gated tickets analyzed
  - 3 tickets drained (moved to resolved state)
  - 2 subsessions opened for merge decisions (see Escalations)

Escalations:
  - Subsession #sub-890: awaiting operator approval for PR #410 (merge or close?)
  - Subsession #sub-891: awaiting operator decision on ticket #board-567 (reassign or close?)

Held for next run:
  - 35 remaining gated tickets (session turn limit reached)
  - Current-main CI failure to be re-checked next run
```

The next `gate-drain` run sees the current-main CI failure is still present and re-confirms or
updates its status, and resumes the gated-ticket enumeration with the remaining 35.

## Endpoints

- `GET /periodic/definitions` — presets with their firing state (`last_fired_at`, `last_session_id`,
  `runs`) and configuration (`schedule_interval_seconds`, `anchor_utc` as an ISO 8601 string when
  set, `model_level`, `enabled`).
- `POST /periodic/definitions/{name}/run` — fire a preset now (409 while its previous session is
  mid-turn).
