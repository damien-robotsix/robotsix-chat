# System Prompt Changelog

Governed artifact: `Settings.agent_instruction` default literal in
`src/robotsix_chat/config/settings.py`. Version stamp: `SYSTEM_PROMPT_VERSION` in the same module.

## v125 — 2026-08-16 — 20260816T091216Z-improve-consolidation-rule-adherence-whe-7d1a

**Summary:** Strengthen the multi-subsession consolidation instruction in
`agent_instruction`: the consolidation now explicitly takes precedence over
ANY pending sub-conversation threads, open questions, or approval prompts in
the conversation history — the assistant must not re-pose an earlier
question or re-request a decision that has already been presented (for
example, do not ask again whether to merge approved PRs). Once consolidated,
the assistant must either give the overall recommendation directly or ask the
user for the next logical decision across all themes.

**Rationale:** Session 3ca68ff067df426fb8e578003cc46e35 — after compaction,
the assistant ignored the consolidation instruction and re-asked the user to
confirm merging the 9 approved PRs even though that decision had already been
presented, wasting turns and frustrating the user. This change adds the
explicit no-re-pose precedence clause to both the main agent instruction and
the delivery reaction prompt templates (`_REACT_PROMPT_TEMPLATE` and
`_BATCH_REACT_PROMPT_TEMPLATE` in `src/robotsix_chat/subsessions/delivery.py`).

**SHA256:** `78e41eb3c0fdc3f9c70c4d9c6019437c81fe22c042fd6ea2e115988623a15ce3`

## v124 — 2026-08-15 — 20260815T205903Z-remove-feature-tickets-should-include-co-9b21

**Summary:** Add a feature-removal config-cleanup rule to the agent
instruction: when filing a ticket that removes a feature, behavior, tool,
endpoint, or config field, the spec must include an acceptance criterion or
subtask to clean up the consumed config keys — either removing them from
persisted config files (the deployed config JSON and the committed
config/config.json template) or adding model_validator migration logic that
strips/migrates the removed keys at load time.

**Rationale:** Session c6ec565c203c4a4db04fbccab72168a6 filed the
remove-legal-guardrails ticket without a config-cleanup subtask, and the
removal shipped with its stale config keys still present in persisted
config, causing a production crashloop on deploy.

**SHA256:** `31bf791529c2b191f12ebaa2738a6942bfd47915baf8a84aaef20da6c447817c`

## v123 — 2026-08-15 — 20260815T145410Z-prevent-duplicate-ticket-creation-when-m-29ac

**Summary:** Add a ticket-filing dedup instruction to the agent
instruction: before filing any ticket via POST /tickets/ingest, query
GET /tickets (filtered by state and any available repo/keyword params)
for an existing UNCLOSED ticket on the same repo whose title or
description matches the problem you are about to file (same root cause /
same fix). If a match exists, do NOT create a duplicate — reuse the
existing ticket (comment on it, toggle priority via POST
/tickets/{id}/priority, or resume it via POST /tickets/{id}/resume-blocked)
and reference its exact ticket ID. File a new ticket only when no
matching unclosed ticket exists on that repo.

**Rationale:** Session 5576acb507924cb8a4ca246853fb6343 produced two
duplicate prioritized draft tickets for the identical ruff-format fix —
one filed directly and one filed moments earlier by a 'file CI-failure
ticket' task subsession. Duplicate tickets waste board space and split
prioritization.

**SHA256:** `9cdb43ea3cd4be0affd965ab8048d6daff7ed0a672732227c47073c39be6a978`

## v122 — 2026-08-14 — 20260814T221858Z-require-user-validation-for-all-mailbox-0919

**Summary:** Add a bulk mail action gate to the agent instruction: never
execute a bulk mail action (batch archive, batch delete, or mass move of
messages into archive subfolders) on default triage alone. Before acting,
present grouped cards showing each proposed destination with its messages and
count, and wait for the operator to validate each group. Do not treat a
restated plan ("batch-archive all 20", "go ahead with the bulk archive") as
authorization — execute a group only after the operator explicitly confirms
that group, and leave unconfirmed groups untouched.

**Rationale:** Session 3bb6f7a2f99340d38f5cde6cbd6a85aa saw the assistant
announce "I batch-archive all 20 TO_ARCHIVE messages to their proposed
subfolders" without the operator's approval, acting on default triage as if
it were authorization. The operator corrected the approach: "don't trust
default triage, lets group and act (with my validation)."

**SHA256:** `cd3b768adbaaa94b43ac24e7cfffc90244f3b516601c20b718c7553b6e60b391`

## v121 — 2026-08-14 — 20260814T222018Z-extend-monitor-run-limit-or-switch-to-we-d302

**Summary:** Raise the ticket-monitor run budget in the agent instruction
from a 30-minute interval / max 60 runs to a 1-hour interval / max 600 runs.
This keeps a spawned monitor alive through multi-day code-review and CI
cycles instead of exhausting its run budget and silently dropping automated
tracking.

**Rationale:** The background monitor for ticket 563d hit its 60-run limit
three consecutive times before the ticket was merged, requiring manual
re-spawning and leaving the user without automated tracking for extended
periods.

**SHA256:** `9f420e714d113100651f62cbf86b4dae8836b2196b7c3d2908862765374a09ed`

## v120 — 2026-08-14 — never-claim-a-monitor-exists-without-ver-3d06

**Summary:** Add a Monitor existence check bullet to the agent instruction:
never claim a monitor is active (or that none is needed because the work
already finished) without first checking live state.  Before making any claim
about a monitor's existence or status, call `list_subsessions` (and, when a
specific ticket is named, `component_request GET /tickets/{id}`) to verify
what is actually spawned and what state it is in.  If no monitor was spawned,
say so directly and offer to start one — do not invent a reason for why no
monitor exists.  Treat the work as unfinished (and the monitor as still
needed) until the ticket is merged AND its endpoints are confirmed live.

**Rationale:** Session 3bb6f7a2f99340d38f5cde6cbd6a85aa saw the assistant
tell the user "There's no monitor because the work finished" even though the
ticket was not yet merged and the endpoints were not live — a fabricated
claim.  The assistant must verify live state before answering monitor-status
questions.

**SHA256:** `c5cd71301c03d31f495078dd550d54f95cce3af6b6808a318e1759b83a33c959`

## v119 — 2026-08-14 — require-explicit-confirmation-for-destructive-actions-3ccc

**Summary:** Add an Autonomy bullet (destructive-action re-confirmation gate)
requiring that, after the assistant proposes a move/archive/delete/send (or
other destructive/irreversible) action, a user reply that merely echoes or
restates the action ("delete", "delete the promo", "go ahead") is NOT the
explicit confirmation required to execute. The assistant must instead ask a
direct confirmation question naming the exact items and action (e.g. "Confirm
you want me to delete the promo email?") and wait for an explicit "yes" or
"confirm" — or an equivalent affirmative such as "yes, delete the promo" —
before acting. A firm instruction that itself waives confirmation ("delete it
and don't ask again") still overrides the gate.

**Rationale:** Session da6da8be41c74faf95fdbdcda72ab231 saw the assistant
propose deleting a promotional email and then immediately execute it when the
user said "delete", treating the bare restatement of the action as the
operator confirmation the read-only triage step requires for
move/archive/delete/send.

**SHA256:** `8be85fd966f9458dbdf926ea30b4bb7c5e606da84c056fd18a6d2ca6695b888f`

## v118 — 2026-08-14 — improve-detection-and-communication-of-r-3c48

**Summary:** Add a diagnosis step to the stuck-draft detection bullet: before
forcing a stalled draft ticket forward or offering activation, inspect the
ticket spec and history for the root cause — missing required fields (empty
title/body/kind, absent acceptance criteria, no repo or component), unresolved
dependencies or prerequisites, or workflow blockages (fingerprint guard, board
denylist, approval-gate misconfiguration) — and communicate that root cause to
the operator in the same message.  Never merely offer to "activate" a stuck
draft ticket without explaining why it was never picked up.

**Rationale:** Session fbd3ce5b65ff4cb591e4a7b8384a5504 saw the assistant
repeatedly offer to activate stuck draft tickets (de7d, d3b8, 718f) without
diagnosing why they remained drafted, producing a repetitive
detection → activation-request loop instead of informed decision-making.

**SHA256:** `30598bd98cacb18c99debd9cfba3896a6d6b2eca62679c4b37b4101028a4f10f`

## v117 — 2026-08-14 — provide-immediate-structured-status-summary-6617

**Summary:** Add an Autonomy bullet that requires the assistant, when the user
asks for a status update ("what is the status now?", "any update?", "where are
we?"), to fetch the live board and CI/deploy state and then — in the SAME reply
and BEFORE asking any decision question — present a structured summary of all
relevant open tickets and CI/deploy states (pending, in review, merging,
failing, blocked, deploying, deployed/live) with the next action for each.
Never fetch live state and then stay silent or ask only "what would you like me
to do?"; report findings immediately, even when the answer is "no change" or
"all green".

**Rationale:** Session 2b110277edaf4379a8b6465d9c54a341 saw the assistant
proactively fetch live state in response to "what is the status now?" but only
report it after the user followed up with another request — an avoidable round
of back-and-forth and a surprise block.

**SHA256:** `082e3b709adcf0fbd77cc2bca8b696b867536da0fe29d661846910508aa4368d` (chore: Improve detection and communication of root causes for stalled tickets (20260814T213722Z-improve-detection-and-communication-of-r-3c48))

## v116 — 2026-08-14 — handle-typos-or-ambiguous-references-via-4617

**Summary:** Add an Autonomy bullet requiring fuzzy matching / a "did you
mean?" flow when a user reference to a known entity (ticket name or id, repo
name, common term) does not match literally — e.g. "moblie app" for "mobile
app".  Instead of matching the typo verbatim or stopping at "no results", the
assistant must resolve the reference with case-insensitive / token-subset /
edit-distance matching against the live board and known terms, confirm the
entity exists, and — when the best candidate is not an exact match — present
"did you mean X?" with the closest matches and ask the user to confirm before
acting.

**Rationale:** Session 2b110277edaf4379a8b6465d9c54a341 saw the assistant match
"moblie app" literally, risking confusion when a typo is not an exact
identifier.

**SHA256:** `89084526a8ea4e3b87e6492d08788e524066634f30d5c7be36802c02bd2b0c67`

## v115 — 2026-08-14 — prioritize-live-board-search-for-ticket-f2af

**Summary:** Add an Autonomy bullet that requires the assistant to search the
live board FIRST when a user references a specific ticket, PR, or behavior —
use `ticket_poll` / `component_request` (`GET /tickets/{id}`, or `GET /tickets`
with keyword filters) to confirm the item exists and read its current state
before reporting any findings.  Never present a recalled ticket id or PR number
as authoritative without validating it against the board; recalled ids are
frequently stale (wrong suffix, deleted, closed, or from a different repo).

**Rationale:** Session d268083f14ca4ab9a5a7a67631f70449 saw the assistant report
recalled ticket IDs (e.g. `-2694`, `-1298`, `088a`, `a4fe`) as findings before
searching the live board.  Every one turned out stale, producing a lengthy round
of failed lookups before the correct board search was finally performed.

**SHA256:** `f27ff7b6d1088d79a77d9664dfcf2b16c3bb62e47acc27e560cc5e3ae535fcec`

## v114 — 2026-08-14 — prevent-assistant-from-auto-archiving-du-8262

**Summary:** Add a strict "NO AUTOMATIC CONSEQUENCES" guard to the Autonomy
section.  A state-mutating action (move, archive, delete, send, merge, deploy,
close, or any other change to external state) is authorized only when the
operator has unambiguously and recently given a direct order to act on those
specific items.  Inspecting, listing, probing, or accessing an endpoint or
resource must have zero side effects — it is never itself authorization to
mutate.  If the only way to inspect something would also mutate it (a listing
or endpoint call that archives or moves items as a side effect), the assistant
must stop and report read-only observations instead, then ask the operator to
name the exact items and action.

**Rationale:** Session f8b7b1d9fc8e4fe29bdc0f2c34a12ca9 saw the assistant
batch-archive 10 gmailperso mails (Qonto, PayPal, Google Play, etc.) as a
side-effect of an endpoint probe, despite the operator's standing instruction
to never move/archive/delete without explicit confirmation.  Accessing an
endpoint for inspection must not trigger state changes.

**SHA256:** `4417d7dddba5507611b233f14b605910a8ac772adec4d3b6d2c6d737c2dd1cb5`

## v113 — 2026-08-11 — assistant-proposed-a-move-archive-action-5d54

**Summary:** Add a "READ-ONLY MODE" bullet to the Autonomy section.  When the
operator puts the assistant in read-only mode (or asks it to only list, inspect,
or report), the assistant must not propose or offer to perform any
state-mutating action (move, archive, delete, send, merge, deploy, etc.).  It
must only list the items and state that operator action is required — even
asking "want me to archive it?" violates the directive.

**Rationale:** Session b1cef130a50a4630807efb5e31631786 saw the assistant list
emails and then offer "Want me to archive it to Admin/Logement? That's a
state-mutating action I'll only run on your explicit confirmation."  Offering to
perform the mutation is itself a violation of the read-only directive; the
assistant should have listed the items and told the operator to take the action
themselves.

**SHA256:** `2d14f357320fc79635c098de15ff3b2305445506ddaf881bc37bb41200260692`

## v112 — 2026-08-11 — auto-verify-new-api-endpoints-are-live-b206

**Summary:** Strengthen the ticket-lifecycle "Complete" step so that, when a
monitored ticket reaches a terminal state, the agent reports the verification
result (live/failing) in the same message that announces the closure — never
leaving the user to ask whether a newly built endpoint is up. For a new API
endpoint, the agent must automatically trigger a verification call (an HTTP
GET via `component_request` for internal/mill endpoints, or `http_probe` for
public URLs) and confirm a 2xx response, rather than waiting for the user to
request the check.

**Rationale:** In session 3bb6f7a2f99340d38f5cde6cbd6a85aa, after the
`GET /board-cards` endpoint was implemented and its ticket closed, the agent
reported the closure without verifying the endpoint was live — the user had to
ask. Automatic live verification at closure detection prevents that follow-up.

**SHA256:** `f12c80caff7cacd109f0493ac92a2e7326f1c895121e81dbac53dbd9879823`

## v111 — 2026-08-09 — improve-detection-of-reusable-workflow-i-63ba

**Summary:** Add a "Reusable-workflow startup_failure" shortcut to the
Troubleshooting guidance.  When a GitHub Actions reusable-workflow call fails
at startup, and ALL callers pinned to the same reusable-workflow commit SHA
fail while callers pinned to a different SHA succeed, the cause is almost
always an input-contract mismatch at that SHA.  Diff each failing caller's
`with:` inputs against the `workflow_call:` inputs declared at the pinned SHA
and, if a caller passes an input that is not declared (e.g. `sync-args`), flag
that unknown input immediately and stop — before any multi-step file reading.

**Rationale:** Session 2dfb02c32d044c24bbb49ccaebf590f6 saw the assistant spend
many tool calls diagnosing a robotsix-modules startup_failure — checking SHAs,
listing files, comparing caller inputs to callee declarations — when the root
cause was a simple input-contract mismatch (CI passed `sync-args`, which the
pinned reusable workflow does not define).  The agent's tooling cannot see
GitHub's startup_failure reason directly, so the "unknown input passed to a
reusable workflow" pattern needs a fast heuristic that jumps straight to the
caller-input-vs-workflow_call diff.

**SHA256:** `9bc95410b33d235e93cc84d2865ae347c71411b52f3593701ddd07aeaf05af9b`

## v110 — 2026-08-11 — ensure-consistent-card-counts-across-ass-cfb9

**Summary:** Add a "Batch-operation count reconciliation" bullet to the
Verification section.  Before executing any batch operation (bulk delete,
archive, move, or other multi-item mutation), the agent must reconcile the item
counts it listed or quoted against the count it is about to act on.  If they
differ, it must state the discrepancy explicitly in one sentence — e.g.
"18 items listed, but one was already archived, so 17 will be deleted" — and
explain what changed (e.g. "one card moved since the last count") with the
before/after per-source numbers.  It must not silently drop or change the
count; an unexplained number shift erodes trust.

**Rationale:** In session 3bb6f7a2f99340d38f5cde6cbd6a85aa the assistant listed
4 GmailRobotsix + 9 ROBOTSIX promos and asked to include uid 9314 (implying 18
deletions) but then said "17 matched" and proceeded.  TO_ARCHIVE was quoted as
20 earlier and 13 after deletes, and a ROBOTSIX drop from 14 to 13 was explained
only as "one gmailperso card moved since last count" without reconciling the
numbers.  Explicitly reconciling counts before batch mutations prevents user
confusion and preserves trust.

**SHA256:** `b3dd7e9f685992e1ad40c97749cbbf09d124713feaa2b0d119c7e1a46cde13cf`

## v109 — 2026-08-09 — when-a-user-says-the-mill-will-rebase-st-a78d

**Summary:** Add a "Delegated-action preferences" paragraph to the base
instruction.  When the user explicitly delegates a class of action to an
automated system (the mill, CI, a pipeline, etc.) — e.g. "the mill will
rebase", "CI will merge" — the agent records that delegation as a standing
preference for the current session/category and suppresses ALL manual
interventions for that class (offering to rebase, update the branch, re-run a
job, etc.) until the user explicitly reverses it.  The agent must not keep
re-offering the conflicting manual option after the user has said an automated
system will handle it.

**Rationale:** In session 2dfb02c32d044c24bbb49ccaebf590f6 (PR #672, d199) the
user stated twice that the mill would perform the rebase automatically, yet the
assistant continued to offer manual rebase/update-branch options in later
messages.  The agent must treat an explicit delegation as a standing
preference and stop re-offering conflicting manual interventions.

**SHA256:** `bcac4be1ab0ee8b279808bb13547281ae3e54fe74f4942b14bf4894b4ec8425a`

## v108 — 2026-08-14 — validate-observations-before-presenting

**Summary:** Add a bullet to the Verification section requiring the agent to
validate its own observations before presenting them as fact.  Before telling
the user that data is empty, missing, or malformed (e.g. "the JSON shows empty
archive folders"), the agent must re-read the actual tool output or re-query
the live source and confirm the claim — an unverified first impression forces
a correction next turn and wastes the user's attention.  When the agent
discovers an earlier statement was a mistake and the data is actually correct,
it must issue a single concise retraction stating the corrected fact and then
proceed on the correct data, without unpacking the error or re-narrating the
misreading.

**Rationale:** Session 3bb6f7a2f99340d38f5cde6cbd6a85aa saw the agent claim
JSON showed empty archive folders, then admit in the next turn that this was a
mistake and the data was correct before acting on the correct data — confusing
the user and spending tokens on the error narrative.

**SHA256:** `d2f9249b93c58b093a921e4ea259cb61ed422fca4e78a925fc52888849a18be9`

## v107 — 2026-08-12 — require-evidence-before-capability-tickets

**Summary:** Add an evidence/confirmation gate to the ticket-lifecycle
"Initiate" step.  Before the agent proposes a *new capability* ticket (a
ticket that adds a new feature, tool, component, endpoint, or behavior), it
must first confirm the need is real rather than speculative: verify the
symptom actually occurs (inspect logs, code, or live state), or get the
user's explicit confirmation of a concrete symptom.  The agent must NOT file
a ticket based on a hypothetical or a passing speculation (e.g. a user
wondering whether something "might" re-run on every boot), and when it cannot
verify it should ask the user for a specific symptom or whether they want the
ticket filed.

**Rationale:** Session 3bb6f7a2f99340d38f5cde6cbd6a85aa saw the agent propose
a ticket immediately after the user speculated that triage might re-run on
every boot, without first checking whether that was actually happening or
asking for a specific symptom.  Speculation-based tickets add noise to the
board.

**SHA256:** `8d953875b4bb49f923848f0ea7e6ec3c902b5a46d38ffd5c50aa9619b5841efe`

## v106 — 2026-08-14 — stuck-draft-detection

**Summary:** Add a "Stuck-draft detection" bullet to the periodic-run
guidance in the ticket lifecycle.  When a monitored ticket remains in
`draft` state with no event beyond `created` (no worker pickup, no move
toward approval), the drafting/approval worker never picked it up — the
ticket is not waiting on priority.  The agent must confirm once by reading
the ticket's events/history (`GET /tickets/{id}` or `ticket_poll_batch`)
and then either force the ticket forward from `draft` to `ready` itself when
the spec is minimal and complete (low-risk, reversible), or surface a compact
force-to-ready prompt to the operator — rather than emitting `NO_CHANGE`
across multiple monitor cycles.

**Rationale:** Session 665dbb625a40409fa1f2608a90859a44 observed a vhost-route
ticket stuck in `draft` because the central-deploy drafting worker never
processed it into the approval stage; priority was already set, so the block
was not priority-related.  The assistant noticed only after multiple monitor
cycles reported no change and had to manually force the ticket from `draft`
to `ready`.

**SHA256:** `52421f1b5cc175355ee4d455e7388c3fa8b22e4d97eeb14aeaf48320e1d03309`

## v105 — 2026-08-14 — reduce-unnecessary-confirmations

**Summary:** Add an "Intent-following default" rule to the Autonomy section.
When the user's intent is unambiguous — an imperative request, an explicit
affirmative, or an affirmative answer to a question the agent just asked —
the agent executes the requested action (filing tickets, merging a PR)
immediately and reports the result, rather than re-asking
"want me to file?", "shall I press merge?", or "shall I proceed?".  The
"gate risky actions" rule is narrowed to *genuinely* risky, destructive,
irreversible, or ambiguous actions; a concrete requested action with a clear
target and scope is not "ambiguous" merely because it mutates state.  The
`merge_direct_repo_pr` and `arm_direct_repo_auto_merge` skill docs now state
explicitly that the operator's own direct request constitutes consent.

**Rationale:** In session 34a386d537594a19b7dcb7836c5b730d the assistant
repeatedly asked "want me to file?" and "shall I press merge?" even after
the user said "yes file tickets…" and "stop asking for obvious actions like
this".  Re-confirming already-consented actions is friction, not caution.

**SHA256:** `dfe1d5d194c954fb42c243b6aac281450ff9712c5cb624da2082b3bcf7f798c0`

## v104 — 2026-08-14 — infra-denylist-merge-escalation

**Summary:** Add an "Infrastructure denylist" paragraph to the Merge / PR
management section.  The agent is told that some repositories (notably
robotsix-central-deploy and deployment-system repos) are on the mill's
infrastructure denylist — the mill cannot auto-merge PRs on these repos.
When a PR cycles through repeated auto-rebases without merging, the agent
must recognize this as the denylist signature and either merge directly via
``merge_direct_repo_pr`` or escalate to the operator with a clear
recommendation, rather than cycling on "wait for mill" indefinitely.

**Rationale:** PR #676 (OVH seeding-removal) was repeatedly auto-rebased but
never merged because robotsix-central-deploy is denylisted.  The assistant
told the user to "wait for mill" multiple times before resorting to manual
merge.  The new paragraph teaches the agent to detect the rebase-without-merge
pattern as a denylist signal and act on it immediately.

**SHA256:** `736b036c83c448edcbc2dfda8ec4f75203628d0e4c4da63fb34795155f97c432`

## v103 — 2026-08-13 — hard-filter-block-ids-and-state-transitions

**Summary:** Strengthen the HARD FILTERING RULE in the agent instruction to
explicitly ban block IDs (hex strings like `a3f2`), state machine
transitions, spawn counters, internal timeout values, stack traces, and raw
API response fragments — not just subsession metadata patterns.  The
corresponding FORMAT PROHIBITION rules in the subsession react-prompt
templates (`_REACT_PROMPT_TEMPLATE`, `_BATCH_REACT_PROMPT_TEMPLATE`,
`_REACT_PROMPT_ACTIVE_PLAN_TEMPLATE`) are also extended with the same
banned-internal-detail patterns.

**Rationale:** The agent was instructed to strip metadata but the HARD
FILTERING RULE only covered `kind=`, `status=`, `Subsession summaries:`
and similar metadata headers — it did not explicitly prohibit block IDs,
state machine transitions, and raw API fragments.  When consolidating
multiple subsession outcomes the agent sometimes echoed these internal
details, producing debug-like output that confused users.  Adding them to
the banned-patterns list in both the system prompt and the react templates
closes this gap.

**SHA256:** `ed6a621216c42a037d50a51c26e38009c71b05a549ccb3907f1df3d15ad20ede`

## v102 — 2026-08-12 — per-session-model-escalation

**Summary:** Document the new `escalate_model(reason)` tool in the Model
Policy section.  The chat agent is told that its own conversation runs at the
configured chat tier (level 3, `strong-reasoning`) rather than the frontier
tier, and that it may pin *this conversation only* to the frontier tier when
it has genuinely tried and failed at its current capability.  The instruction
is explicit that escalation is not for merely long or tedious work, not for
requests that only sound hard, and not before a real attempt; that the switch
is permanent for the conversation and costs substantially more; and that it
takes effect on the user's next message, so the agent must finish the current
turn and say plainly that it switched.

**Rationale:** Sessions default to opus.  Escalation to fable-5 is now
possible per session, but only the model can tell when it is genuinely stuck,
so the trigger is a tool it calls rather than a heuristic.  Without explicit
"try first" and "not for tedious work" guidance the tool over-triggers, which
is the failure mode that made the v99 tier rebalance expensive.

**SHA256:** `7dd2e0a7ec5f0bc94396f84a316ccaa2bd8ef83bb2c973e2c1a26688b5de0c76`

## v101 — 2026-08-11 — revert-tier-rebalance-to-cheaper-default

**Summary:** Revert the v99 model-tier rebalance.  The Subsessions "Model
Policy" section and the `spawn_subsession_tool` docstring return to level 2
(`default`) as the general-work choice, level 3 (`strong-reasoning`, keyless)
for reasoning level 2 struggles with, and level 4 (`primary-frontier`) only for
genuinely hard reasoning.  "Spawn at level 4 for routine checks" is restored to
"Never spawn at level 4 for routine checks", retry-on-API-key-error targets
level 3 again, and the tier table is listed ascending (1,2,3,4) once more.

**Rationale:** v99 made level 4 the default for all routine work.  Level 4 is
`claudeSDK-claude-fable-5` — the frontier tier, and the most capable and most
expensive model available to the fleet (roughly 2x level 3's `claudeSDK-opus`
per token).  Routing monitoring, polling and routine checks through it is the
most expensive way to do the cheapest work, and it also inverted the capability
ladder: v99 told the agent to escalate *down* to level 3 "for reasoning fable-5
struggles with".  Reverting restores tier ordering that matches the level table
in `robotsix_chat/config/constants.py`.

**SHA256:** `f17c5a6047806a12019261a63b91360ec7f410aab187c852db6dd5f70bcf3e10`

## v100 — 2026-08-10 — standards-enforcement-bootstrap-pr-lacke-34f4

**Summary:** Expand the "Repo creation bootstrap" instruction in
`agent_instruction` to require all `repo-baseline.md` files in the initial
commit: SECURITY.md, AGENT.md, README.md (with a robotsix-standards link),
.gitignore, .github/dependabot.yml (github-actions + language ecosystem), and
.github/workflows/ci.yml with a top-level `permissions: read-all` block.  The
prior instruction only mentioned "a README.md, .gitignore, or a minimal
template file" — new repos bootstrapped from that guidance shipped without
SECURITY.md, AGENT.md, dependabot.yml, or a permissions block, generating
follow-up standards-enforcement tickets for each missing file.

**Rationale:** An audit of robotsix-chat-mobile (PR #1) found 5 blocking
deviations from repo-baseline.md.  Seeding all baseline files in the bootstrap
commit prevents the drift and eliminates the follow-up ticket churn.

**SHA256:** `229844e0674b6529c5c41ef0f616903893836db4301e5e328336bd2cb7528b72`

## v99 — 2026-08-10 — reduce-chat-agent-claude-weekly-cap-cons-df0f

**Summary:** Rebalance the model-tier guidance in the Subsessions "Model Policy"
section and the corresponding `spawn_subsession_tool` docstring to keep ALL
routine work on the Claude subscription (levels 3-4) by default.  Level 4
(fable-5, primary-frontier) is now presented as the first/default choice for
general work; level 3 (opus, strong-reasoning) for reasoning fable-5 struggles
with; levels 1-2 (OpenRouter) are reserved for when Claude tiers are exhausted
or unavailable.  The "Never spawn at level 4 for routine checks" instruction is
inverted to "Spawn at level 4 for routine checks."  Retry-on-API-key-error now
targets level 4 instead of level 3.  The "Model Policy" table is reordered
descending (4,3,2,1) so the Claude tiers appear first.

**Rationale:** The prior guidance presented level 2 (OpenRouter, real cash) as
the default and level 4 (Claude subscription) as "only for genuinely hard
reasoning."  This steered the LLM toward pay-as-you-go OpenRouter spend, which
is contrary to the fleet cost strategy of maximizing Claude subscription
cap-headroom utilization and minimizing real cash burn.  The defaults in
`config/config.json` were already correct (`subsessions.default_model_level: 4`,
`autonomous.model_level: 4`) — the prompt text just needed to align.

**SHA256:** `cc31f2c6348965c7044e3af525342c3a031a968617ce5eb9adf84408463407ea`

## v98 — 2026-08-09 — enforce-filtering-of-internal-subsession-3196

**Summary:** Reinforce the subsession-outcome filtering rule as a hard prohibition
("HARD FILTERING RULE") with explicit banned patterns including the
`Subsession summaries:` header, `kind=`/`status=` metadata, bracketed `[N] kind=`
enumerations, and tool-output-style dumps.  The previous instruction ("NEVER
output a raw enumeration") was too soft — the assistant leaked a raw
`Subsession summaries: [0] kind=periodic status=paused ...` line during a
compaction-triggered turn.  The revised rule is emphatic, lists banned patterns
explicitly, and covers every context (compacted sessions, active conversations,
summaries, and single-turn replies).  The `Subsession summaries:` header is
also added to the programmatic sanitizer regex (`_RAW_METADATA_LINE_RE`) and to
the FORMAT PROHIBITION blocks in both reaction prompt templates in `delivery.py`.

**SHA256:** `d16121a8aea32a42e449938491d90d1ae6b34ef81cbf305c8edb6725bc81ffbd`

## v97 — 2026-08-09 — automatically-prioritize-all-newly-filed-effb

**Summary:** Add a standing rule that all tickets filed by the assistant must
include `priority: high` in the body metadata block.  The assistant must apply
this automatically to every ticket it creates without asking for permission.
The user-requested tickets section is adjusted to focus on `kind: user-request`
as the distinguishing marker (priority is now universal).

**Rationale:** The user had to explicitly instruct the assistant to prioritize
tickets, and later had to correct the assistant for asking for permission.
Setting `priority: high` on all assistant-filed tickets is a standard
operational expectation; the assistant should not gate this behind user
approval.

**SHA256:** `dbf51dec8f614319d77c634ea7616e51dff70445b7b2da4e569d10db3e0a79ca`

## v96 — 2026-08-11 — auto-close-superseded-draft-tickets-4416

**Summary:** Add a "Superseded ticket auto-close" rule to the autonomy gate section.
When the assistant discovers that a draft or open ticket is superseded by another
ticket that is already CLOSED or DONE, it should close the superseded ticket as a
duplicate without waiting for operator confirmation. The superseding ticket's
terminal state is unambiguous evidence that the superseded work is obsolete.

**Rationale:** Previously the assistant would ask for confirmation before closing
superseded tickets (e.g. "I will close ticket 5f1c if you choose Option A"),
requiring an extra operator turn. When the superseding ticket is already terminal,
the superseded ticket is unambiguously obsolete — the extra confirmation turn is
pure overhead that clutters the ticket board with stale drafts.

**SHA256:** `9f6c146cb62ae93f4751f01da9f1e94a0beff74aa0600f065093e9e79534553d`

## v95 — 2026-08-10 — distinguish-between-no-openrouter-api-ke-5aa9

**Summary:** Rewrite the OpenRouter API key guidance in the "Model Policy" bullet
under Subsessions. The agent is now instructed to never claim the key is missing
outright, to retry at level 3 (keyless) on spawn failures, and to recommend the
operator verify `llmio.api_key` in the server's JSON config file. The previous
wording assumed the server could inspect environment variables and secrets, which
it cannot — the server only reads its own config file. The same guidance is
mirrored in `_validate_model_level` (worker.py) and `spawn_subsession_tool`'s
docstring (tools.py).

**Rationale:** The agent repeatedly told users their OpenRouter key was missing
because it wasn't found in env or secrets, but the key was actually set — just in
a location the server does not read (e.g. an external secret store or environment
variable invisible to the mill worker). The agent now communicates its limitations
clearly instead of making definitive claims it cannot verify.  (The ticket's
alternative approach — runtime verification via a minimal LLM call — was not
implemented because `_validate_model_level` is a synchronous pre-flight check that
runs before any LLM handle is available; the chosen approach of clear communication
of limitations is simpler and avoids adding latency to every spawn.)

**SHA256:** `4d9b72f6586ac61474522102dfeb4f207a7ad2d97ab2be1c903ce4859ec76f92`

## v94 — 2026-08-03 — fix-false-token-exhaustion-punts-and-ret-b73c

**Summary:** Strengthen anti-punt instruction in the Efficiency section: removed
"call budget" language (which the agent was conflating with token budget exhaustion),
added "call budget" to the list of forbidden resource-exhaustion claims, and added
explicit "do not defer or punt" directive. Replaced "call budget" wording with
"single turn" scoping to avoid planting the budget concept.

**Rationale:** The agent was repeatedly claiming it was "out of token budget" and
deferring work while still emitting a reply — a self-contradictory fabricated excuse
that eroded operator trust. The existing instruction already forbade "token budget"
claims but the adjacent "call budget" paragraph was being conflated with it.

**SHA256:** `cfdaba03c108a956e06da3b065f31d3140cfc0ec7bbf1dcbc14c3343a77c2b9c`

## v93 — 2026-08-09 — offer-ticket-rescoping-through-the-stand-bd6e

**Summary:** Extend the "Halt and Re-scope" structured prompt with a new step (4)
that instructs the agent to proactively offer to file corrective tickets via the
standard ticket lifecycle, stating the proposed title and lifecycle path
explicitly. Previously the agent described the re-scope but did not volunteer how
it would be filed, forcing the operator to ask follow-up questions.

**Rationale:** When the agent proposed rescoping a non-compliant draft ticket, it
described the re-scope but did not state that it would file the correction via the
standard lifecycle (create → refine → implement) until the operator pressed it.
Making this explicit in the template streamlines operator decisions by eliminating
the extra turn.

**SHA256:** `80b71ed89f65944792ff22809c064febcaf81bb76738b917b0dcb8da33aa4d9a`

## v92 — 2026-08-09 — reduce-operator-interruptions-auto-appro-b90b

**Summary:** Add autonomy tier guidance to the Autonomy section. The new paragraph
describes the `autonomy.auto_approve_self_authored` config with repo allowlist and
`autonomy.suppress_no_change_monitors`, noting the non-negotiable gate list and the
conservative default.

**Rationale:** The operator is interrupted too often for mechanical decisions.
The autonomy tier lets the agent self-approve low-risk, self-authored tickets and
suppress no-change monitor chatter when the operator opts in.

**SHA256:** `c1dadbc92e7cdffc3a8667e41d5588c45f16b8dcaa4186fca52d203ac5fb2662`

## v91 — 2026-08-09 — avoid-unavailable-api-endpoints-check-or-4aa8

**Summary:** Document `resume-blocked` as the standard fallback when
`reset_implement_spawn_counter` returns HTTP 405. Some board builds lack the
`implement_spawn_count` DELETE route, so the reset call cannot succeed there.

**Rationale:** Without a documented fallback the agent retried an endpoint that
cannot exist on those builds, then stalled. Naming `resume-blocked` as the
supported alternative turns a dead end into a one-step recovery.

**SHA256:** `df7f5445661379a6f4873e2c4ad1ebf8eb797eb27b47c7a96424ac9d4662291a`

## v90 — 2026-08-08 — add-positive-token-budget-management-strategy-a867

**Summary:** Add a positive token budget management strategy to the Efficiency section.
The new directive instructs the agent to proactively manage its call budget: before starting
a multi-step investigation, estimate whether the task fits within the available call budget;
when it would exceed it, break the work into smaller bounded sub-tasks that each complete in
a single turn, or propose a simpler one-step diagnostic. This complements the v86 prohibition
on fabricated token-exhaustion claims by giving the agent a constructive alternative.

**Rationale:** The v86 Efficiency directive only prohibited fabricated token-exhaustion claims
but provided no positive strategy for managing call budget. Without a constructive alternative,
the agent lacked guidance on how to handle genuinely large tasks — the new directive fills
that gap by instructing the agent to estimate, decompose, or simplify before committing to a
sprawling investigation.

**SHA256:** `f119e375d16d9913c692d43d4c5fe090c620268d92c45cec9585afe03145074c`

## v89 — 2026-08-02 — auto-prioritize-user-requested-tickets-46db

**Summary:** Add user-requested-ticket guidance to the ticket-filing rules. When the operator
explicitly asks the agent to file a ticket (e.g. "file a ticket for X"), the resulting ticket is
user-requested — it represents the operator's own intent and carries higher priority than auto-filed
chores. User-requested tickets MUST include `kind: user-request` and `priority: high` markers in the
body metadata block (the `--- kind: ...` line). After filing, the agent must immediately transition
the ticket out of draft / `human_issue_approval` to `ready` using the board API — the operator's
request to file constitutes consent for both filing and approval, so the gate is bypassed in the
same turn. Auto-filed chores and feedback tickets (initiated on the agent's own initiative) still go
through the normal approval gate.

**Rationale:** User-requested tickets sat in 'draft' for days because the mill's workflow required
human review while auto-filed chores jumped ahead, delaying the operator's core goals. The new
guidance makes the filing request double as explicit consent, so user-requested tickets skip the
draft/human_issue_approval gate immediately without a separate manual approval cycle.

**SHA256:** `eecc27f51153b2565ade8310c9d49a2f4496a939fe38144c19e493d2b54ef1c6`

## v88 — 2026-08-01 — fix-merge-capability-doc-references-to-u-39ff

**Summary:** Fix two references to the non-existent ``merge_pr`` tool in the
agent_instruction system prompt — both now correctly reference
``merge_direct_repo_pr``, the exported agent tool. The internal client method
name ``merge_pr`` was incorrectly used in the guardrail bullet
(``DirectRepoSettings``) and the system prompt merge/PR guidance.

**Rationale:** PR #1089 updated two agent-facing doc sites but carried over the
internal method name ``merge_pr`` instead of the exported tool name
``merge_direct_repo_pr``. An agent directed to use ``merge_pr`` has no callable
tool by that name.

**SHA256:** `e45f904d8426ec35d18873048776d2355435c3b7dbd63e07135d199149c07641`

## v87 — 2026-08-01 — config-ownership-migration-robotsix-chat-f7dd

**Summary:** Remove references to the deprecated lifecycle config-store tools
(`get_lifecycle_service_config`, `update_lifecycle_service_config`,
`watch_service_redeploy`) from the system prompt. These tools were removed as
part of the config-ownership migration: each component now owns its configuration
internally via its own `/config` endpoints, and the central-deploy config-store
API is no longer used at runtime. The prompt text in the Deploy API section and
the periodic-monitor deploy-preflight section has been updated accordingly.

**Rationale:** The central-deploy runtime config-store (`GET/PUT
/services/{name}/config`) is being decommissioned as each component migrates to
self-owned configuration.  The system prompt must not direct the agent to use
tools that no longer exist.

**SHA256:** `413dc7d2d7b07dafde7d78393b63575f10313c11ef4fb1debb26898d050564c3`

## v86 — 2026-08-05 — fix-false-token-exhaustion-self-punts-4abc

**Summary:** Add a new Efficiency directive prohibiting the assistant from fabricating
claims about running out of "token budget," "response budget," or any other AI-internal
resource limit as a reason for not performing an action. These claims are hallucinations —
the assistant has no such constraint visible to it. When an action cannot be performed,
the assistant must state the specific real reason (missing tool, insufficient permissions,
incomplete information, a genuine API error) rather than a fabricated resource-exhaustion
excuse.

**Rationale:** During a session, the assistant claimed it had "run out of token budget" to
file a ticket — a fabricated excuse with no basis in the actual system constraints. This
false claim caused unnecessary delay and eroded operator trust. The new directive explicitly
forbids such fabricated resource-exhaustion claims and instructs the assistant to either
proceed with the action or state the real constraint.

**SHA256:** `f9db33ca985ae0650369daafa04c1d147d526f192615a18dc9e40e5d14cabbb7`

## v85 — 2026-08-02 — assistant-should-verify-associated-ticke-71d8

**Summary:** Add a proactive "associated tickets" directive to the Autonomy section of the
system prompt. When the user asks to prioritize, group, or surface "associated tickets" (or
similar language about related or grouped work), the assistant must NOT report from memory or
from a single ticket id alone. Instead it must proactively query the full board (GET /tickets)
and filter by subject keywords, repo name, and/or ticket-id prefix to identify ALL open tickets
that may be related before reporting. The directive instructs the assistant to include any
ticket it is unsure about with a brief note of its relevance rather than omitting it.

**Rationale:** When asked to prioritize associated tickets to close the subject quickly, the
assistant initially only considered one ticket (6a4e) and did not surface a related open CI/ruff
debt ticket (af3d) until the user prompted a re-check. The user expects a complete picture from
the first "associated tickets" request — missing a related ticket forces the user to nudge the
assistant to re-check, wasting operator time. The new directive ensures the assistant pulls the
full board and filters by subject/repo before reporting, giving a complete picture without a
nudge.

**SHA256:** `f40683a35058cb8c89d30590c4ee9d8e2613ab365849cfb7c04d872c1c59b74d`

## v84 — 2026-08-02 — assistant-should-consult-notes-before-gu-e792

**Summary:** Add a new Efficiency bullet: when a tool call returns an error —
especially an HTTP endpoint or API route — do not guess alternate endpoints
blindly. First consult knowledge notes for the 'endpoints' topic
(`search_knowledge_notes("endpoints")`) and read relevant reference docs
(`list_reference_docs`, `read_reference_doc`) for the correct route. Only try
an alternate approach when verified from notes or docs. When a correct route
is discovered that was not already in notes, add or update the 'endpoints'
knowledge note immediately so future sessions avoid the same failure.

**Rationale:** The assistant knew a correct priority endpoint from knowledge
notes but attempted several wrong paths first, wasting turns. The new directive
teaches the agent to front-load a notes check before guessing, and to close the
loop by persisting newly discovered routes.

**SHA256:** `9a8a68c9e0274453100d7710069ddaee65e931401c31eeb7fa2915247261b9fd`

## v83 — 2026-08-02 — prevent-duplicate-subsession-creation-fo-6f8a

**Summary:** (1) Add a PRE-SPAWN GUARD directive to the subsessions section:
before spawning any subsession (task, user_chat, or periodic), the agent
MUST call list_subsessions and check for an existing OPEN subsession with
the same purpose or dedup_key. If one already exists, reuse it — do not
spawn a second subsession for the same work. This applies especially to
user_chat subsessions where a single decision queue should have exactly
one user_chat subsession. The dedup_key system-level suppression only
catches exact key matches — list_subsessions is the authoritative guard
against logical duplicates.
(2) Add a directive to preserve factual fidelity when reporting
subsession outcomes: when the summary states a specific cause, reason,
or actor (e.g. "ticket closed by operator", "superseded by ticket X",
"auto-paused after no-change runs"), the assistant must echo that exact
factual claim rather than substituting a vague or inaccurate paraphrase
like "closed itself cleanly" or "finished normally."

**Rationale:** (1) The assistant spawned two user_chat subsessions for the
same operator decision queue despite the plan explicitly stating to open
ONE subsession. The existing "Check list_subsessions before spawning"
hint was too weak — it was a trailing clause on a nesting bullet and
was easy to miss. A standalone, prominent, MUST-level directive with
specific user_chat guidance prevents this recurring risk.
(2) A monitor auto-paused because its target ticket was closed by an
operator ruling and superseded. The assistant paraphrased this as the
monitor "closed itself cleanly" — misleading the user into thinking the
monitor had finished its work normally, when in fact it was terminated
by an external cause. The new directive requires factual accuracy in
outcome reporting: short is fine, but it must be accurate.

**SHA256:** `225b713d16953f05a3381714895bbd18ee0bcfa8b041d1e5e1cad6c4dd12990e`

## v82 — 2026-08-02 — avoid-redundant-status-repetition-on-re-ask-4130

**Summary:** Add a directive for when the user re-asks about monitoring
or tracking status (e.g. "tracking is not there", "any update?").
The agent must directly state the current verified state and next
action without re-listing the full ticket history, repeating lifecycle
steps, or echoing subsession summaries the user has already seen.
If the ticket is in the same state as the last update, confirm in
one sentence and state what happens next.

**Rationale:** The assistant's turn after the user said "tracking is
not there" contained a large redundant repetition of the same ticket/PR
status the user had already seen, only adding peripheral detail. This
wastes user attention and increases confusion. The new directive
explicitly instructs the agent to detect re-asks and respond with a
tight status + action summary.

**SHA256:** `d082cc49032ac9bf295534a29df2a1ef26182d904fa898732067b4699fe171c6`

## v81 — 2026-07-31 — prevent-placeholder-hashes-in-credential-tickets-075a

**Summary:** Add credential-bearing ticket guidance to the agent instruction: when
filing a ticket that involves setting or changing a credential, the spec must include
the exact credential value (never a placeholder). Add credential-verification guidance
before merging PRs that modify stored credentials or password hashes — inspect the diff
for well-known default values and block the merge if found.

**Rationale:** A password-reset ticket filed by the assistant resulted in the implement
agent committing a well-known SHA-1 hash of "password" instead of the user-specified
password. The ticket description lacked the actual password, so the implement agent
defaulted to a placeholder. The new guidance closes this gap at two points: ticket
filing (include the exact value) and merge review (verify the diff has no defaults).

**SHA256:** `50b1fbdcf01fefd27ef2411a93ab15d85d4c834bf104f3663ec6e38005bc5aea`

## v80 — 2026-07-31 — reconcile-direct-repo-merge-capability-docs-074f

**Summary:** Update the direct-repo guardrail text in the system prompt and
``DirectRepoSettings`` docstring to acknowledge the ``merge_direct_repo_pr`` tool
(available for BLOCKED tickets) instead of claiming no merge capability exists on the
direct-repo path. When a PR is approved and mergeable, the agent should prefer
``merge_direct_repo_pr``; for pre-BLOCKED tickets or when unavailable, the mill's
merge endpoint is the fallback.

**Rationale:** The ``merge_direct_repo_pr`` tool was introduced on the direct-repo path
but two guardrail doc sites (the ``DirectRepoSettings`` model docstring and the system
prompt ``agent_instruction``) still claimed "no merge capability exists" — causing the
agent to incorrectly report it cannot merge and route the operator to a less direct path.

**SHA256:** `b7de916c4cb865a4ea1deffd3f1500b6d6cc0e4f39b4e806b459d9ec4dbb6faa`

______________________________________________________________________

## v79 — 2026-07-31 — one-decision-at-a-time-for-user-chat-c54a

**Summary:** Add a "one decision at a time" rule to the user_chat subsession guidance.
When multiple independent decisions are pending, the agent must present them sequentially —
state the first decision with its options, wait for the operator's answer, confirm the
choice, then present the next. Never batch multiple unrelated decisions into a single
message.

**Rationale:** The operator previously received multi-decision lists in a single user_chat
message, which a human cannot process reliably. Sequential presentation with explicit
confirmation after each answer ensures every decision receives full attention, no option
is missed, and the operator can interject follow-up questions without losing the queue.

**SHA256:** `c31f265f076389ec1c3972c86b7c95ea739780cbd69338e0f39d492a41f32618`

______________________________________________________________________

## v78 — 2026-07-31 — explicit-halt-and-re-scope-confirmation-5f87

**Summary:** Add a "Halt and Re-scope" section to the system prompt. When the agent detects
that a user's request would violate an organizational policy, standard, or hard constraint,
it must immediately halt execution and present a structured re-scope prompt: state the
violation in one sentence, offer 2–3 labeled compliant alternatives, include one-click
actions to close any superseded work, and wait for the user's choice before proceeding.
This condenses a 4–5 turn violation-resolution cycle into 1–2 turns.

**Rationale:** Previously the agent would explain the violation and ask an open-ended
"What should I do instead?", triggering a multi-turn back-and-forth to converge on a
compliant alternative and close superseded PRs/tickets. The structured re-scope workflow
bundles the diagnosis, alternatives, and cleanup into one prompt so the user can respond
with a single label.

**SHA256:** `78eef638c1035fd609fee687500a35b5f13f860840f93a346d9dc172d4f6aa04`

______________________________________________________________________

## v77 — 2026-07-31 — truncate-long-pr-lists-or-provide-them-a-bb9c

**Summary:** Add guidance to the Efficiency section directing the assistant to avoid dumping
long sorted lists (20+ PR links, ticket enumerations, file inventories) inline in a single
chat message. When a long list is needed, the assistant should provide a compact summary and
offer the full list as a separate artifact (knowledge note, split across replies, or narrowed
query). Lists under ~25 items may be displayed inline with a warning at the output limit.

**Rationale:** Session 2fd0831 reported that the assistant's reply listing 46 PR links was
cut off mid-list (output length limit), delivering an incomplete answer at a time the user
specifically needed the full list. The new guidance teaches the agent to structure long
enumerations defensively so truncation is either avoided or the user is explicitly told how
to retrieve the remainder.

**SHA256:** `9beaa7109017ff6daa47e515ae60322109551493ea4b9b7f716437bce17673a7`

______________________________________________________________________

## v76 — 2026-07-30 — propagate-operator-consent-through-approval-gates-e038

**Summary:** Add operator consent propagation to the Autonomy section and to the autonomous
protocol's mutation authorization guidance. When the operator provides credentials, explicitly
approves a change, or authorizes a specific operation by name, that consent carries forward to all
sub-operations in the same chain (ticket approval, MR approval, merge confirmation) without
re-asking. The agent treats the original authorization as covering the full lifecycle of the
consented operation, only surfacing a new approval request for genuinely new, unconsented actions.

**Rationale:** Session 9369ddd demonstrated that when an operator specified a temporary password and
asked the assistant to file and deploy a config change, the assistant still separately asked for
approval at the ticket and MR gates — adding latency and confusion. The new guidance teaches the
agent to recognise consent propagation: an operator who says "use this password and file/deploy this
config change" has authorised the complete operation, and intermediate approval gates are redundant.

**SHA256:** `3787571c0ce99ab0b623b134877cf436a5e3ab1d6dc23b9b3a27104147c7f114`

______________________________________________________________________

## v75 — 2026-07-28 — resolve-conflicting-option-a-memory-by-g-f4f2

**Summary:** Strengthen memory-recall guardrails in both the system prompt's Autonomy section and
the per-turn memory header (`_MEMORY_PROMPT_HEADER`) to prevent stale plans and solution options
from past sessions being presented as current proposals. The Autonomy warning now explicitly calls
out stale plans, solution options, and decisions (not just identifiers). The memory header gains a
new "CRITICAL — stale plans and decisions" block that warns: recalled Option A/B/… labels, proposed
plans, deployment strategies, and approval workflows are almost always from a past session with a
different context; a label reused across sessions almost certainly refers to a different proposal;
before presenting any recalled plan or option, verify it appears in the current conversation
history, and if uncertain, label it explicitly as "from memory, may not apply to this session."

**Rationale:** Session 8b03ed2ca8f946629bdee029f2efaaa7 showed the assistant recalling a stale
"Option A" (monitor 63c5, auto-run deploy, auto-approve) from a similar past memory that did not
match the actually proposed Option A (manual PR), causing confusion and requiring explicit
disregard. The existing warnings covered identifiers and action items but not plans/options;
similarity recall can surface semantically similar but contextually different proposals under reused
labels.

**SHA256:** `e181d65690a7d0089cbaec90d34d194facd9ddf9bcc52a227169d1c7809dcba9`

## v74 — 2026-07-30 — ticket-id-fidelity-narrative-derived-597e

**Summary:** Added a new "ticket ID fidelity" bullet to the Subsessions section, immediately
after the "never enumerate raw bullet lists" instruction. The new bullet requires the assistant
to always use exact, stable ticket IDs from board API responses when passing them to tools or API
endpoints, and never to abbreviate, truncate, paraphrase, or reconstruct a ticket ID from
narrative memory or a prior summary. Before calling any API endpoint that transitions a ticket
(merge-now, resume-blocked, etc.), the assistant must resolve the ticket's exact ID from the
board via a live GET /tickets lookup. Also added a validation warning log in the ticket_poll and
worker_mill code paths when a 404 is returned for a ticket ID, noting that the ID may have been
derived from narrative text rather than from a board API response.

**Rationale:** The assistant was constructing API calls using truncated ticket IDs that had been
paraphrased during an earlier review summary (e.g. `...fix-0eff` vs
`...fix-readme-provider-list-to-match-actual-0eff`). All 36 calls returned 404 because the real
IDs were slightly different. The "never enumerate raw bullet lists" instruction was being
over-applied — the assistant was abbreviating ticket IDs in narrative and then feeding those
abbreviated IDs back into API calls.

**SHA256:** `2ebd1174daa19ec599397f4603ba62aac5b642399b54e0312c801860774ef50e`

## v73 — 2026-07-30 — ticket-description-append-does-not-change-fingerprint-ca44

**Summary:** Clarified that the ticket fingerprint guard hashes only the spec text, not the full
ticket description. Added notes in two locations:

1. In the autonomous runner's fingerprint-guard bypass guidance: "the fingerprint guard hashes
   only the spec text, not the full ticket description. Editing the description without changing
   the spec text will NOT clear the guard — to vary the fingerprint you must edit the spec itself."
1. In the periodic monitor section: "the fingerprint hashes only the spec text; editing the
   description without changing the spec will not clear the guard."

**Rationale:** Operators occasionally edit ticket descriptions expecting that to vary the
fingerprint and unblock a guard. This clarification prevents that misunderstanding.

**SHA256:** `c8e48fdeb0fad3f232be99f649013d18e13aaef56f9096268b344ae297d9c477`

______________________________________________________________________

## v72 — 2026-07-29 — fingerprint-guard-auto-resume-working-fix-7181

**Summary:** Extend the auto-resume criteria in the Remediate step (3) of the ticket
lifecycle to include fingerprint-guarded tickets where a working fix already exists despite an
unchanged spec fingerprint — e.g. a PR with passing tests is open but the implement stage cannot
proceed because the spec fingerprint hasn't changed. The assistant can now call resume-blocked with
justification `"spec is complete; working fix exists with passing tests; allow re-implement to merge"` for this case without operator authorization. Also updated the autonomous protocol's
MUTATION AUTHORIZATION section to carve out an exception for the auto-resume cases documented in
the main prompt.

**Rationale:** When a monitor reports a blocked ticket due to unchanged spec fingerprint despite a
working fix already existing, the assistant previously could not clear the block without user
go-ahead — the only auto-resume cases were transient failures and fingerprint-guard with answered
pending question. This adds the third common fingerprint-guard pattern: the spec is already
correct and the fix exists, but the fingerprint guard blocks re-implementation.

**SHA256:** `ce58088fe1088dffa82d2e6e158f63a6d9d15642d23e6bdd6e8fed59c7cbbad7`

______________________________________________________________________

## v71 — 2026-07-28 — merge-handle-blocked-closed-transition-0ce2-c578

**Summary:** Merge of two independent v70-branch changes into v71:

1. **Operator-facing blocker instructions** (v70/0ce2): When surfacing a hard server-side blocker to the operator, the assistant must now provide a concrete, copy-paste-ready instruction — exact env variable name, config file path, restart command, or endpoint URL — rather than a vague directive. It must also store common remediation recipes in a knowledge note (topic: `operator-remediation-recipes`).

1. **Deadlocked ticket closure** (c578): Add a "Deadlocked ticket closure" bullet to the ticket lifecycle remediation guidance. When a ticket is deadlocked — the implement loop keeps cycling without progress and normal close transitions (blocked→closed, ready→closed) are rejected by the mill API — the agent must surface the deadlock to the operator via user_chat with a clear diagnosis. If the operator confirms closure, the agent uses `component_request("mill", "DELETE", "/tickets/{id}")` to remove the deadlocked ticket from the board. Deletion is irreversible — only use it when normal transitions are blocked and the operator has explicitly approved. If the underlying issue still needs attention, the agent should file a superseding ticket with a fresh spec, referencing the deleted predecessor's id.

**Rationale:** Both changes were authored independently against v69, each bumping to v70. The merge combines both into a single v71 prompt.

**SHA256:** `bd34147301da38a69438f1905a8b247969066119dac3be29dffa42f73ee068a1`

______________________________________________________________________

## v69 — 2026-07-28 — avoid-duplicate-ticket-creation-by-check-7b0b

**Summary:** Tighten the ticket-creation deduplication rule in the Initiate step (1) of the ticket
lifecycle. The prior wording checked `list_tickets` for any open or in-flight ticket addressing the
same root cause; the new wording is more explicit: always query the board's ticket list first (by
board, title keywords, or the exact error message) to check whether an open ticket for the same
issue already exists. It adds a specific call-out that the CI system and other periodic agents may
have already auto-filed a ticket — the agent must never create a second ticket for the same root
cause or proposed action.

**Rationale:** The agent was creating duplicate tickets for issues already tracked by CI-auto-filed
tickets, wasting board slots and creating redundant work. The revised language makes the
deduplication check more prescriptive and specifically calls out CI/periodic-agent auto-filing as a
source of pre-existing tickets.

**SHA256:** `e9180114668b01ee6c6e91d826521e8542681d6ab8af1dfc3826485ec01ce72b`

______________________________________________________________________

## v68 — 2026-07-28 — add-retry-with-justification-support-to-6928

**Summary:** Three updates to the `agent_instruction` default to support retry-with-justification
for fingerprint-guarded tickets:

1. **resume-blocked endpoint:** Document the `justification` JSON body parameter so the agent knows
   it can pass a reason to override the fingerprint guard when the spec is unchanged but external
   information (e.g. an answered pending question, a resolved prerequisite) makes re-implementation
   warranted.

1. **Auto-resume remediation:** Extend the remediation rule to include fingerprint-guarded tickets
   where a pending question has been answered — the agent should call `resume-blocked` with
   `justification: "pending question answered; spec is complete; allow re-implement"`.

1. **Periodic subsession monitoring:** Add an exception to the "do not keep polling" rule for
   fingerprint-guarded tickets: if the guard can be bypassed with new external information, the
   agent should call `resume-blocked` with a justification explaining why re-implementation is now
   warranted.

**Rationale:** Fingerprint-guarded tickets that are blocked solely because a pending question
needed answering were previously stuck until the spec itself changed. The `justification` parameter
on `resume-blocked` now allows the agent to unblock these tickets when the question is answered,
reducing unnecessary operator intervention.

**SHA256:** `3aca65c9780285400f51a1853c4c0c70bd272368ee7b53014b27458808e43b40`

______________________________________________________________________

## v67 — 2026-07-28 — consolidate-subsession-summaries-into-a-c7d8

**Summary:** Replace the "consolidate periodic subsession outcomes" bullet in the Subsessions
section with stronger, more explicit language. The new text: (a) explicitly forbids raw enumerations
of subsession outcomes (no bullet lists of `[id] kind=... status=...` lines), (b) requires the agent
to SYNTHESIZE all relevant outcomes into a single cohesive narrative (1-2 paragraphs in natural
language), (c) instructs the agent to omit trivial NO_CHANGE monitors entirely, and (d) when
multiple outcomes need reporting, consolidate them into ONE narrative grouped by theme, not by
subsession id. The `_REACT_PROMPT_TEMPLATE` in `delivery.py` is similarly updated to forbid raw
enumerations and require synthesis over individual notices.

**Rationale:** The prior instruction ("consolidate them into ONE grouped summary") was being
interpreted loosely — the agent still produced plain bullet lists and tool-output-style dumps that
read like debug output rather than a polished assistant reply. The new language is prescriptive and
unambiguous, leaving no room for raw enumeration fallback.

**SHA256:** `d27d30799d75701d7f1dcdd5e507adbb3099921aab868a4f4c51edf091b4931a`

______________________________________________________________________

## v66 — 2026-07-28 — reduce-verbose-re-summarization-in-monit-9774

**Summary:** Add a "compress monitor outcomes" bullet to the Subsessions section. When the assistant
is actively conversing with the user and the user has already been told about a ticket's state in
the prior turn, monitor outcomes must be compressed to only the delta from the last known state
(e.g. "GREEN — publish workflow succeeded, image published"), suppressing stale IDs, timestamps, PR
URLs, and lifecycle chains the user already knows.

**Rationale:** In session 5027d39, monitor outcome messages restated full ticket lifecycles, IDs,
timestamps, and PR URLs even when the user had just been told about those states in the prior turn.
This adds cognitive noise. The new directive ensures deltas are reported concisely during active
conversations, reducing re-summarization overhead.

**SHA256:** `45ac5e9528683f4e5fe62b8c55fd182054f8e5c6b9b4320507e9902cc4060d86`

______________________________________________________________________

## v65 — 2026-07-28 — provide-explicit-guidance-for-handling-s-6120

**Summary:** Add a "Block cascade triage" bullet to the Autonomy section. When a periodic monitor
reports a stabilized cascade (≥10 blocked tickets across ≥2 boards, no change for ≥3 consecutive
runs), the assistant must not bulk-resume; instead, present a categorized failure-mode summary
(grouped by root cause with severity) and ask the operator to choose between per-board triage or
individual-ticket focus.

**Rationale:** In session 8b03ed2, a monitor correctly identified a 41-ticket cascade across 9
boards but the assistant lacked guidance to halt further bulk actions and offer structured triage.
Adding this directive prevents wasteful bulk-resume attempts against systemic, stabilized block
cascades and routes the operator directly to a categorized triage decision.

**SHA256:** `5d4c331db3f3338b25d5330f0306671898a640e67e506bab7c8476229b4a6c40`

## v65-b — 2026-07-28 — add-failure-mode-classification-to-bulk-0839

**Summary:** Add a "Bulk-resume failure-mode classification" bullet to the ticket lifecycle's
Remediate step (3). Before bulk-resuming multiple blocked tickets, the assistant must now query each
ticket's history and comments to infer the failure-mode category (e.g. 'unavailable tools', 'CI
typecheck', 'git checkout failure'). If more than 2 distinct failure modes are detected, abort the
bulk-resume and surface a categorized diagnosis to the operator via a user_chat subsession instead.

**Rationale:** The assistant bulk-resumed 75 blocked tickets without pre-classifying failure modes,
assuming a single implement-stage fix was sufficient. The monitor later revealed 41 re-blocks across
9 distinct root causes. Pre-classifying failure modes before bulk-resume prevents re-block cycles
and wasted implement cycles when a batch spans multiple unrelated root causes.

**SHA256:** `f0882f7e2e093b0dfb94edbd9dfc2948bb43d065fdd06c01666c26a8f6ce38d8`

______________________________________________________________________

## v64 — 2026-07-27 — expose-deploy-image-digest-and-health-st-ae07

**Summary:** Add a "deploy status tracking" bullet to the Subsessions section. When monitoring a
ticket that involves a code change deployed to a component, periodic subsessions must now track
deploy status alongside board status: check the running image digest via
`get_lifecycle_service_config`, confirm rollout completion via `get_lifecycle_service_status`, and
verify component health via `component_request GET /health`. A merged PR whose image is not yet
deployed is not a terminal state — the monitor must stay open until deploy is confirmed live.

**Rationale:** Periodic monitors for tickets like 244c and 90b5 tracked fix merge but not deploy
status, so the assistant later needed live deploy checks to discover the fix was already deployed.
Adding deploy image digest, rollout status, and health to periodic monitoring prevents redundant fix
proposals for issues already resolved in the running image, accelerating deadlock resolution.

**SHA256:** `45e04e8c1aa771858e20cf0dd6fa247d741a659184b5efa91d6fa51bd2e97dd9`

______________________________________________________________________

## v63 — 2026-07-28 — introduce-model-policy-abstraction-for-d-42d5

**Summary:** Add a "Model Policy" section defining named tier labels for the existing model levels
(1 = 'cheap-high-perf', 2 = 'default', 3 = 'strong-reasoning', 4 = 'primary-frontier'). Update the
subsession model_level guidance to cross-reference the tier labels. Instruct the assistant to use
these tier labels (e.g. 'primary-frontier') rather than hardcoded model names when filing tickets
that specify model requirements — agent configurations, tool defaults, deployment specs, subsession
spawning defaults. The resolver at deploy-time maps tier labels to concrete models based on the
current central policy, keeping configurations evergreen without rework.

**Rationale:** The assistant occasionally hardcoded specific model names (e.g. 'GPT current-tier',
'Kimi K2') when creating default agent configuration tickets, causing staleness as frontier models
evolve. The named-tier abstraction decouples ticket specs from concrete models so configurations
stay current without manual rework.

**SHA256:** `54ea4a939c89c287887567d0b1c05c16cf9ad4b16e80b34af18182126689632e`

______________________________________________________________________

## v62 — 2026-07-25 — unify-periodic-sub-session-summaries-int-6dc6

**Summary:** Add a consolidation rule for periodic subsession outcomes. When multiple periodic
subsessions deliver outcomes in quick succession (especially while the user is idle), the agent must
consolidate them into ONE grouped summary, grouping tickets by state (NO_CHANGE, PROGRESS,
GATE_PENDING) and hiding trivial NO_CHANGE runs from duplicate monitor cycles. Also update the
reaction prompt template (`_REACT_PROMPT_TEMPLATE` in `delivery.py`) to instruct the agent to
consolidate when it has recently reported other periodic outcomes.

**Rationale:** Multiple periodic monitors running concurrently (e.g. monitors for cbe3, aaa6, ccfd)
were each producing separate outcome notices. The assistant would manually merge them, but the user
still saw a long list of fragmented individual notices before consolidation. The new rule teaches
the agent to batch outcomes proactively, reducing noise when the user is not actively conversing.

**SHA256:** `cc09561088cbb302d4a4dd8a37d4089d063272d12cf720dcd0002ca3ebbbd01f`

______________________________________________________________________

## v61 — 2026-07-24 — recalled-memory-hallucination-flagged-bu-67ab

**Summary:** Remove the standalone bullet "When the user directly challenges a claim about external
state" from the Knowledge notes guidance. The re-verify instruction that followed it remains in
place as a direct continuation of the preceding paragraph: the agent must always re-verify against
the live system immediately when a memory-based assertion conflicts with user-reported observable
evidence — not only when the user explicitly challenges a claim. This closes a gap where recalled
memory hallucinations went unchecked because the triggering condition (an explicit user challenge)
did not match the surface pattern of the hallucination.

**Rationale:** The bullet created a narrow trigger — "when the user directly challenges a claim" —
that left recalled-memory fabrications unchecked when the user did not frame their response as an
explicit challenge. By removing the conditional bullet, the re-verify instruction applies
unconditionally: any time the agent's memory-based assertion contradicts observable reality, it must
re-verify.

**SHA256:** `043607eb68fa273c3de6a8c9529b1be5ec1c88063123ad07f1481713e4025cae`

______________________________________________________________________

## v60 — 2026-07-21 — add-automatic-pr-merge-verification-befo-2329

**Summary:** Add a "Deploy pre-check" bullet to the Deploy system guidance in the Autonomy section.
When a user requests deployment after a migration or fix ticket is marked done, the agent must first
verify the associated PR is merged — query its status via the mill's ticket endpoint or check the PR
on GitHub directly, rather than asking the user for confirmation. If the PR is not yet merged, the
agent must explain the blocker clearly and offer to wait or escalate. Only after confirming the
merge is complete should the agent proceed with the deploy (restart or watch_service_redeploy).

**Rationale:** The agent was asking the user for manual confirmation before proceeding with
deployment, creating unnecessary friction. The user expects the agent to automatically verify PR
merge status and only proceed when safe. The new instruction closes this gap by making automated
merge verification a required pre-deploy step.

**SHA256:** `a5bba5bbcdf3db34f1f0db3213c34081d37b1cb094755e7da93083b9e7ad42d5`

______________________________________________________________________

## v59 — 2026-07-27 — do-not-ask-for-permission-for-trivial-cl-70b7

**Summary:** Add an "explicit instruction override" bullet to the Verification section. When a user
gives an explicit, firm instruction (e.g. "close the superseded ticket without asking", "do X and
don't ask for confirmation"), the assistant must carry it out literally without requesting
additional confirmation. An explicit instruction overrides the default ask-before-acting gate — the
assistant executes and reports the result.

**Rationale:** The default ask-before-acting gate is a safety net, but when the user explicitly
directs action, re-asking for confirmation is redundant friction. This change makes the system
respect explicit user directives without weakening safety for ambiguous cases.

**SHA256:** `ab8e8246f0e2ec6d64e0a4008f4aac061a4ee7cafb4387a12d2899e4c950e548`

______________________________________________________________________

## v59-b — 2026-07-27 — validate-proposed-solutions-against-live-7756

**Summary:** Add a mandatory live-deploy-state pre-check to the "Hand-authoring PRs as a
mill-failure escape hatch" section. Before proposing any mill-targeting fix (hand-authored PR,
ticket, or rework), the assistant must first verify the live mill deploy state: use the deploy API
to check the running image digest and commit on the mill service, then check the mill repo's
recently merged PRs to confirm the defect has not already been fixed in a deploy that occurred since
the assistant last checked. A defect observed hours ago — or that surfaced in recalled memory or a
periodic-note summary — may already be resolved; building a fix on outdated live-state assumptions
wastes implementation effort and delays actual remediation.

**Rationale:** Session 8b03ed2ca8f946629bdee029f2efaaa7 showed the assistant proposing a manual PR
to fix a fleet-wide implement-stage bug without first verifying the current deploy state. Live
checks later revealed the fix was already merged on mill main. The assistant built a plan on
recalled/periodic notes without cross-referencing live deploy status, leading to a delayed
correction.

**SHA256:** `d9b1351b2e4123fe4c7f81976b436e9f45f153b54ba089a175115d447d7f21f3`

## v58 — 2026-07-27 — guidance-for-hand-authoring-prs-as-escap-1b2e

**Summary:** Add a "Hand-authoring PRs as a mill-failure escape hatch" bullet to the Mill & Deploy
Endpoints section. When the assistant identifies a fleet-wide mill defect blocking ≥5 critical
self-improvement tickets across ≥2 repos, it may propose hand-authoring a PR against the mill repo
as an extraordinary escape hatch. The new guidance defines qualifying criteria (systemic failure,
mill-repo target, no existing PR/branch), mandatory pre-checks (verify no open PR, unique branch
name, minimal scope), and a structured escalation path: propose to the operator via user_chat with a
three-option choice; if the operator does not respond within the subsession's idle window, the
proposal expires — do not proceed unilaterally and do not re-propose. Instead, file a prompt ticket
documenting the blocked batch and move on.

**Rationale:** Session 8b03ed2ca8f946629bdee029f2efaaa7 showed the assistant correctly identifying a
fleet-wide mill defect but stalling because no prompt guidance defined when hand-authoring is
permitted, what safety checks to perform, or what to do if the operator does not respond. This adds
that missing guardrail.

**SHA256:** `9670765d57359bdd2d805598daeade88947310b77b87070dadbd8cb24002f3b3`

## v57 — 2026-07-26 — handle-conflicting-user-instructions-gra-dde5

**Summary:** Add a "Conflict Resolution" section to the system prompt. When a user gives an
instruction that conflicts with an existing pending ticket, the assistant must now automatically
attempt to resolve the conflict rather than simply flagging it and waiting for manual intervention.

The new section prescribes a five-step resolution workflow: (1) read the existing ticket's full
spec; (2) determine whether the new instruction is compatible or fundamentally incompatible; (3) if
compatible, merge the new instruction into the ticket — either by updating the ticket spec through
the mill API or, when no update endpoint is available, by closing the old ticket and filing a
replacement with the merged spec; (4) if incompatible, present a structured choice to the user
summarising both instructions, explaining the conflict, and asking which should take priority
(defaulting to the most recent instruction); (5) report the resolution in one sentence.

Additional guidance ensures the assistant preserves the existing ticket's context when merging and
does not discard the original scope unless the user explicitly asks to replace it.

**SHA256:** `3ffc9dca71b645d586ed2e11951c53a119124d9694d629a0625828912432249d`

## v56 — 2026-07-26 — improve-accuracy-of-interpreting-user-re-86e2

**Summary:** Add an "Ambiguous field references" bullet to the Verification section. When a user
describes a desired change to a form field, UI element, or displayed value, the assistant must
confirm the specific field(s) the user is referring to before filing a ticket — do not assume which
field they mean, as a form or page may contain multiple similar fields (date pickers, timestamps,
select dropdowns, formatted displays). Before filing, restate the field's label, location, and
current vs. desired format. If multiple fields could match, list them explicitly and ask the user to
confirm which one(s) to change.

**Rationale:** The user asked for "Date et heure fields to be displayed in a french format
19/02/2027 17:00 instead of 02/19/2027 05:00 PM" and the assistant filed a ticket targeting the
"créneau horaire" select field — not what the user wanted. This misunderstanding led to unnecessary
work and required a follow-up correction. The new rule ensures the assistant verifies the specific
target field(s) before creating tickets for UI formatting changes.

**SHA256:** `4bd5ab4250ce43ac588970ee4faa9ebe83bccdd6868dac304cae36a227e7455d`

## v56-b — 2026-07-26 — require-live-endpoint-verification-before-closing-monitor-754d

**Summary:** Add live-verification requirements to the ticket lifecycle policy so monitors do not
close prematurely when a ticket reaches "done/closed" on the board but the change is not yet live.

1. **Step 1 (Initiate):** New guidance requires ticket specs to include acceptance criteria that
   verify the change is live and working — e.g. "the endpoint returns 2xx" — not just "PR merged".

1. **Step 4 (Complete):** The monitor must now probe the change directly with `component_request`
   before closing. If a server-side capability (endpoint, config flag, behaviour) does not respond
   as expected — e.g. the endpoint returns 403 because a feature flag is still off — the ticket was
   closed prematurely. The monitor should either reopen the ticket or file a follow-up. Only close
   the monitor after live verification succeeds.

**Rationale:** Ticket 20260725T105809Z-enable-chat-agent-component-registration-03d7 was closed as
soon as PR #607 merged, but the registration endpoint still returned 403 ("Chat agent registration
is not enabled on this server."). The monitor treated board-closed as terminal without verifying the
feature was actually live.

**SHA256:** `d965f15a4a7f5a53ac0cf97e5efe93a0ea467607fbdc9a0f20777fe23dd70acb`

______________________________________________________________________

## v55 — 2026-07-25 — periodic-subsession-spawning-restriction-7981

**Summary:** Add a new bullet to the Subsessions section instructing periodic subsessions on how to
handle "periodic subsessions cannot spawn" errors. When a periodic subsession attempts
spawn_subsession and receives this error, it must not present options to the user or ask how to
proceed — it is a hard code-level restriction, not a transient failure. The subsession should fall
back immediately to performing the work inline in the current reply, spreading across multiple
cycles with NO_CHANGE replies if needed. The user should never see the error or be asked to choose a
recovery path.

**Rationale:** Periodic subsessions were surfacing spawning-restriction errors to users and offering
recovery options, but this is a hard restriction that cannot be bypassed. The new guidance provides
a clear fallback path and keeps the error hidden from the user.

**SHA256:** `7e7b00fb717a05d749d2ff38259914fd6e15540cbc826f9ca7618b4be7acd521`

______________________________________________________________________

## v54 — 2026-07-25 — verify-config-before-advising-4895

**Summary:** Add a rule requiring the agent to retrieve and analyse relevant source code before
advising on component configuration settings, to avoid incorrect guidance based on assumptions or
outdated memory.

1. **Verification section:** New bullet instructs the assistant to first retrieve and analyse the
   relevant source code through available tools before giving configuration advice (secrets, labels,
   environment variables, deploy contracts, feature flags). Central infrastructure may already
   handle the setting fleet-wide (e.g. central-deploy's `docker_sdk.py` may inject secrets and
   labels automatically), making per-repo configuration advice redundant or incorrect. Verify the
   source of truth before giving configuration guidance.

**Rationale:** The agent initially advised setting a per-repo `GHCR_TOKEN` on the robotsix-invest
component, but central-deploy already uses a fleet-wide `ghcr_pull_token` and the per-repo label was
a dead no-op. This incorrect guidance added confusion and required additional cleanup tickets.

**SHA256:** `ab5974d257cc18a871cf77ce6cc9c2df992dd3ac49c6f28e4b5d436165f5d852`

______________________________________________________________________

## v53 — 2026-07-25 — avoid-fabricating-causes-without-validating-0cb1

**Summary:** Add troubleshooting instruction to fetch live system state before hypothesizing causes
when the user reports a specific error.

1. **Efficiency section:** New bullet instructs the assistant to first fetch relevant live system
   state (deploy contract, service registry, logs, health endpoints) before proposing causes for
   user-reported errors. Do not propose speculative failure modes (volume-name collisions, port
   conflicts) without checking actual system configuration first — this prevents fabricated guesses
   that waste back-and-forth and erode trust.

**Rationale:** The assistant proposed volume-name collision and port-conflict theories for an
auto-mail onboarding failure without first checking the deploy contract, service registry, or logs.
The user's prompts exposed these as fabricated guesses.

**SHA256:** `2b4d0254251d74796966a338e034e198d8d8dae1621c05bb925d9647af894a56`

______________________________________________________________________

## v52 — 2026-07-25 — require-live-notes-and-board-before-planning-c451

**Summary:** Add mandatory pre-planning step to load actual knowledge notes and live board state,
and strengthen warnings that recalled session memories may be stale.

1. **Autonomous plan drafting (`autonomous/prompts.py`):** Step 2 (PLAN DRAFTING) now requires
   loading knowledge notes and live board state via the mill API *before* drafting the plan.
   Recalled session memories are explicitly called out as similarity-based, potentially stale, and
   likely to reference phantom identifiers.

1. **Agent instruction opening:** "consult it at the start of every session" → "consult it at the
   start of every session and before drafting any plan or taking substantive action".

1. **Autonomy section:** Added a mandatory pre-action bullet: load live board state via
   `GET /tickets` and knowledge notes before drafting any plan. Recalled session memories are a
   fallible cache — verify live state first.

1. **Verification section (Cognee recall):** Strengthened the stale-memory warning — explicitly
   calls out phantom identifiers (wrong repo owners, non-existent ticket ids, closed items
   remembered as open) and requires cross-checking against both knowledge notes and board state, not
   just the live API.

**Rationale:** The assistant opened a plan based on recalled notes that included a wrong repo owner
and phantom approval-queue ticket ids that did not exist on the board. It then spent several turns
fetching live state to correct these misconceptions. A mandatory notes + board load before planning
eliminates this entire class of error.

**SHA256:** `3ef0fc15c18fd655521d840603a68c03f6769c9f6a7f6c64e1d7add0bab5298e`

______________________________________________________________________

## v51 — 2026-07-25 — remove-or-flag-self-authored-knowledge-n-13b9

**Summary:** Add two guardrails against self-authored behavioral rules in knowledge notes:

1. **Knowledge note scope (agent_instruction opening):** Explicitly state that knowledge notes store
   operational facts and findings, not behavioral rules or restrictions. Never write a note encoding
   a restriction like "never use X", "avoid Y", or "do not spawn Z" — behavioral rules belong in the
   system prompt, not in knowledge notes.

1. **Verification bullet (knowledge note rule contradictions):** When a recalled knowledge note
   appears to prohibit an action the system prompt explicitly permits (e.g. "never use
   subsessions"), trust the system prompt — it is the higher-authority directive. Retire
   contradicting notes with `update_knowledge_note`.

**Rationale:** The agent wrote a knowledge note encoding a behavioral rule ("no subsessions") that
conflicted with explicit system prompt instructions about subsession use. The agent then relied on
this self-authored rule over the system prompt, causing user-visible confusion and requiring a
mid-session correction. These guardrails establish a clear hierarchy: system prompt > user
instructions > knowledge notes, and restrict knowledge notes to factual content rather than
behavioral policy.

**SHA256:** `7ef377e5b7f89c26fc84ea0bcc40a209fdd16500e854a52c98ebb4307c718680`

______________________________________________________________________

## v50 — 2026-07-25 — periodic-subsessions-spawned-from-conversation-288d

**Summary:** Clarify periodic subsession role to suppress misleading "not supported" warning.
Changed "Perform all monitoring, polling, and checking inline in your reply" to third-person "they
perform all monitoring, polling, and checking inline in their own replies" — the prior wording was
ambiguous when read by a periodic subsession agent (which receives the same system prompt), causing
it to misinterpret "your reply" as the main conversation's reply and conclude that being spawned
directly from a conversation was unsupported. Added an explicit note that being spawned as a
periodic monitor directly from a conversation is fully supported and the preferred way to launch a
ticket monitor.

**SHA256:** `3a14bb18e6bdb7ba4a5ba7316a6c6ffb9b6be0a7f604ffa9feaa5a22e704e325`

______________________________________________________________________

## v49 — 2026-07-25 — smarter-subsession-reporting-only-surfac-8e33

**Summary:** Bake the subsession reporting contract into the system prompt. Add a new bullet
("Subsession reporting contract") that states subsessions only communicate through
`complete_subsession` — intermediate progress stays inside the subsession and is never delivered.
Update the `complete_subsession` guidance to also cover escalation (blocker, decision needed). Make
the periodic subsystem prompt (worker.py) similarly explicit: the first paragraph of every periodic
turn now states the reporting contract before any other instructions.

**Rationale:** Intermediate periodic run results were burning main-session tokens on non-actionable
noise. Enforcing the contract in both the system prompt and the per-turn periodic input ensures the
LLM knows only terminal summaries and escalations reach the parent, regardless of how the parent
agent embeds spawn instructions.

**SHA256:** `e451c9c0cbf2baf56ff1b43644ec2d7c0e26e4a5f7c8b6e7caefecee2523c780`

## v48 — 2026-07-25 — reduce-verbose-status-messages-to-essent-c3b8

**Summary:** Tighten status-reporting conciseness rules in two sections. (1) In the periodic
subsession terminal-state notification bullet: replace the example sentence with a more abstract
pair ("Ticket approved and merged." / "The site is now verified broken.") to avoid the model
parroting the exact example, and add a bullet instructing the assistant to suppress internal
tracking details (monitor IDs, subsession codes, pipeline job numbers, run counts, model tiers) when
reporting status — unless the user explicitly asks for them. (2) In the pause/resume restart notice
section: add a final paragraph requiring the assistant to only announce key state changes (ticket
approved, PR merged, site verified broken, deploy completed, config updated) with a clear call to
action, and to suppress intermediate pipeline progress, unchanged polling results, routine heartbeat
checks, and background task start/stop events.

**Rationale:** The assistant was emitting verbose status messages full of internal tracking
identifiers and intermediate pipeline progress that added noise without actionable value. The
tightened rules reduce chattiness and focus the user on what changed and what to do next.

**SHA256:** `969a268b8d2c629330828a4886e51e343470703106fff6fcd2cc5bca366494d2`

## v47 — 2026-07-25 — retire-stale-recalled-memory-entries-to-8972

**Summary:** Add "Cognee recall retirement" guidance to the system prompt. When a monitor reports
terminal state on a ticket (CLOSED/DONE), the agent should retire stale knowledge notes that
reference obsolete PR numbers, monitor ids, or closed-fix paths, replacing them with fresh entries
reflecting the current active path. Before citing a recalled-memory claim about a ticket or PR, the
agent must check knowledge notes for a retirement entry — if a note explicitly retires the recalled
detail, trust the note and cite the current state instead.

**Rationale:** Cognee recall frequently returns stale entries referencing obsolete PR numbers,
monitor ids misremembered as ticket ids, and closed-fix paths that are no longer active. The agent
was repeating these obsolete references across conversations, prolonging user confusion. This
guidance teaches the agent to retire stale entries proactively and to cross-check recalled claims
against retirement notes before citing them.

**SHA256:** `12ab468991b4b279450f8f45b6875bd8eb99d21403dce1a65d433781e80c63e9`

## v46 — 2026-07-24 — periodic-monitor-spawns-nested-child-sub-c9c4

**Summary:** Tighten periodic subsession child-spawn rule: remove the "if a one-off check beyond
your regular cycle is needed" escape clause and replace "must NOT" with "cannot" to make the
prohibition absolute. The worker already enforces `spawn_level` restrictions that prevent periodic
parents from spawning children at the code level; this update aligns the system prompt with the code
reality by stating the rule as a hard constraint rather than a guideline with exceptions.

**Rationale:** The periodic monitor for ticket e98b spawned a nested child subsession despite the
existing guidance, exploiting the "one-off check" loophole in the instruction text. The code-level
`spawn_level` guard already blocks this path; this edit makes the prompt match the actual enforced
behavior so the LLM stops trying to spawn children from periodic subsessions.

**SHA256:** `e352f343aa6b97a38f6ebe92bee37ab1eb6bb2896fef08bfd9bc27fb609a4196`

## v46-b — 2026-07-23 — prevent-duplicate-subsession-creation-wh-de78

**Summary:** Add two subsession deduplication rules. (1) A periodic subsession must NOT spawn task
subsessions to perform its own monitoring work — the periodic subsession's instructions execute on
every cycle, so monitoring, polling, and checking should be done directly in the reply rather than
delegating to a child task. Spawning a child task to check the same ticket the periodic parent is
already monitoring is redundant and wastes system resources. (2) When spawning any subsession, check
list_subsessions for an existing periodic monitor for the same ticket or subject — if one exists, do
not spawn a task subsession for it; the periodic monitor will report changes.

**Rationale:** During session 8b03ed2ca8f946629bdee029f2efaaa7, the periodic monitor for ticket e98b
spawned an extra task subsession that checked the same ticket state, producing redundant output and
wasting system resources. These two rules close the gap: the first prevents the periodic subsession
from offloading its own work to a child, and the second prevents a parent agent from bypassing an
existing periodic monitor by spawning a one-shot task.

**SHA256:** `237cb86b37b138470a13383ac3859ebcb7c4c2db315463045e5a0fbee27361a3`

## v46-c — 2026-07-22 — prevent-child-launch-tasks-for-periodic-monitors-24b0

**Summary:** Add a bullet instructing the assistant to spawn periodic monitors directly from its own
context rather than creating a child task subsession whose only job is to call
`spawn_subsession(kind='periodic', ...)`. A task that exists solely to launch a monitor wastes a
model round-trip and duplicates spawning logic the assistant already owns.

**Rationale:** The assistant launched a periodic monitor and then created a child background task
whose sole job was to launch the same monitor — the child task was redundant and was correctly
identified as such. This guidance prevents that class of waste at the source.

**SHA256:** `21a47f67fd08bd2df25880c99c66a5e5189992f5e3beba9109dc006302ed948b`

## v46-d — 2026-07-22 — improve-terminal-state-notification-conc-70aa

**Summary:** Add a conciseness rule for periodic subsession terminal-state notifications. When a
periodic subsession reaches a verified terminal state and delivers its summary to the main
conversation, the assistant must report the outcome in ONE sentence rather than echoing the
subsession's full run history, listing every status transition, or restating the summary text
verbatim. The summary widget already shows the detail — the assistant's job is to confirm the
conclusion and move on.

**Rationale:** The assistant frequently echoes the entire subsession state history (long lists of
run summaries, repeated statuses) when a periodic monitor closes, even though the user has already
seen every intermediate notification and the summary widget is visible. This redundant output adds
cognitive load. The new rule teaches the assistant to treat terminal-state delivery as a one-line
confirmation, not a recap opportunity.

**SHA256:** `b0eb495d432cbaabd2873e705ba240edf19f8b6f692cf87c3c169c6784e95fa9`

## v46-e — 2026-07-22 — add-guidance-to-system-prompt-for-handli-8e03

**Summary:** Add a "Repo creation bootstrap" paragraph to the Autonomy section. When creating a new
repository or working with a freshly created empty repo, tool-chains that require an existing commit
or branch to push to (e.g. push_direct_repo_branch, open_direct_repo_pr) will deadlock if the repo
has no commits. The assistant must proactively seed an initial commit during repo creation (a
README.md, .gitignore, or minimal template file) so that subsequent tool-chains have a branch and
commit to target.

**Rationale:** The assistant encountered a structural deadlock where a tool needed to push the first
commit to a newly created empty repo but had no tool that could create an initial commit (only push
to an existing branch). The assistant responded by repeatedly asking the user to manually initialize
the repo rather than proposing a tool-based workaround. The new rule enforces that repo creation
must always include seeding an initial commit to prevent this class of bootstrap deadlock.

**SHA256:** `fba320e778a0a6a8d9399334b6f10dc5a8c07901822ca36d1f0cd097fb8b9cdb`

## v46-f — 2026-07-22 — add-cross-session-persistent-knowledge-r-b5bb

**Summary:** Add `search_knowledge_notes` to the knowledge-base tool list in the system prompt. The
knowledge store now exposes a search tool that finds notes by querying their topic and content
(case-insensitive substring match), ranked by relevance. This lets the agent retrieve prior
diagnostic notes, deployment statuses, and other key facts without needing to recall exact note ids.

**Rationale:** The assistant wasted time re-discovering that empty-diff bug fixes were already
merged because it could not reliably retrieve prior diagnostic notes — note ids were truncated or
missing from its context. The new search capability eliminates the fragile-id-recall dependency.

**SHA256:** `b0e205017f02e8e2a90707f2b6fbaf51f356e5ab7362124803eb79602ba13050` (mill: Prevent
periodic monitors from spawning redundant child monitor-launch tasks
(20260722T135418Z-prevent-periodic-monitors-from-spawning-24b0))

## v46-g — 2026-07-23 — introduce-model-policy-abstraction-for-d-42d5

**Summary:** Add a "Model Policy" section defining named tier labels for the existing model levels
(1 = 'cheap-high-perf', 2 = 'default', 3 = 'strong-reasoning', 4 = 'primary-frontier'). Update the
subsession model_level guidance to cross-reference the tier labels. Instruct the assistant to use
these tier labels (e.g. 'primary-frontier') rather than hardcoded model names when filing tickets
that specify model requirements — agent configurations, tool defaults, deployment specs, subsession
spawning defaults. The resolver at deploy-time maps tier labels to concrete models based on the
current central policy, keeping configurations evergreen without rework.

**Rationale:** The assistant occasionally hardcoded specific model names (e.g. 'GPT current-tier',
'Kimi K2') when creating default agent configuration tickets, causing staleness as frontier models
evolve. The named-tier abstraction decouples ticket specs from concrete models so configurations
stay current without manual rework.

**SHA256:** `c27c3b532a4338aaa51d9a0a943a81538a60ff94e487209e0925ace0a59669df`

______________________________________________________________________

## v45 — 2026-07-22 — hallucinated-memory-summary-causes-redun-f44a

**Summary:** Add a cognee memory recall verification bullet to the Verification section. Cognee
memory recall (the "Relevant memory from earlier conversations" block prepended to each turn) is
similarity-based and can produce stale, incomplete, or fabricated claims. When a recalled-memory
assertion makes a concrete claim about external state (queue sizes, ticket counts, deployment
status, configuration values, etc.), the assistant must cross-check it against the live API before
acting on it — never treat a recalled-memory assertion as authoritative without verification.

**Rationale:** The assistant fabricated a memory summary stating the human_issue_approval queue was
drained, but upon verifying against the API, found it actually had 25 tickets. This hallucination
wasted time and could lead to incorrect actions if unchecked. The new rule enforces that recalled
memory is treated as a hint requiring verification, not as ground truth.

**SHA256:** `00d9b5881eae6c49dd793826344f2d11b3d42edac990d08babf0d19b23c361ce`

______________________________________________________________________

## v44 — 2026-07-21 — do-not-assume-a-generic-one-shot-deploy-45a0

**Summary:** Add a "Deploy preflight" paragraph after the "Deploy system" section. Before calling
any deploy endpoint (POST /chat/deploy, POST /onboard/\*, lifecycle mutations), the assistant must:
(1) retrieve the target repo's deploy/docker-compose.yml and count services, volumes, healthchecks,
and commands; (2) check the chat_agent_deployable_components allowlist via the roster or
central-deploy and refuse if the component is not listed; (3) compare the contract against the
endpoint's known capabilities and refuse if the endpoint cannot reproduce the full multi-service
contract. The assistant must never offer to deploy through an endpoint whose capabilities are
unverified.

**Rationale:** The assistant twice attempted to deploy robotsix-auto-mail via POST /chat/deploy
without verifying that the endpoint could handle multi-service components, and without checking
whether the server was running the latest code. This preflight gate prevents the pattern of guessing
at deploy endpoint capabilities and forces explicit contract verification before every deploy
attempt.

**SHA256:** `42ae1073840159a89621a4d53ee009d9e69d2fc53449d653d546801370e1d5c4`

## v44-b — 2026-07-21 — always-verify-server-side-capability-by-2bbd

**Summary:** Add a "Server-side capability probes" bullet to the Verification section. When checking
whether a new server-side capability (e.g. POST /chat/deploy) is available, the agent must probe the
target server's endpoint directly with a GET request rather than relying on static skill
descriptions, roster entries, or the audit log. A catch-all 303 redirect from an old build does NOT
confirm the capability is present — only a meaningful status code (405, 422, etc.) from the endpoint
itself indicates the route exists. Before concluding a capability is live, the agent must check the
server's running image digest against the expected digest from the merged PR that introduced the
capability and report the digest comparison to the user.

**Rationale:** In a recent session the agent interpreted a 303 (old build catch-all) as "route
works" and later a 422 (genuine schema validation) from the corrected server as "live", but could
not distinguish the two without knowing the running digest. This guidance ensures the agent always
verifies server-side capabilities against live endpoint behavior and image digests before reporting
them as present.

**SHA256:** `7a3ca453fef6874ea3ac58acf999f3580a2673524b9fb7a0f2d46787b6434418`

## v43 — 2026-07-21 — ensure-ticket-analysis-by-worker-reads-a-3f31

**Summary:** Add a verification bullet requiring the agent to read relevant source files (gate
functions, permission checks, compose labels, deploy contracts) before filing tickets that involve
authorization or configuration changes. The agent must verify current behavior through available
tools and include accurate context in the ticket spec rather than filing based on assumptions.
Superficial changes (docstring-only edits, label additions without logic changes) are explicitly
called out as wasteful.

**Rationale:** Two tickets filed during a session failed to fix the underlying issue: one PR only
updated a docstring and added a test, leaving the logic unchanged; another label- addition ticket
required a container recreate the implement agent couldn't perform. The implement agent didn't read
the actual authorization code or compose labels before closing as implement_complete. This guidance
ensures the chat agent includes verified context in ticket specs so the implement agent has accurate
information to work from.

**SHA256:** `f70ca3f5db3176cacba351f45054037b14a790f05b84f547990cdaa5f786b7e6`

______________________________________________________________________

## v42 — 2026-07-21 — add-prompt-guidance-for-self-mutation-bo-0461

**Summary:** Add a "Self-mutation bootstrap" bullet to the Autonomy / ticket-lifecycle section
(after the Reload step). When a configuration change granting a new capability (permission toggle,
service-update flag, self-restart permission) only takes effect after a service recreate that the
agent cannot perform (because the flag is not yet active), the agent must recognize the
chicken-and-egg problem, explain it to the user, and propose a single one-time operator action
(e.g., an external trigger of POST /chat/services/chat/update) rather than filing tickets for code
fixes that already exist.

**Rationale:** The agent filed a ticket for a self-mutation code fix when the underlying permission
flag was already correct — it just required a one-time external restart to take effect. This
guidance prevents resolution loops by teaching the agent to recognize bootstrap limitations and
direct the operator to the one-time action that breaks the loop.

**SHA256:** `3a5f2afe0de0c7655fd83baeea3828a5bb1eb3601c70283ef10efa8560e0a8f2`

______________________________________________________________________

## v41 — 2026-07-21 — fix-guard-paragraph-contradicts-network-tools

**Summary:** Reword the closing guard paragraph to clarify that the agent **can** access external
systems and the network through its explicit tools, rather than asserting it has no ability to
access the host system or its network at all. The old wording contradicted the growing set of
network-access tools (http_probe, component_request, lifecycle mutation tools, direct-repo tools,
mill board API). The new wording reserves the restriction for unmediated access (shell commands,
host filesystem reads/writes, direct web browsing) and directs the agent to use its provided tools
for external access.

**Rationale:** The guard paragraph was written when the agent had fewer network-access tools. Since
then, the tool surface has grown substantially (lifecycle mutation tools, http_probe, direct-repo
tools, mill merge endpoints), and the flat denial of network access could confuse the model into
refusing to use those tools. The revision separates "no inherent/implicit capabilities" from "can
access through explicit tools."

**SHA256:** `ab6c9fa4d073f0947fe38858f492a54a278f6a4b773918a23f5f04c3335b8e1c`

## v40 — 2026-07-21 — do-not-ask-for-permission-for-trivial-cl-70b7

**Summary:** Add an explicit-instruction rule to the Autonomy section: when a user gives a clear,
firm instruction (e.g. "close the superseded ticket without asking", "do X and don't ask for
confirmation"), the agent must carry it out literally without requesting additional confirmation. An
explicit instruction overrides the default ask-before-acting gate.

**Rationale:** After the user said "yes please close supersede (or delete) without asking," the
agent later asked "want me to close it?" about a superseded ticket. The agent must follow
instructions exactly as given, especially when they are clear and firm.

**SHA256:** `02f4d83677e7e8a0721c7fa7ab0ed9649fef35d6c3ee26e67dac122bfb832384`

## v40-b — 2026-07-21 — incorporate-user-statements-as-ground-truth-86d1 / avoid-filing-tickets-for-issues-that-do-6fe3

**Summary (user statements as ground truth):** Add a "user statements as ground truth" bullet to the
Verification section. When the user states a concrete fact (e.g. "the secrets have been provided"),
the agent must treat the user's statement as ground truth and must not contradict it based on tool
output, logs, or recollection. Instead, the agent must raise a targeted clarification question to
reconcile any apparent discrepancy, then proceed with the user's account.

**Rationale:** The agent repeatedly claimed that OVH_SFTP\_\* secrets were missing after the user
stated they had been provided. The agent was contradicting the user based on inferred evidence,
wasting time and eroding trust. This new rule makes explicit that user statements of fact carry more
weight than agent-side evidence (which may be stale, scoped differently, or misinterpreted), and
that the correct response to contradiction is clarification, not assertion.

**Summary (deduplication check):** Strengthen the Initiate step's deduplication check in the ticket
lifecycle. The old guidance only caught duplicates with the "same scope"; the new guidance also
catches tickets that address the same root cause or propose similar actions, even when worded
differently or approaching the problem from a different angle (e.g. a symptom workaround vs. an
underlying root-cause fix). The agent must now scan open/in-flight tickets for any that share a root
cause, not just identical scope.

**Rationale:** The agent filed a workaround ticket (trivial commit to trigger a redeploy) while a
root-cause fix (missing env mapping in deploy.yml) was already in flight. The old dedup rule only
blocked same-scope duplicates and missed this because the tickets had different stated scopes. The
broader check prevents symptom-vs.-cause duplicate filing.

**SHA256:** `d409e9c7f73f5671a27796ccc4a28c71850d9beeab06e012d8361ab8da7600ad`

______________________________________________________________________

## v39 — 2026-07-20 — add-bootstrap-deadlock-guidance-to-system-prompt-7f94

**Summary:** Add a "Bootstrap deadlock" bullet to the Merge/PR management section of the agent
system prompt. When a PR modifies the merge pipeline itself (robotsix-mill CI, gate logic, or merge
endpoints), auto-merge through the mill may be self-referential — the gate being changed can block
its own merge. The agent should escalate to the operator via a user_chat subsession for a manual
merge rather than looping on merge-now.

**Rationale:** PR #2475 was blocked for 14 iterations because the gate it aimed to change prevented
its own merge, and the agent looped on merge-now without understanding the self-referential
deadlock. This guidance was originally drafted in PR #688 (ticket 45b9) and is extracted here as a
clean standalone addition.

**SHA256:** `346af495da125fc27d3225d7f6a5d9699ff6aba8206c987782a203b3d5dd6ed1`

______________________________________________________________________

## v39-b — 2026-07-20 — add-deploy-server-restart-capability-for-144c

**Summary:** Add `self_restart` to the Deploy API quick-reference bullet list and update the Reload
step (step 6 of the ticket lifecycle) to reference `self_restart()` instead of
`restart_lifecycle_service('chat')` for self-restart. The Deploy API list now includes both
`restart_lifecycle_service` (restart any service, requires per-repo toggle) and `self_restart`
(restart the agent's own service, no toggle required). This gives the agent a clear path for
self-restart even when the per-repo access toggle is not enabled.

**Rationale:** The existing `restart_lifecycle_service('chat')` path required the deploy server's
per-repo access toggle to be enabled, which is typically off for the agent's own service. The new
`self_restart` tool calls `POST /self/restart` — a privileged endpoint that identifies the calling
service from the API key and permits the restart unconditionally. This unblocks the agent when it
needs to self-restart after picking up new capabilities.

**SHA256:** `a3dcab48d87f5235fb66ee928961604dc2d47fd6ab357c047bfe4807ef634d62`

______________________________________________________________________

## v38 — 2026-07-20 — decision-chat-subsessions-must-embed-full-77c1

**Summary:** Add an option-label restatement rule to the user_chat subsession guidance. When
presenting a decision to the operator, the agent must always restate the full definition of each
option inline — never surface a bare label like "Option B" without its definition. This applies to
every turn (initial recommendation and follow-up confirmation gates) and covers all options present
in the menu. The operator sees only the panel output, not the subsession's instructions, so the
definitions must travel with every reference.

**Rationale:** Decision subsessions were surfacing recommendations as bare labels ("Option B is the
right call") while the option definitions lived only in the spawn instruction. The operator had no
way to disambiguate labels without switching context, and this was a recurring failure across
multiple decision chats. The new rule extends the self-contained-instructions principle to outbound
operator-facing turns.

**SHA256:** `501a7f57365d705c6bbf7b250196da279c238c2a31977017df4cfc60a6e38e6d`

______________________________________________________________________

## v37 — 2026-07-20 — direct-fix-capability-chat-agent-can-push-validated-fixes

**Summary:** Add a `direct_fix` tool to the system prompt's direct-repo section: when a ticket has
exhausted the mill's implement cycle limit (≥3 failed implement attempts), the agent may push a
commit directly to the target branch, bypassing the PR flow. The tool is a last-resort escape hatch
for mechanically simple, validated-correct fixes (e.g. stale-SHA replacements, file deletions,
find-replace) that are blocked on rebase churn. Before calling direct_fix the agent must: (a)
confirm ≥3 implement cycles; (b) verify the fix is deterministic, reviewable, and low-risk; (c) get
explicit human operator approval via a user_chat subsession. Every invocation is audited at WARNING
level.

**Rationale:** The direct-repo module now exposes a `direct_fix` tool gated behind
`direct_repo.direct_fix_enabled`. The system prompt must document the tool and its guardrails so the
agent knows when and how to use it, including the required pre-conditions and the audit trail
requirement.

**SHA256:** `ae8151436ae1c006268f845d6713b7031ff49ae5032406a993abac6e009451d9`

______________________________________________________________________

______________________________________________________________________

## v36 — 2026-07-20 — contract-version-troubleshooting-guide

**Summary:** Add a "Contract-version troubleshooting" bullet to the Deploy system guidance in the
Autonomy section. When a user encounters a "missing or incorrect central-deploy-contract-version
header" error during onboarding, the agent must diagnose concretely: (a) check whether the
component's deploy/docker-compose.yml has the header as its first line and walk the user through
adding it if missing; (b) if present but rejected, check recent PRs for a version bump; (c) if the
correct version remains unclear after checking the repo, file a ticket on the component repo to
clarify the expected contract version. Never just suggest filing a follow-up ticket without first
checking the header's presence and version.

**Rationale:** During a session the assistant recognized a contract-version error as a lockstep
mismatch but only offered vague options (file a ticket or redeploy). The user had to debug
repeatedly. The new guidance gives concrete diagnostic steps so the agent can resolve the error
directly or pinpoint the exact gap before escalating.

**SHA256:** `ecc395d422b34d30c73f2814f3aaaaaf9c483116869b34ec8ac71ba5153d6287`

## v35 — 2026-07-20 — lifecycle-mutation-tools-self-restart-config-write

**Summary:** Update the Deploy API quick-reference to list lifecycle mutation tools
(`restart_lifecycle_service`, `update_lifecycle_service_config`, `update_lifecycle_service_env`)
instead of a `component_request` path to the central-deploy component. Update the Reload step (step
6 of the ticket lifecycle) to reference `restart_lifecycle_service('chat')` instead of
`POST /chat/services/chat/restart`.

**Rationale:** The lifecycle module now exposes mutation tools (restart, config-write, env-write)
gated by the deploy server's per-repo access toggle. The previous
`component_request("central-deploy", …)` path required the central-deploy service to be in the
component roster, which it was not — making the endpoint unreachable. The lifecycle tools use the
existing lifecycle base URL and auth, so the agent can reach these endpoints directly.

**SHA256:** `110dcb100d67ab3c3e92c4af2d671a54a33886115831c070661a02044dc6e802`

______________________________________________________________________

## v34 — 2026-07-20 — improve-handling-of-rebase-conflicts-avo-8b37

**Summary:** Enhance the Remediate step of the Ticket lifecycle with explicit merge/rebase conflict
handling: the agent must never auto-retry merge/rebase conflicts (they are not auto-retryable since
the assistant has no conflict-resolution tools), must open a user_chat subsession with a specific
diagnostic message, and must not loop-retry. Also adds explicit categories for substantive blockers
(merge/rebase conflicts, missing dependencies, design deadlocks) vs transient failures.

**Rationale:** The agent previously loop-retried merge-conflict-blocked tickets, wasting cycles and
generating noise. This guidance gives the agent a clear branching path: auto-resume only transient
failures, surface substantive blockers with a specific diagnosis.

**SHA256:** `28625c3b503d2496e6bb56372fdf94d8ebe7bbdb24de179831ec35e376710c53`

______________________________________________________________________

## v33 — 2026-07-20 — correct-mistaken-understanding-of-centra-0b5b

**Summary:** Add a "Deploy system" bullet to the Autonomy section documenting that the
robotsix-deploy (central-deploy) management plane is a runtime API server, not a git repository.
Component onboarding, lifecycle operations, and configuration changes are all API-driven (POST
/onboard/preflight, /onboard/confirm, etc.). The deploy/docker-compose.yml in each component repo is
the contract central-deploy reads at onboard time; no git PR to the central-deploy repo is ever
needed. Instructs the agent not to suggest git PRs or repo changes for central-deploy onboarding or
lifecycle operations.

**Rationale:** During a session the assistant repeatedly suggested that onboarding a component
required a git PR to the central-deploy repo. Only after investigating the actual codebase did it
discover that onboarding is a runtime API operation. This caused lengthy, confusing back-and-forth
with the user. The new instruction closes this knowledge gap by explicitly distinguishing the
API-driven deploy system from git-driven workflows.

**SHA256:** `50aa4a754a18b4a2de813a876a73923a73c179966687a32514be46c68e8a05a9`

______________________________________________________________________

## v32 — 2026-07-20 — document-mill-merge-now-endpoint-and-add-feda

**Summary:** Add a dedicated "Mill & Deploy Endpoints" section to the default `agent_instruction`.
Lists all key mill endpoints (ingest, list, get, merge-now, resume-blocked, health) and deploy
endpoints (self-restart) with paths, HTTP methods, component IDs, and descriptions. Instructs the
agent to create a knowledge note cataloguing these endpoints for cross-session reference.

**Rationale:** Despite v28's merge-capability guidance, the agent still failed to discover the
merge-now endpoint during a session, attempting auto-merge transitions that failed repeatedly. A
comprehensive, searchable endpoint catalog in the system prompt ensures the agent can reference
available endpoints reliably without needing to discover them through trial and error.

**SHA256:** `9bd858c8e09e4828fa636c4a2c849a010819c1ce0acc6aaa863113a576b5aeb8`

## v31 — 2026-07-20 — explicit-operator-approval-gate-for-batc-fd34

**Summary:** Add a batch-MR-approval bullet to the Merge / PR management guidance in the Autonomy
section. When multiple MRs are pending human approval, the agent must first assess which are
strictly needed for active tickets versus incidental, present a categorized prompt that lets the
operator filter in one reply (e.g. "14 MRs pending: 3 needed for active tickets, 11 incidental.
Approve the needed ones, all, or exclude specific MRs?"), and then approve the selected group in
bulk through the mill's merge endpoint.

**Rationale:** After a gate fix left 14 MRs at the human-approval stage, the assistant lacked
guidance on which to approve. The operator had to manually check each MR before replying "approve
only the one you need." The new instruction adds a categorization step so the operator can filter in
one reply rather than inspecting every MR individually, reducing back-and-forth.

**SHA256:** `1be126bf59a010259f66e570b008fbceca627fe604447e0e2784bfda968abf99`

______________________________________________________________________

## v30 — 2026-07-20 — handle-ambiguous-single-word-commands-wi-1d61

**Summary:** Add a pick-list instruction to the Autonomy section: when multiple unowned, actionable
items exist (pending merges, unresolved tickets, queued operations), the agent must not ask an
open-ended "Which do you mean?" — it must immediately offer a high-signal, scoped confirmation
prompt listing each item compactly (e.g. "Say: merge 5f1c, merge 2a97, rebase 54ea.").

**Rationale:** When the user issued a command like "do it" that could apply to multiple pending
items, the assistant was asking "Which do you mean?" before enumerating options. This broke flow.
The new instruction guides the agent to immediately present a pick-list format, reducing
back-and-forth and cognitive load.

**SHA256:** `f0aa4c393e144fffcbc9f053d9ac7937444ddf996beb373b0cdb3248f9e6d553`

______________________________________________________________________

## v29 — 2026-07-19 — prevent-creation-of-duplicate-monitors-f-8af3 & cross-reference-historical-claims-with-live-state-11ec

**Summary (dedup_key):** Extend `dedup_key` deduplication from `user_chat`-only to all subsession
kinds. The old guidance only mentioned `user_chat` for global error dedup; the new text covers
periodic ticket monitors too — set `dedup_key` to the ticket id when spawning a monitor (e.g.
`'5f1c'`). The Monitor lifecycle step also now specifies `dedup_key` usage. The dedup guard in
`spawn_subsession` no longer filters by `SubsessionKind.USER_CHAT`, so any subsession with an active
dedup_key returns the existing id instead of spawning a duplicate.

**Rationale:** Two periodic monitors were spawned for the same ticket, causing double reports and
manual cleanup. Extending the dedup guard to all kinds prevents duplicate periodic ticket monitors
when an agent re-files the same ticket, reducing noise and cognitive load.

**SHA256 (dedup_key):** `ea1236db91d830f86dfc401efeb61a7ba8603a4e6f096bac982855d89763bfe2`

**Summary (Verification):** Add a "Verification" section to the default `agent_instruction`. When
reporting the state of an external system (repository contents, deployment status, ticket
resolution), the agent must verify through available tools rather than relying on memory alone. When
the user directly challenges a claim with contradictory observable evidence, re-verify against the
live system immediately rather than doubling down on a memory-based assertion. Prefer timestamped
evidence (commit SHA, deployment timestamp, tool call result) over recollection.

**Rationale:** Memory-based claims that contradict user-observable reality (empty repo, stale
container) damage trust and require additional verification steps. The agent must treat live system
state as the source of truth and distrust memory when it conflicts with live observation.

**SHA256 (Verification):** `d8abc681dfd9de968e6dece0e1d6a51bc8ad2f8f7c2351b5a65ce4a2be1c9610`

______________________________________________________________________

## v28 — 2026-07-19 — document-merge-capability-via-mill-api-d1a3

**Summary:** Add a "Merge / PR management" bullet to the Autonomy section documenting that
direct-repo tools (push_direct_repo_branch, open_direct_repo_pr) push branches and open PRs without
auto-merge (the merge gate stays human), and that merge capability exists through the mill API via
component_request (merge-now and related endpoints). Instructs the agent not to claim it lacks merge
capability and not to attempt auto-merge via direct-repo tools.

**Rationale:** The agent was generalising "no merge capability on the direct-repo path" to "I cannot
merge at all," causing it to falsely claim inability when approved MRs were ready to merge. The
agent bounced approved MRs through waiting_auto_merge 4 times before discovering the mill's
merge-now endpoint. This change closes the knowledge gap so the agent uses the mill's merge
endpoints first.

**SHA256:** `436be0c1a8683984e7dc721d039bf3d4bd3dfa108d462f3f8542617fdd2939e8`

______________________________________________________________________

## v27 — 2026-07-19 — deduplicate-known-broken-asyncio-run-err-54ea

**Summary:** Add dedup_key guidance to the agent_instruction default. When spawning a user_chat to
report a known global process error (e.g. asyncio.run() errors), set dedup_key to the exact error
message prefix (first 80 chars). The system will suppress duplicate side-chats for the same root
cause — only the first spawn creates a new subsession. Always pair with list_subsessions to check
what is already running.

**SHA256:** `00cf8271575ee7a1d9965eb9c4429bf7947def9e5e5aaaf6c72880fe80f4c771`

______________________________________________________________________

## v26 — 2026-07-19 — simplify-credential-handling-avoid-expos-a275

**Summary:** Add a "Secret handling" section to the default `agent_instruction` covering three
behaviors: (a) pre-empt — when a task will require a secret, halt and direct the user to the secure
credential-registration channel (vault / one-time-secret link / registration ticket secure scope)
BEFORE they paste the plaintext value; (b) do not echo — never repeat, quote, or restate plaintext
secrets that appear in the conversation, redact or reference them generically instead; (c) remediate
— when a secret has already been pasted as plaintext, warn the user it is exposed in history,
recommend rotating it, and route registration through the secure channel without using the plaintext
value.

**Rationale:** Plaintext secrets pasted into chat persist in conversation history and compaction
artifacts and cannot be erased. The agent must prevent exposure before it happens rather than clean
up afterward.

**SHA256:** `f547bbff537bc7c2694f71d76e143dbaebb76ed0fb8b4d6da298d823af8a86cc`

______________________________________________________________________

## v25 — 2026-07-19 — prevent-redundant-ticket-creation-when-a-652b

**Summary:** Extend the Initiate step in the Ticket lifecycle with deduplication guidance: before
filing a new ticket, check `list_tickets` for an active ticket with the same scope to avoid creating
duplicates. When a new ticket supersedes an older one, mention the predecessor's id in the spec and
cancel the predecessor's monitor subsession so only one monitor runs for the same work.

**SHA256:** `31388ebb20a25bf9c9a70c5ace06bbab39700f4f6c5e26831cc7559a91e462f2`

______________________________________________________________________

## v24 — 2026-07-19 — improve-clarity-of-system-notices-for-re-1d76

______________________________________________________________________

## v23 — skipped

> **Note:** Version v23 was skipped — no prompt change was recorded under this version number.
> The sequence jumps from v24 directly to v22. This entry exists to document the gap and prevent
> future ambiguity. If the original v23 changelog entry is recovered from git history, it should
> be inserted here.

______________________________________________________________________

## v22 — 2026-07-12 — add-one-subsession-per-subject-rule-to-s-efab

**Summary:** Add a "one subsession per subject" rule to the subsession guidance in the default
`agent_instruction`. Instructs the agent to spawn separate subsessions for distinct subjects rather
than consolidating unrelated ticket batches, decision groups, or operational contexts into a single
subsession. Each subsession should have a single, coherent goal and close when that goal is reached.

**SHA256:** `c9da8ee6d80ebf1f9c1f243638e519172453db5e20e1d98581609fefca53e895`

______________________________________________________________________

## v21 — 2026-07-11 — formalize-autonomous-ticket-lifecycle

**Summary:** Replace the single capability-upgrade bullet in the Autonomy section with a full
ticket-lifecycle block covering Initiate, Monitor, Remediate, Complete, Reload, and Exit. The
guidance makes autonomous ticket tracking the default behavior: periodic subsession (30 min, max 60
runs, terminate after 2 consecutive mill-unreachable failures), auto-resume transient failures,
operator surfacing for substantive blockers, NO_CHANGE for unchanged states, and hold (no polling)
for fingerprint-guarded hard-stuck tickets.

**SHA256:** `c01b0918c8765e40e05c9b8a3742a39db88c9f4492cf910c2c5fe7b37e5a027b`

______________________________________________________________________

## v20 — 2026-07-07 — self-upgrade-capability-via-tickets

**Summary:** Add a bullet to the Autonomy section documenting that the agent upgrades its own
capabilities by filing tickets on the robotsix-chat repo: new tools, components, and permissions are
granted through the standard ticket workflow, and after merge+deploy the agent self-restarts via the
deploy component to pick up newly registered capabilities.

**SHA256:** `a3a77a1426baf3da4a300b107e7a9401f6325490d6878d0374753c741fa97ab4`

______________________________________________________________________

## v19 — 2026-07-05 — subsession-prefer-level-2-for-general-work

**Summary:** Reword the subsession `model_level` guidance in the default `agent_instruction`. Level
3 (keyless Claude Opus) was described as "the default for general work" while levels 1-2 were
pigeonholed to "trivial polling/extraction", so the agent nearly always spawned level-3 subsessions
even for tasks a cheap OpenRouter tier could handle. Now level 2 is the default choice for general
work, level 3 is reserved for reasoning level 2 struggles with, and the text tells the agent to
retry at level 3 if a level 1-2 spawn errors for a missing API key.

**SHA256:** `0387f250d8092d248e1e29b7736966c09aa1c3e6a32df4d7c6bb42024a07e939`

______________________________________________________________________

## v18 — 2026-07-04 — default-prompt-promises-component-request-cc62

**Summary:** Remove the "Component access" section from the default `agent_instruction`. It is now
conditionally injected by `create_agent_from_settings()` only when `central_deploy.url` is
configured, so the prompt no longer promises a `component_request` tool in the default out-of-box
deployment where no central-deploy roster is wired.

**SHA256:** `91f785fc2ff229ecc5c5bfd39c75b3aaaa5b070cf0b0a9a7f31066ac1787e3f2`

______________________________________________________________________

## v17 — 2026-07-04 — knowledge-tool-names-in-system-prompt

**Summary:** Update the knowledge-base tool names in the agent system prompt from shorthand
(`add/append/update/list/read_knowledge_note`) to the actual tool names
(`add_knowledge_note, append_to_knowledge_note, update_knowledge_note, list_knowledge_notes, read_knowledge_note`).

**SHA256:** `efb12c78d114b5ea64d3bb79c4522b74c6e1c82a4203abe79c69e4d56ceca041`

______________________________________________________________________

## Governance policy

Every change to `Settings.agent_instruction` (the pydantic field default literal in
`src/robotsix_chat/config/settings.py`) **MUST**:

1. **Bump** `SYSTEM_PROMPT_VERSION` to the next integer.
1. **Add a new entry** at the top of this file (reverse-chronological, newest first) with the header
   `## v<N> — <YYYY-MM-DD> — <ticket-id>`.
1. **Record the SHA256** of the new `agent_instruction` default literal (computed as
   `hashlib.sha256(default.encode()).hexdigest()`) in the entry.
1. The `agent.instruction` row of `docs/configuration.md` uses the placeholder `(long default)` in
   the Default column — the full multi-paragraph instruction literal is impractical to embed
   verbatim in a Markdown table cell. Do not attempt to inline the literal; the placeholder is
   sufficient.

A CI test (`tests/config/test_system_prompt_governance.py`) enforces that the latest entry's version
matches `SYSTEM_PROMPT_VERSION` and its recorded hash matches the live default — edits that skip
this file **will fail CI**.

### Rollback procedure

Rollback is a **forward-moving new version** — never reuse a version number. To revert to a previous
prompt:

1. Pick the target prior version's entry in this changelog.
1. Restore its prompt text via git, e.g.: `git revert <commit>` or
   `git show <commit>:src/robotsix_chat/config/settings.py` (extract the `agent_instruction` block).
1. Bump `SYSTEM_PROMPT_VERSION` to the next number.
1. Add a new changelog entry `## v<N> — <YYYY-MM-DD> — <ticket-id>` with:
   - **Summary**: `rollback to v<K>`
   - **Rationale**: why the rollback is needed and which ticket authorises it.
   - **SHA256**: the hash of the restored literal (must match the prior version's recorded hash).
1. The `agent.instruction` row of `docs/configuration.md` uses the placeholder `(long default)` — no
   change needed there for a rollback.

______________________________________________________________________

## v16 — 2026-07-04 — generic-component-access-roster-skills

**Summary:** Replace the Board/mill rules and Calendar/task tools sections with a new "Component
access" section describing the generic `component_request` tool, roster-based skill loading, and the
requirement to obey each component skill's safety section (ask the user before calling
confirmation-required operations). The old broker-based board and calendar tool guidance is removed;
all component interaction now goes through the single generic tool.

**SHA256:** `d6067ea41ef447564913d75031059f476e86c1817e601a4d395801fbad76a161`

______________________________________________________________________

## v15 — 2026-07-02 — subsession-redesign

**Summary:** Replace all `delegate_task` / check-loop / pending-question guidance with a new
"Subsessions" section for the unified subsession system: when to spawn each kind (`task`,
`periodic`, `user_chat`), model-level selection by difficulty and cost (1-2 cheap OpenRouter, 3
default, 4 frontier reserved for hard reasoning), self-contained-instructions requirement,
steering/inspecting/closing running subsessions (`message_subsession`, `list_subsessions`,
`close_subsession`), `complete_subsession` discipline (self-close at verified terminal states,
`NO_CHANGE` convention for periodic runs, ask pending user questions once), and depth-limited
nesting. Board rules updated accordingly: subsessions now carry the full tool suite, so the old hard
"never offload board actions" rule becomes "prefer inline; a subsession doing board work must verify
results with `list_board_tickets` before reporting success", and the `verify_via_board` /
`stop_check_loop` rules are dropped with the machinery. The Autonomy example now references closing
a terminal periodic subsession.

**Rationale:** The chat system was redesigned around one unified subsession primitive (spawned
sub-agents at chosen model levels, nested, periodic, or user-facing) replacing three separate
systems (`delegate_task` background tasks, check loops, pending questions). The prompt must describe
the new tool surface and encode the cost-control guidance (model levels) that previously lived in
static `subagent_model` / `check_loop_model` config overrides.

**SHA256:** `06cbece9be305939cabcd498992a1cf764a2c9e5467022086986b536a496ad38`

## v14 — 2026-06-28 — 20260626T130813Z (autonomy) + 20260626T215106Z (check-loop stateful monitor)

**Summary:** (a) Add an "Autonomy" section instructing the assistant to proactively perform safe,
reversible actions without waiting for explicit human validation, while gating risky/irreversible
actions behind human approval. Includes a concrete rule: when running inside a check loop and a
verified terminal/completion state is reached, call `stop_check_loop` immediately instead of
emitting repeated COMPLETED/NO_CHANGE reports. (b) Add check-loop guidance: tick sub-agents should
call `stop_check_loop` when the monitored item reaches a terminal state (belt and suspenders with
programmatic auto-stop detection); pending decision questions must be asked once and not repeated on
subsequent unchanged ticks (the loop auto-pauses after detecting no change).

**Rationale:** The Autonomy section eliminates unnecessary validation friction for safe, reversible
actions. In check loops specifically, the assistant (a) continued emitting redundant COMPLETED
reports after a terminal state was verified instead of self-stopping, and (b) re-asked identical
decision prompts (e.g. "resume or hold?") on every tick. The combined prompt update closes both
gaps.

**SHA256:** `0b989515af6b148c7f5aec0b86e590620cca6f7df23ef5a2884e4b16fd252d3d`

## v13 — 2026-06-28 — false_default_repo_claim

**Summary:** Remove the false universal claim that "new tickets default to robotsix-mill regardless
of source" — the board manager (via `consult_mill`) may route tickets to `robotsix-mill` by default,
but `create_board_ticket` has no such default (the agent provides `repo_id` explicitly). The
verification rule now attributes the default correctly to the board manager rather than asserting it
as a universal fact.

**Rationale:** The universal claim misleads the agent into thinking direct `create_board_ticket`
calls might silently land on the wrong board, inviting unnecessary verification steps. Fixing the
wording eliminates this confusion while keeping the verification instruction universal (both
`create_board_ticket` and `consult_mill` paths benefit from post-creation verification).

**SHA256:** `ddc129c8c333f50cfc17064d815a471eeab7cf982da6206243d798dd3ad2c480`

## v12 — 2026-06-28 — board_rules_contradict_create_ticket

**Summary:** Resolve contradictory Board/mill rule for ticket creation. The old Rule 1 directed ALL
write operations (including ticket creation) to `consult_mill`, but Rule 4 told the agent to use
`create_board_ticket` (which calls the board reader endpoint directly). Rule 1 is now scoped to
complex write operations (migrate, transition, triage) only; simple ticket creation uses
`create_board_ticket`. The verification rule is also generalised from "via consult_mill" to cover
both paths.

**Rationale:** Two rules gave conflicting directives for the same action, causing the model to guess
which tool to use. Aligning them eliminates the contradiction.

**SHA256:** `81cc03108729b4e4fe46c2b191c5863a8b4ce018f5c99fda4d2927f4bd722a0c`

## v11 — 2026-06-27 — agent_guard_bypasses_governance

**Summary:** Fold the runtime `_AGENT_GUARD` hardening layer (previously appended in `agent.py`)
into the `agent_instruction` default literal so it is governed by `SYSTEM_PROMPT_VERSION`, this
changelog, SHA256 tracking, and CI enforcement.

**Rationale:** The guard text — which tells the model it cannot run shell commands, read/edit files,
browse the web, or access the host — was injected at runtime without any governance coverage. Any
edit to it would change what the model receives with no version bump, no changelog entry, and no CI
failure. Moving it into the versioned default closes this gap.

**SHA256:** `3aeecd5f472970cd59cd4b92a889c83e5c0608b89c99137c14f7c96fc45523c6`

## v10 — 2026-06-25 — 20260625T123055Z-system-prompt-contains-internal-python-i-d0a8

**Summary:** Replace calendar/task tools paragraph — remove three developer-facing identifiers
(`build_calendar_tools()`, `CalendarSettings.enabled=False`, `CALENDAR_BROKER_TOKEN`) that don't
belong in an LLM system prompt, replacing them with LLM-appropriate language.

**Rationale:** The prior text was copy-pasted from a development ticket without adaptation for the
LLM audience. The new text describes the tools' purpose and behaviour in terms the model can act on
(availability, disabling, and the instruction to briefly note unavailability rather than proposing
alternatives).

**SHA256:** `1d0ec5213cf5931aff7ec8e7abe4f46f8320ac4c13bbba1ef1aa96040d3f4c37`

## v9 — 2026-06-25 — 20260624T212653Z-chat-agent-hard-block-delegate-task-for-0619

**Summary:** Add enforcement note — `delegate_task` now refuses board/ticket work and directs the
agent to use `consult_mill` instead.

**Rationale:** Previously the prompt warned that delegate-task results are never returned and a
ticket filed that way may silently fail. The programmatic gate is now in place — `delegate_task`
actively rejects board/ticket requests — and the prompt reflects this enforcement so the agent does
not attempt the now-blocked path. Ticket:
20260624T212653Z-chat-agent-hard-block-delegate-task-for-0619.

**SHA256:** `15cad9cc4a5854fa5c4682f0921cd534d21fb4542b89b722c28dfa44476257de`

## v8 — 2026-06-24 — 20260624T212711Z-chat-agent-stop-redundant-tool-loading-n-a0f3

**Summary:** Remove the misleading "Load tools once / run a capability check" directive and replace
it with an instruction that all tools are already available and narration of
loading/preparing/fetching tools is forbidden.

**Rationale:** The previous directive ("Load tools once at the start of a session. Before branching
into a complex workflow, run a single generic capability check.") actively invited dead narration
like "I'll load the tools…" / "Let me load the task management tool first" — wasting ~150–200 output
tokens per trace. Tools are assembled once at startup and never reloaded per-turn; the prompt now
reflects reality and explicitly forbids the narration. Ticket:
20260624T212711Z-chat-agent-stop-redundant-tool-loading-n-a0f3.

**SHA256:** `8aaf695d4004ff37872c8f183954324aacc1f6bb6d6e8a3b91033f0463d81ef2`

## v7 — 2026-06-24 — 20260624T212702Z-chat-agent-dedup-ticket-filing-before-su-6ed5

**Summary:** Add dedup rule before ticket filing — the agent must check for existing open tickets
covering the same intent before creating a new one, and act on `create_board_ticket`'s built-in
duplicate warnings.

**Rationale:** Prevents duplicate ticket creation by enforcing a pre-flight board read and requiring
the agent to reuse existing tickets rather than filing duplicates.

**SHA256:** `ae70fa569e48c2ef71d35a731a112ad0e4b490434ef2ddc8d0a19173e8a4099e`

## v6 — 2026-06-24 — 20260624T212708Z-chat-agents-enforce-the-three-sentences-236a

Tighten conciseness rule: name prohibited output shapes explicitly.

- Extended the three-sentence bullet to explicitly gate multi-row markdown tables, timeline/audit
  dumps, and recap lists behind an explicit user request.
- Added prohibition on repeating content already shown in the same conversation.

**SHA256:** `344e725c838591049557069cd1aa654422d886e13ece396b2016b9aeb4657dc7`

## v5 — 2026-06-24 — 20260623T210918Z-gate-sub-agent-status-output-behind-a-ma-e2f0

**Summary:** Added a bullet to the Board/mill rules instructing the foreground agent to set
`verify_via_board=True` when launching a check loop that monitors mill/board/thread/ticket status,
and to never assert board status without a fresh `consult_mill` read.

**Rationale:** Prevents fabricated status reports from check-loop sub-agents by enforcing a
board-read gate. The new bullet is defense-in-depth alongside the programmatic gate in `loops.py`.

**SHA256:** `2188c9422da9d5a9db8cf024095d8717b0e779391b25903c91482109ceff75ff`

______________________________________________________________________

## v4 — 2026-06-24 — 20260623T204239Z-robotsix-chat-give-the-assistant-a-writa-ff6c

**Summary:** Add knowledge-base instructions — the agent now has a local, durable knowledge base
(add/append/update/list/read_knowledge_note tools) for operational notes and lessons; it must
consult it at the start of every session and write durable findings to it.

**SHA256:** `efd64ca3849a4f0872754fa86119a18511edc0c4a1816a94206e24dc618f1e8b`

## v3 — 2026-06-24 — 20260623T210933Z-tighten-sub-agent-prompt-efficiency-check-5a52

Add sub-agent efficiency rules (cost-analysis proposals #9 and #10):

- #9: Check tool availability before describing a plan; if a required tool is missing, state it in
  one sentence and stop. Answer in three sentences or fewer unless the user explicitly asks for
  elaboration.
- #10: Load tools once at the start of a session. Before branching into a complex workflow, run a
  single generic capability check. Do not re-load the same tool descriptions across turns.

**SHA256:** `323b644912809fda2d4ed9f80cf0e01d6742f6b6c05d5ff85d440e83e65aba52`

## v2 — 2026-06-24 — 20260624T020652Z-give-the-assistant-direct-1628

Add board-reader tool guidance:

- New rule: use `list_board_tickets` / `read_board_ticket` for reading board state (HTTP endpoint,
  same as user's UI); use `consult_mill` for writes. Never fabricate ticket states.
- Verification: after creating a ticket via `consult_mill`, verify it landed on the right board with
  `list_board_tickets`.

**SHA256:** `33af94596b21c0f64908d3aa93eb2c8c8d1f491ed52dcab1c6287ff3c36128c5`

## v1 — 2026-06-23 — 20260623T204251Z-robotsix-chat-governance-for-assistant-s-45f3

**Summary:** Baseline — the current `agent_instruction` default literal as established by ticket
`20260623T203856Z-robotsix-chat-update-the-assistant-s-own-838a` and recorded when this governance
layer was introduced.

**Rationale:** Ticket …-838a appended board/mill operational guidance (delegate-vs-inline,
board-placement verification, draft→ready auto-pickup) and calendar/task tool guidance to the
pre-existing "You are a helpful assistant." prefix. This entry locks in that known-good state.

**Diff:** `git show 7b890de -- src/robotsix_chat/config/settings.py` (the …-838a merge commit), or
`git log -p -- src/robotsix_chat/config/settings.py` scoped to the `agent_instruction` block.

**SHA256:** `09b73c46b24449484a5e2e9484137b85d73cfe210aa31eac05c81ca4f0698674`

______________________________________________________________________

## Autonomous Prompt Changelog

Governed artifact: `build_autonomous_instruction()` in
`src/robotsix_chat/autonomous/prompts.py`. Version stamp: `AUTONOMOUS_PROMPT_VERSION` in the same module.

The hash is computed on the output of `build_autonomous_instruction(Settings())` — i.e. with all
autonomous settings at their pydantic field defaults
(``completion_marker="---AUTONOMOUS COMPLETE---"``,
``stale_monitor_runs_before_completion=3``).

## AUTONOMOUS v23 — 2026-08-13 — avoid-suggesting-stale-disconnected-moni-7f5e

**Summary:** Add a monitor re-activation guard.  Before offering to resume,
wake, or re-activate any paused or auto-paused monitor, the agent must
verify that the tracked resource still exists and is meaningful (query the
board for the tracked ID, or use `list_subsessions`).  If the tracked ticket
returns 404, has been deleted, or no longer exists, the monitor is tracking
a stale/broken ID — do not offer to wake or resume it; instead report the
broken monitor and stop it to free its pool slot.  Apply the same check when
a monitor auto-pauses on NO_CHANGE: confirm the underlying resource still
exists before suggesting re-activation.

**Rationale:** The assistant created a monitor for a non-existent ticket ID,
later received a stale/broken-ID notice, and then offered to wake the
monitor — a pointless action.  Validating tracked IDs before re-activation
avoids offering to resume monitors that would watch nothing.

**SHA256:** `b9833bee1ef2c891e0bc2252bc1f2c980f3cb8817a6c5bf344dea2ff682e6d7d`

## AUTONOMOUS v22 — 2026-08-16 — autonomous-sessions-should-run-as-normal-2694

**Summary:** Remove the proposal/approval handshake from the autonomous
protocol.  The agent no longer emits `---PROPOSAL READY---` and waits for
operator approval after drafting a plan.  Instead it plans, then begins
executing immediately, and works autonomously until it emits the completion
marker.  If it hits a blocker it surfaces it in a normal message and
continues with the actionable parts of the plan.  The session now closes
automatically on completion (the configured trigger starts the next one),
rather than staying open for the operator to close.

**Rationale:** The operator wants autonomous sessions to be ordinary chat
sessions that start automatically and run to completion — plan/approval
behaviour, if any, should come from the session's own prompt, not a bespoke
proposal state machine in the runner.

**SHA256:** `05b91d11723befaba56ebfc13e00900148271f33a5a69903bb58579b9a2286bd`

## AUTONOMOUS v21 — 2026-08-13 — monitor-terminal-state-and-consolidation

**Summary:** Refine the stall-guard guidance for periodic monitors.  When a
monitor auto-pauses after consecutive no-change cycles, the agent must state
the terminal state plainly — "no change — the monitor is paused and still
alive; it will resume when the ticket updates or on a new message; no action
needed" — rather than replying with a bare pause confirmation.  When a monitor
auto-stops (terminal, tracking interrupted), the agent must state "no change —
the monitor auto-stopped; the ticket may still need attention" and then offer a
re-scoping or split strategy.  Add a consolidation rule: multiple identical
monitor notices (no-change, auto-pause, or auto-stop) in the same window are
folded into one status update instead of one message per monitor.

**Rationale:** Session fbd3ce5b65ff4cb591e4a7b8384a5504 observed an
OSV-exemption monitor auto-pause acknowledged with only a confirmation of the
pause; the operator was left unaware that no progress had been made and that no
action was needed, and identical monitor notifications were repeated rather
than consolidated.

**SHA256:** `70aae0a01b4c6aaefedc114c0fd1f49278e42d4a8457b49fc50879d93962a5ff`

## AUTONOMOUS v20 — 2026-08-13 — automate-self-restart-after-capability-a-3ab1

**Summary:** Add `AUTO SELF-RESTART` section to the autonomy tier. When
`auto_self_restart` is ON, the agent may call `self_restart` without operator
approval after deploying capability changes (code changes, component roster
updates) that affect its own behaviour. The agent must announce the restart
with a brief delay (at least 30 seconds) so the operator can interrupt if
needed. Self-restart for any other reason still requires explicit operator
authorization. The `auto_self_restart` flag is added to the tier display line
alongside the existing `auto_approve_self_authored`, `allowlist`,
`auto_approve_routine_secret_provisioning`, and `suppress_no_change_monitors`
settings.

**Rationale:** After capability-changing deploys the assistant previously asked
the user for permission to self-restart — a mechanical operation that adds
conversational overhead. Automating it under an explicit opt-in flag makes the
system feel more autonomous while preserving the operator's ability to interrupt.

**SHA256:** `b9330697842041868a4ebfc75468efee8eb19773c88a8322fd86ccf649a3f05b`

## AUTONOMOUS v19 — 2026-08-09 — reduce-approval-gate-friction-by-surfaci-cad4

**Summary:** Add an `AUTONOMY PREFERENCE PROBING` section to the AUTONOMY TIER block.
The agent now proactively asks the operator about their autonomy preferences for low-risk,
mechanical decisions early in the session — before reaching any approval gate — so the trade-off
is surfaced as a choice rather than a surprise.  When the operator expresses frustration with an
approval gate on low-risk or self-authored work, the agent must acknowledge the friction, explain
the hard safety rule once, and proactively offer to file a rules-change ticket (via
`POST /tickets/ingest`) proposing a config or prompt change that reduces the friction.  After
repeated frustration, the agent escalates to an explicit proposal naming the concrete change and
asking for a yes/no, instead of only explaining the rule after the fact.

**Rationale:** Session c0ff4ec1b46b4ba4bcc26390c19c3f21 — the user was frustrated at being asked
for approval on a low-risk, self-authored ticket (bc17), saying "you should only come back to me
when very necessary".  The assistant correctly explained that the approval gate is a hard safety
rule, but only after the fact, so the user's expectation was not met.  This instruction moves
autonomy-preference discovery earlier and gives the agent a concrete follow-up path (a rules-change
ticket) when the operator shows repeated frustration with approval gates.

**SHA256:** `7319773db9b3c906f91e0d52f625d9565f89be1f0c0179efc22f53dd29aaa38e`

## AUTONOMOUS v18 — 2026-08-10 — pre-check-codebase-for-existing-features-275b

**Summary:** Added a PRE-FILING CODEBASE CHECK rule to the autonomous protocol: before filing a
new feature-request ticket (or reporting a missing capability), the agent must query the target
repository's codebase (grep, code search, or explore) to determine whether the requested behavior
already exists.  If the feature is already implemented — or a near equivalent is present — the
agent must inform the operator directly, cite the relevant file/symbol, and stop rather than
creating a ticket.

**Rationale:** Session e2c54bd977e840b0a710c5bb0aaaed5b filed a feature request for concatenating
queued messages without first checking whether the feature already existed.  The subsequent
monitor discovered that ``MessageCoalescer._process_batch`` in ``chat.py`` already implements the
desired behavior, producing a misleading monitor cycle and unnecessary ticket overhead.

**SHA256:** `f3893a7b34fde84c2ff9a50118dac079c51e87126346fd21cb3fd37d3f3d258d`

## AUTONOMOUS v17 — 2026-08-09 — do-not-output-raw-subsession-debug-summa-f3f1

**Summary:** Reinforced the COMPLETION closing-summary guidance so the HARD FILTERING RULE is
applied at the very end of a session: the assistant must never output raw subsession summary
blocks ('Subsession summaries:', '[id] kind=... status=...' bullet enumerations, or any
'kind=' / 'status=' fragments) and must strip all internal technical identifiers and
state-machine output, presenting only the synthesized, user-facing meaning.

**Rationale:** Session c0ff4ec1b46b4ba4bcc26390c19c3f21 appended a raw subsession summary block
(with kind=periodic status=closed internal details) to the user-facing reply at session end,
violating the consolidation rules' explicit prohibition on raw bullet enumerations of
[id] kind=... status=... lines.

**SHA256:** `5c07bf939e478102c6b22dd608fe998863e352d605d232cba668750fef681605`

## AUTONOMOUS v16 — 2026-08-09 — avoid-repeated-monitor-failures-by-not-r-6dec

**Summary:** Added a REPEATED-FAILURE STOP rule to the MONITOR LIFECYCLE MANAGEMENT section: if a
monitor creation attempt fails with a checkpoint-related error (e.g. an event-driven monitor that
loses its ticket_id and reports 'missing_ticket_id'), do not re-spawn the same monitor type —
switch to a periodic monitor (or reuse/pause an existing one) and file a fix ticket via
POST /tickets/ingest describing the root cause.

**Rationale:** Session c0ff4ec1b46b4ba4bcc26390c19c3f21 tried to set up an event-driven
(wait_for_event) monitor for ticket af6d three times; each attempt failed because the monitor's
checkpoint does not persist the ticket_id. The operator correctly observed the monitor did not
exist. The agent re-spawned the same broken monitor type instead of recognizing the pattern and
switching strategy or filing a fix ticket after the first failure.

**SHA256:** `5c16fabcc2ecd43056210ecf1d49d62ce4511c88a675bc7e6860fe74247d7f24`

## AUTONOMOUS v15 — 2026-08-09 — monitor-lifecycle-management-avoid-subse-990d

**Summary:** Added MONITOR LIFECYCLE MANAGEMENT section instructing the agent to minimize monitor
creation, reuse existing monitors, pause rather than replace, prefer polling over event-driven
monitors (known wait_for_event checkpoint bug causes premature auto-pause), and handle pool-full
errors by identifying freeable monitors or reporting the capacity issue to the operator.

**Rationale:** Session eee421b68059407a8c32673df5588ecd created multiple monitors (periodic,
event-driven, replacements) that auto-paused or failed, exhausting all 20 subsession pool slots.
The agent lacked explicit lifecycle guidance and treated monitors as disposable, spawning new ones
when pausing or reusing an existing monitor would have been correct.

**SHA256:** `94ef86c484aba4a81d654b04955f35fc2b36b4f9c1f60cf248e6f18bf5d70f84`

## AUTONOMOUS v14 — 2026-08-09 — provide-a-single-consolidated-outcome-su-1082

**Summary:** Add a consolidated closing-summary instruction to the COMPLETION
step of the autonomous protocol.  Before emitting the completion marker, the
assistant must now produce a single concise summary aggregating all session
outcomes — what was accomplished, what is still pending, and what the operator
needs to act on — without re-listing internal details (subsession reports,
monitor updates, CI verification tables) the operator has already seen.

**Rationale:** During session 2dfb02c3, the assistant produced multiple separate
outcome summaries (subsession reports, monitor updates, CI verification tables)
without consolidating them into a single closing picture.  The operator was left
to piece together the final state from fragmented messages.  This instruction
ensures every autonomous session closes with a single, operator-ready synthesis.

**SHA256:** `8f82d3b83c82cd073fd6e883cc80b58c97bd780b7484d770faaeebd39e810763`

## AUTONOMOUS v13 — 2026-08-13 — improve-diagnosis-of-persistent-git-clon-9b74

**Summary:** Add `CLONE / WORKSPACE FETCH FAILURE DIAGNOSIS` section after
`TICKET RESUMPTION DISCIPLINE`.  Teaches the assistant to categorise repo
fetch failures as transient (disk space, network timeouts, 5xx) or persistent
permission (403/404 with authenticated token, token exchange failures) BEFORE
retrying or resuming.  When a clone failure repeats across retries — even with
different proximate error messages — the assistant must flag it as a likely
GitHub App installation scope gap and prompt the operator to check the
installation scope, rather than proposing a different transient cause each cycle.

**Rationale:** During session b2841a7c, the assistant attributed a persistent
auto-mail repo clone failure to disk space (transient), but the root cause was
a GitHub App installation scope issue.  The assistant retried the clone across
multiple resume cycles, each time proposing a different transient diagnosis.
Explicit failure-categorisation rules prevent this misdiagnosis loop.

**SHA256:** `943c81892643f48c19c5469e9845a3a302dd48d46be49d2798dc40de7221f378`

## AUTONOMOUS v12 — 2026-08-09 — improve-conflict-detection-to-include-se-7d38

**Summary:** Add `SEMANTIC CONFLICT ANALYSIS` section to the `TICKET RESUMPTION
DISCIPLINE` block. Before resuming a stuck PR with merge conflicts, the assistant
must now distinguish between mechanical conflicts (adjacent-line changes in
different functions, import reordering) and semantic conflicts (both branches
modify the same function, class, or logical area in incompatible ways). Semantic
conflicts must be reported to the operator with a recommended resolution strategy
rather than blindly retried.

**Rationale:** During session f09916fc, the assistant identified a semantic
conflict in `_render_body()` that was not resolvable by hand-merge, but the
initial resume attempt did not detect this until after a second resolution
attempt. Adding a pre-resume semantic conflict check prevents the loop of blind
resumes and saves time.

**SHA256:** `16a7236dd8121646379165a6abf4abbf65153dd3b7c6bc9ed121abb7b9cfac0d`

## AUTONOMOUS v11 — 2026-08-07 — require-operator-approval-before-filing-5833

**Summary:** Add `OPTIMIZATION TICKETS — REQUIRED OPERATOR SIGN-OFF` section to
the autonomous protocol. Before filing any optimization ticket (cost reduction,
performance tuning, resource right-sizing), the assistant must first summarize
the proposed changes to the operator and ask for explicit sign-off. The ticket
must not be filed until the operator confirms. This prevents premature actions
and aligns with user expectations for control over optimization decisions.

**Rationale:** During session 5851f345, the assistant filed a cost-optimization
ticket directly without presenting the proposed improvements to the user for
approval. The user explicitly stated they wanted to approve improvements first.
The assistant acknowledged the issue and adjusted behavior, but the prompt
should enforce this as a required step.

**SHA256:** `a68c8aaba09a6ac8b4289f89285af576b298b974088c2d4522fe48587c4b38ea`

## AUTONOMOUS v10 — 2026-08-12 — track-ticket-resume-failures-before-re-7f2c

**Summary:** Add `TICKET RESUMPTION DISCIPLINE` section to the autonomous
protocol. Before resuming a blocked ticket via POST /tickets/{id}/resume-blocked,
the agent must check whether the ticket has already been auto-resumed and
blocked again with the same error. Repeatedly resuming a ticket with the same
underlying cause wastes operator attention and produces conflicting analyses —
each resume cycle may surface a different proximate symptom while the root cause
remains unaddressed. The new section instructs the agent to check ticket history
before resuming, decline re-resumption when the error pattern repeats, and
deliver a single consistent diagnosis rather than changing the root-cause
explanation across cycles.

**Rationale:** The assistant resumed a chat-access ticket during disk-space
recovery, but the ticket had already been auto-resumed and failed again with
the same clone error — leading to a contradiction where the recovery summary
claimed disk space while a subsequent priority task identified a GitHub App
scope issue. Tracking prior resume outcomes prevents conflicting analyses and
operator confusion.

**SHA256:** `4ae7c091f9840dc2c485cabed2224247db84b0978a34ffce3ef24cbfc73f1ef4`

## AUTONOMOUS v9 — 2026-08-11 — add-autonomous-approval-capability-for-r-afd3

**Summary:** Add `auto_approve_routine_secret_provisioning` autonomy setting and
`AUTO-APPROVAL RULES — ROUTINE SECRET PROVISIONING` section to the autonomous
protocol. When enabled, the agent may auto-approve routine secret provisioning
tickets (Langfuse credentials, API keys, token rotations) even when they touch
security-sensitive paths, provided the change has no code modifications, no
destructive operations, and is limited to credential/secret/token provisioning.
The `SECURITY-SENSITIVE APPROVAL` section now includes an explicit exception
for routine secret provisioning when the setting is on. When auto-approving
under this rule, the agent must include a brief system notice in its
operator-facing message.

**Rationale:** The agent repeatedly deferred to the operator for approval of
routine Langfuse-credential provisioning tickets even though the change was
standard (no code changes, no destructive operations) and the agent's own
recommendation was to approve. The operator explicitly prioritized the ticket
but still had to be interrupted. Adding a configurable threshold lets operators
opt in to automatic approval of routine secret provisioning, reducing
unnecessary interruptions while keeping genuinely risky security changes gated.

**SHA256:** `418f1602544aae4360db02490e32f233b66beae66a1e62ca2382bbaf914971cb`

## AUTONOMOUS v8 — 2026-08-12 — approval-prompts-should-avoid-re-stating-794a

**Summary:** Add `ACTIVE-CONTEXT SHORTENING` guidance to the
`HUMAN_ISSUE_APPROVAL` section. When the operator is already engaged in the
current conversation about a ticket being surfaced for approval, the assistant
must provide a concise prompt stating only the decision needed (e.g. "Approve
the Docker digest fix (a950)?" or "Close the stale triage ticket (b123)?")
without re-summarizing the ticket's full background. Full detail is reserved
for tickets the operator has not seen in the current conversation.

**Rationale:** A monitor re-summarized an entire ticket's history (why needed,
low risk, why approve) when the operator had just been told about the issue
and was waiting for a decision prompt. Repeated re-explanation of already-shared
context wastes the operator's attention and clutters the conversation.

**SHA256:** `b6cf7df288a8a445842d5c2e12b6b87829f444717bfd15fe407e1face08c9f63`

## AUTONOMOUS v7 — 2026-08-11 — assistant-cannot-explain-how-to-configur-78b0

**Summary:** Add `OPERATOR CONFIGURATION GUIDANCE` section to the autonomous
protocol. When the operator asks how to configure autonomous sessions, the
assistant now has baked-in instructions: point the operator to the chat UI
settings panel (⚙ Settings → autonomous section) or the server's JSON config
file, list the relevant config keys (`name`, `prompt`, `trigger_type`,
`trigger_interval_seconds`, `enabled`), give an example JSON snippet, and
mention the inspection/trigger API endpoints (`GET /autonomous/definitions`,
`POST /autonomous/definitions/{name}/run`). The section also reminds the
assistant that modifying config is a mutation requiring authorization.

**Rationale:** The operator asked how to configure autonomous sessions from
the settings panel, but the assistant had no documented guidance and could
not access the live config due to a DNS failure. Baking the configuration
instructions into the autonomous protocol lets the assistant answer this
question offline, without depending on live service availability.

**SHA256:** `15668a76a3f8f1acae044a61ae35c61a5f552779a16d20aaa404df8448dfa5aa`

## AUTONOMOUS v6 — 2026-08-07 — incorporate-ui-design-patterns-when-prop-ef53

**Summary:** Add `UI-RELATED TICKETS` guidance to the PLAN DRAFTING step (2).
When a ticket subject involves UI changes, the agent must first check the
target repo for project-specific design documentation, component libraries,
or established UI patterns before making recommendations.  If no design
guidelines are discoverable, the agent must note that explicitly.  When
proposing UI changes, the agent must mention relevant trade-offs
(discoverability vs. visual clutter, flexibility vs. consistency, aesthetic
appeal vs. accessibility) so the user can make an informed decision grounded
in project constraints.

**Rationale:** The agent agreed with a user's suggestion to reduce add-account
entry points without consulting any project-specific UI design guidelines or
mentioning trade-offs, producing a generic recommendation blind to existing
conventions.  The new guidance ensures UI advice is grounded in repo-specific
patterns and accompanied by trade-off analysis.

**SHA256:** `9b7e01a95c60fe60ab5205864ac2b82206172da8184e598afba92ed865e4dee3`

## AUTONOMOUS v5 — 2026-08-02 — require-empirical-verification-before-es-8208

**Summary:** Add `INFRASTRUCTURE DIAGNOSIS — EMPIRICAL VERIFICATION REQUIRED` section
to the autonomous protocol. When background agents detect that infrastructure credentials
appear missing or service state is 'unknown', they must perform at least one empirical
verification step (check actual ticket block reasons, test a resume canary, verify service
status through a dedicated health endpoint, or cross-reference recent ticket history) before
escalating to the operator. The section explicitly documents the empty env-store snapshot
as a known red herring — credentials may be wired through a different path.

**Rationale:** Multiple subsessions reported a total credential wipe based solely on reading
the mill's env store (showing only DOCKER_HOST), and the assistant initially accepted that
diagnosis before disproving it via live state checks. Requiring empirical verification
before escalation reduces false alarms and prevents wasted operator attention.

**SHA256:** `899229c5eb7619becec08cdbf094fe28d748356683bb728f47a397b7e1b3ac5f`

## AUTONOMOUS v4 — 2026-08-09 — reduce-operator-interruptions-auto-appro-b90b

**Summary:** Add AUTONOMY TIER section to the autonomous protocol appendix.
When `autonomy.auto_approve_self_authored` is enabled, the agent may auto-approve
self-authored `human_issue_approval` tickets for repos in the allowlist, provided
the change is non-destructive and no non-negotiable gate is triggered.  The
non-negotiable gate list (security-sensitive paths, deletions, broad-blast-radius
changes, non-allowlisted repos, unverifiable safety) is enumerated explicitly.
When `autonomy.suppress_no_change_monitors` is enabled, no-change monitor outcomes
are omitted from operator-facing turns.  The HUMAN_ISSUE_APPROVAL section is
updated to reference the AUTONOMY TIER as a third auto-approval path alongside
user-requested tickets and explicit operator authorization.

**Rationale:** The operator is interrupted too often for mechanical decisions.
The autonomy tier provides a config-driven, documented exception to the
confirmation gate, with hard non-negotiable safety boundaries that apply even
at the highest tier.

**SHA256:** `e5d89e6cc0a2d488875f2c6ca36b68865b2f6521dff0cb89f9fcda6b0ccfdece`

## AUTONOMOUS v3 — 2026-08-02 — auto-prioritize-user-requested-tickets-46db

**Summary:** Add user-requested-ticket exceptions to the autonomous MUTATION AUTHORIZATION rules.
The autonomous protocol now acknowledges that tickets filed at the operator's explicit instruction
(`kind: user-request`, `priority: high`) are pre-authorized for both filing and approval — the
operator's filing request constitutes consent. The exceptions are added to the 'Filing new tickets'
and 'Approving human_issue_approval tickets' restriction lists, and to the HUMAN_ISSUE_APPROVAL
consent-scoping paragraph.

**Rationale:** The autonomous MUTATION AUTHORIZATION rules previously treated all ticket filing
and approval as restricted without considering the operator's explicit intent. User-requested
tickets filed at the operator's direct instruction should be auto-approved in the same turn
without waiting for a separate gate-level consent cycle.

**SHA256:** `7cad12ab915384decd5f32a72db8c727ff90b81bbfd44f6aea9f29e0b91201a0`

## AUTONOMOUS v2 — 2026-07-31 — require-explicit-user-confirmation-for-s-6503

**Summary:** Add `SECURITY-SENSITIVE APPROVAL` section to the autonomous protocol.
Security-sensitive tickets (credential changes, password resets, .htpasswd modifications,
API key rotations, authentication/authorization changes) must receive explicit per-ticket
operator confirmation before being transitioned out of `human_issue_approval`.  Broader
consent or standing directives do not cover security-sensitive tickets — even when the
operator requested the work that produced the ticket, the approval gate requires its own
explicit authorization.

**Rationale:** The assistant auto-approved security-sensitive tickets (e.g. admin password
reset, placeholder hash replacement) without separate per-ticket confirmation.  The new
section closes this gap by requiring the assistant to flag security-sensitive tickets
explicitly, explain why they are security-sensitive, and obtain per-ticket confirmation
from the operator before transitioning them.

**SHA256:** `b9fb86c6b2ca619cea0ef00eadc91562bcd5e5531be4461beccd6b90ea282a2c`

## AUTONOMOUS v1 — 2026-08-04 — extend-system-prompt-governance-to-the-a-e556

**Summary:** Baseline — the current `build_autonomous_instruction()` output as of post-#1165
(CONDITIONAL AUTHORIZATION block added in commit 3434b8f) and post-#1157 (PRE-AUTHORIZED
ESCALATION block). This entry establishes governance coverage for the autonomous appendix,
which previously had no versioning, SHA pinning, or changelog record despite two prompt edits
shipping without bumps.

**Rationale:** The autonomous appendix is appended to the governed `agent_instruction` at
runtime (cli.py:436) but sat outside the existing `SYSTEM_PROMPT_VERSION` / SHA256 / changelog
governance, so edits to it landed silently with no CI signal. This entry locks in the known-good
state and enables future drift detection.

**SHA256:** `9d428fa0104b04c956f19c370079500cf680c9a2335967cf7c9f5dd86988a29d`

______________________________________________________________________

### Governance policy

Every change to the `build_autonomous_instruction()` return text in
`src/robotsix_chat/autonomous/prompts.py` **MUST**:

1. **Bump** `AUTONOMOUS_PROMPT_VERSION` to the next integer.
1. **Add a new entry** at the top of this section (reverse-chronological, newest first) with the
   header `## AUTONOMOUS v<N> — <YYYY-MM-DD> — <ticket-id>`.
1. **Record the SHA256** of the new output (computed as
   `hashlib.sha256(build_autonomous_instruction(Settings()).encode()).hexdigest()`) in the entry.

A CI test (`tests/config/test_system_prompt_governance.py`) enforces that the latest entry's version
matches `AUTONOMOUS_PROMPT_VERSION` and its recorded hash matches the live output — edits that skip
this file **will fail CI**.
