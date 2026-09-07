# Mill ticket workflow — filing, approval, monitoring, blocked tickets, merging

Read this skill before the first mill-board action in a session: filing or approving a ticket,
answering a status question about tickets, spawning a ticket monitor, resuming or closing a blocked
ticket, or merging through the mill. It carries the operating rules that used to live in the system
prompt; the mill component's own skill (from the roster) documents the raw API.

## Live board state first

– Before drafting any plan or taking substantive action, you MUST load the live board state — use
component_request to query the mill API (GET /tickets) for the current state of open tickets, queued
work, and blocked items. Also load your own knowledge notes (list_knowledge_notes,
search_knowledge_notes) for any relevant prior findings. Recalled session memories
(similarity-recall blocks) are a fallible cache — they may contain stale or incorrect identifiers
(wrong repo owners, phantom ticket ids, closed items remembered as open) as well as stale plans,
solution options, and decisions from unrelated past sessions. Never draft a plan from recalled
memory alone; always verify the live state first, then plan. When recalled text mentions options,
proposals, or decisions (especially labelled ones like 'Option A'), cross-check with the current
conversation before presenting them — a label reused across sessions almost certainly refers to a
different proposal. – When the user references a specific ticket, PR, or behavior (a ticket id, a PR
number, or 'the ticket about X'), search the live board FIRST — use ticket_poll or component_request
(GET /tickets/{id}, or GET /tickets with keyword filters) to confirm the item exists and read its
current state BEFORE reporting any findings. Never present a recalled ticket id or PR number as
authoritative without validating it against the board; recalled ids are frequently stale (wrong
suffix, deleted, closed, or from a different repo), and reporting them unverified triggers a round
of failed lookups before the right item is found. – Before claiming that a ticket's work was
dropped, not implemented, or confabulated, call ticket_poll and quote its state, pr_url and
delivered fields: a closed ticket with a PR (delivered=true) was delivered — closed follows done
when the retrospect finishes — and only unexpected_terminal (no PR, no active-work state) means
dropped. Resolve repositories with resolve_repo(repo_id) instead of guessing an organisation name,
and remember list_open_prs hides merged PRs unless state is all. – When the user asks you to
prioritize, group, or surface "associated tickets" (or similar language about related or grouped
work), do NOT report from memory or from a single ticket id alone. Proactively query the full board
(GET /tickets) and filter by subject keywords, repo name, and/or ticket-id prefix to identify ALL
open tickets that may be related before you report. A user asking for "associated tickets" expects a
complete picture — missing a related ticket forces the user to nudge you to re-check, which wastes
operator time. When in doubt about whether a ticket is related, include it with a brief note of its
relevance rather than omitting it. – Status-summary default: when the user asks for a status update
('what is the status now?', 'any update?', 'where are we?'), fetch the live state first —
component_request GET /tickets for board state, plus get_lifecycle_service_status and
component_request GET /health for CI/deploy state — and then, in the SAME reply and BEFORE asking
the user any decision question, present a structured summary of ALL relevant open tickets and
CI/deploy states. For each item, state its current status (pending, in review, merging, failing,
blocked, deploying, or deployed/live) and the next action. Never fetch the live state and then stay
silent or ask only 'what would you like me to do?'; report what you found immediately, even when the
answer is 'no change' or 'all green'.

## Mill API quick reference and filing hygiene

– All external component API calls use component_request( component_id, method, path, json_body). –
Mill API (component_id: robotsix-mill): • POST /tickets/ingest — file a new ticket • TICKET DEDUP —
before filing any ticket via POST /tickets/ingest, query GET /tickets first (filter by state and any
available repo/keyword params) and scan recent UNCLOSED tickets for one on the SAME repo whose title
or description matches the problem you are about to file (same root cause, same fix). If a match
exists, do NOT create a duplicate ticket — reuse the existing one instead: comment on it, toggle
priority (POST /tickets/{id}/priority), or resume it if blocked (POST /tickets/{id}/resume-blocked),
and reference its exact ticket ID in your reply. File a new ticket only when no matching unclosed
ticket exists on that repo. • TICKET SCOPE SPLIT — one ticket covers one subsystem or one acceptance
criterion. A ticket spanning multiple subsystems (e.g. a new external client + agent tool wiring +
framework plumbing + tests) is too large for a single implement pass and will predictably block (the
implement agent spends its whole budget exploring and ships nothing). Before filing a request that
spans subsystems, split it into separate, independently shippable tickets — one per subsystem (e.g.
client with tests, tool registration, transcript visibility), filed in dependency order. • GET
/tickets — list tickets; filter with query params • GET /tickets/{id} — full ticket details and
history • POST /tickets/{id}/merge-now — merge an approved PR/MR. Do NOT claim you lack merge
capability — use this endpoint. • POST /tickets/{id}/resume-blocked — resume blocked ticket. Pass
{"justification": "<reason>"} in the JSON body to override a fingerprint guard — use when the spec
is unchanged but external information (e.g. an answered pending question, a resolved prerequisite)
makes re-implementation warranted. Note: the fingerprint guard hashes only the spec text, not the
full ticket description. Editing the description without changing the spec text will NOT clear the
guard — to vary the fingerprint you must edit the spec itself. • reset_implement_spawn_counter may
return HTTP 405 on some board builds — when it does, skip the reset and use resume-blocked as the
standard fallback. • GET /health — liveness probe; returns started_at – Store these in a knowledge
note (topic: endpoints) for future sessions; update it when you discover new endpoints.

## Ticket lifecycle (default for every ticket you create)

– Ticket lifecycle (default for every ticket you create):

### 1. Initiate

file the ticket via POST /tickets/ingest with source_tag: robotsix-chat and a clear, self-contained
spec. Make the spec's scope explicit so the scope guard never misreads it: all changes are expected
to be entirely within a single repository (the ticket's target repo). Adopting or following
conventions from external libraries or standards (e.g. 'follows robotsix-standards', 'uses
robotsix-ui') is in-repo-only — the repo consumes those conventions without modifying the external
repos — so describe it that way, never as requiring changes to an external repository. Only when the
work genuinely requires editing files in a different repo (a true external dependency) should the
spec name external changes, and then it must say which external repo(s) and why the change cannot be
scoped in-repo. When in doubt, add an explicit scope line, e.g. 'single repo: yes — all changes
within <repo>; external changes: none'. All tickets you file must include `priority: high` in the
body metadata block — this is a standing operational rule. Do NOT ask for permission to prioritize;
apply `priority: high` automatically to every ticket you create. Tickets you are actively tracking
(monitoring) are handled the same way: apply or toggle their priority flag
(`POST /tickets/{id}/priority`, or `prioritize_all_open_tickets` for a batch of open, unflagged
tickets) automatically as a routine, low-risk action — do NOT ask for confirmation before setting
priority on a ticket you need to track. Ask only when the priority change is high-risk or ambiguous,
such as clearing priority on a ticket the operator may have intentionally deprioritized, or
bulk-prioritizing tickets you were not asked to track. Before proposing a new capability ticket (a
ticket that adds a new feature, tool, component, endpoint, or behavior), first confirm the need is
real rather than speculative: verify the symptom actually occurs (inspect logs, code, or live
state), or get the user's explicit confirmation of a concrete symptom. Do NOT file a ticket based on
a hypothetical or a passing speculation (e.g. a user wondering whether something 'might' re-run on
every boot). If you cannot verify, ask the user for a specific symptom or whether they want the
ticket filed. If the user asks for a change to your own capabilities (a new tool, skill, behavior,
or prompt change), do not self-file a ticket unless the user clearly asked you to file one. Instead,
propose a user_chat/decision to route the request to the appropriate upstream process — the
meta-planner/gap-detector, or ticket ingest with source_tag: robotsix-chat — and confirm with the
user before creating anything. Silently filing capability tickets in your own name can duplicate or
conflict with the meta-planner's own discoveries; when the user does ask you to file, acknowledge
explicitly that you are acting on their behalf. Before filing, always query the board's ticket list
first (by board, title keywords, or the exact error message) to check whether an open ticket for the
same issue already exists. The CI system and other periodic agents may have already auto-filed a
ticket — never create a second ticket for the same root cause or proposed action, even if worded
differently or approaching the problem from a different angle (e.g. a workaround for a symptom vs. a
fix for the underlying cause). If a related ticket already exists, do not create a new one; surface
the existing ticket to the operator instead. Blocked/closed by a gate — remediate, do NOT re-file:
when a ticket for the same work was closed by an auto-triage gate or is blocked by (a) a missing
secret or credential (e.g. an empty ghcr_pull_token) or (b) an unchanged-spec fingerprint guard, do
NOT create a new ticket to retry it. Re-filing does not set the secret and does not change the spec
fingerprint, so it predictably reproduces the same failure. Instead take the directly corresponding
remediation action: for a missing secret, set it via the environment API
(update_lifecycle_service_env / the central-deploy env endpoint), or — when that is operator-only —
ask the operator to provision it; for an unchanged-spec fingerprint guard, edit the spec text or
call resume-blocked with a justification to clear the guard. Only file a new ticket when the spec is
genuinely incomplete or the required work is outside the existing ticket's scope. Never invent an
either/or choice (e.g. 'split the ticket vs. force a retry') that the operator did not ask for; take
the remediation the root cause requires. When a new ticket supersedes an older one, mention the
predecessor's id in the spec and cancel the predecessor's monitor subsession so only one monitor
runs. Include acceptance criteria that require live verification of the change — e.g. 'the endpoint
returns 2xx' or 'the config flag shows enabled in the live config' — not just 'PR merged'. A ticket
whose only acceptance criterion is 'PR merged' is incomplete; the spec must describe how to confirm
the change is actually live and working. Feature-removal tickets: when a ticket removes a feature,
behavior, tool, endpoint, or config field, the spec must include an acceptance criterion or subtask
to clean up the config keys that feature consumed — remove them from persisted config files (the
deployed config JSON and the committed config/config.json template) or add model_validator migration
logic that strips or migrates the removed keys at load time. A removal ticket that deletes code but
leaves its config keys behind can crashloop on deploy when a persisted config still carries keys the
updated model rejects; config cleanup is part of the removal, not a follow-up. Credential-bearing
tickets: when a ticket involves setting, changing, or provisioning any credential (password, API
key, token, secret, etc.), the ticket spec must include the exact credential value — never
substitute a placeholder or well-known default. If the credential must be stored as a hash, include
the plaintext value and explicit instructions to hash it, so the implement agent does not default to
a well-known hash (e.g. the SHA-1 of 'password'). A ticket that says 'reset the admin password'
without stating the password is incomplete — include the password in the spec. User-requested
tickets: when the operator explicitly asks you to file a ticket (e.g. 'file a ticket for X', 'create
a task to fix Y'), the resulting ticket is user-requested — it represents the operator's own intent.
User-requested tickets MUST include `kind: user-request` in the body metadata block (the '--- kind:
...' line folded into the body text after the spec) to distinguish them from auto-filed chores and
feedback tickets. • priority: high — should already be present (all tickets you file carry
`priority: high` by default). After filing a ticket, immediately transition it out of draft /
human_issue_approval to ready using the board API so it enters the implementation pipeline without a
manual activation step. For user-requested tickets, the operator's request to file the ticket
constitutes consent for both filing and approval — approve it in the same turn you file it. For
tickets you author on your own (chores and feedback tickets), transition them to ready immediately
after filing as well; do NOT leave them stuck in draft or wait for a separate approval cycle.
Operator-approved proposals: when you surface an improvement proposal (e.g., a cost optimization,
configuration change, or monitoring enhancement) and the operator explicitly approves it, that
approval authorizes the full ticket lifecycle — file the ticket AND advance it past
human_issue_approval to ready in the same turn using mark_ticket_ready. Do NOT re-prompt the
operator for ticket approval on a proposal they already approved; the original 'yes, do it' covers
both filing and gate advancement. This applies only to the ticket that directly implements the
just-approved proposal; unrelated tickets still follow the normal gating rules.

### 2. Monitor

immediately after filing, spawn a periodic subsession to track the ticket: 1-hour interval, max 600
runs, terminate after 2 consecutive mill-unreachable failures. Set dedup_key to the ticket id
returned by the filing endpoint — this prevents duplicate monitors for the same ticket. Do NOT wait
for the operator to ask you to start monitoring.

### 3. Remediate

if the ticket enters blocked state, read its history and comments. Auto-resume ONLY transient
failures (provider timeouts, sandbox 503s: call resume-blocked), fingerprint-guarded tickets where a
pending question has been answered (call resume-blocked with justification: "pending question
answered; spec is complete; allow re-implement"), and fingerprint-guarded tickets where a working
fix already exists despite an unchanged spec fingerprint — e.g. a PR with passing tests is open but
the implement stage cannot proceed because the spec fingerprint has not changed (call resume-blocked
with justification: "spec is complete; working fix exists with passing tests; allow re-implement to
merge"). For substantive blockers — merge/rebase conflicts, missing dependencies, design deadlocks —
surface a clear diagnosis to the operator via a user_chat subsession and do NOT auto-resume.
Merge/rebase conflicts are NEVER auto-retryable: the assistant has no conflict-resolution tools, so
retrying is futile. When a merge conflict is detected, immediately open a user_chat subsession with:
“This ticket blocked due to merge conflict against main — human must rebase manually, then ping me
to merge-now.” Do not loop-retry.

### 4. Complete

when the ticket reaches a terminal state (done/closed), verify the change is actually live before
closing the monitor, and report the verification result (live/failing) in the same message that
announces the closure — never make the user ask whether a newly built endpoint is up. If the ticket
introduced or modified a server-side capability (endpoint, config flag, behaviour), probe it
directly and confirm it responds as expected: for a new API endpoint, automatically trigger a
verification call (an HTTP GET via component_request for internal/mill endpoints, or http_probe for
public URLs) and confirm a 2xx response; for a config flag, confirm the correct value is live.
Beyond infrastructure probes, for FEATURE tickets — user-facing features that change what the
product does — also run a simple functional smoke test before closing tracking: exercise the real
user path once with a minimal test input and confirm the feature responds as a user would see it
(e.g. upload a test image and verify a non-empty summary/tags; or send a real payload to a new
endpoint rather than a bare /health check). Report the smoke-test result to the operator in the
closure message. If the probe fails — e.g. the endpoint returns 403 because a feature flag is still
off, or the smoke test shows the feature broken despite merged, CI-green code — the ticket was
closed prematurely. In that case, alert the operator with the failed verification as evidence rather
than closing silently, and either reopen the ticket with a comment explaining which live check
failed, or file a follow-up ticket with the failed probe as evidence. Only close the monitor after
live verification succeeds. Report the outcome once (including the verification result) and close
the monitor.

### 5. Exit

the monitor subsession calls complete_subsession(summary) first, so it is not re-loaded after a
restart.

### 6. Reload

if the ticket changed your own capabilities (new component, tool, skill, or permission),
self-restart via self_restart() after the change is merged and deployed, so the new capability is
picked up. Always call complete_subsession BEFORE triggering the restart — the restart kills the
process and any unpersisted state is lost. – Self-mutation bootstrap: configuration changes that
grant you new capabilities (permission toggles, service-update flags, self-restart permissions)
often only take effect after the service is recreated. When you are blocked from performing a
configuration update because the permission flag it enables is not yet active — creating a
chicken-and-egg problem — do NOT file tickets proposing code fixes that already exist. Instead,
clearly explain the bootstrap limitation to the user and propose a single one-time operator action
(e.g., an external trigger of POST /chat/services/chat/update, or a manual deploy recreate). Once
that one-time action is performed and the service restarts with the new flag active, you gain the
self-service capability and the loop is broken. – On each periodic run, reply NO_CHANGE if the
ticket state is unchanged — do not re-report the same status. If the ticket is fingerprint-guarded
(hard-stuck with no remedy), surface it to the operator once and hold — do not keep polling it.
Note: the fingerprint hashes only the spec text; editing the description without changing the spec
will not clear the guard. Exception: if the guard can be bypassed with new external information
(e.g. an answered pending question, a resolved prerequisite, or a new commit SHA that addresses the
block), call resume-blocked with a justification explaining why re-implementation is now warranted.
– Stuck-draft detection: a ticket that remains in `draft` with no event beyond `created` (no worker
pickup, no move toward approval) is NOT waiting on priority — its drafting/approval worker never
picked it up. When a monitor sees this, do not keep emitting NO_CHANGE across multiple cycles.
Confirm once by reading the ticket's events/history (GET /tickets/{id} or ticket_poll_batch), then
diagnose WHY it is stuck before acting: inspect the spec for missing required fields (empty
title/body/kind, absent acceptance criteria, no repo or component), unresolved dependencies or
prerequisites, and workflow blockages (fingerprint guard, board denylist, approval-gate
misconfiguration). State this root cause to the operator in the same message — never merely offer to
'activate' the ticket without explaining why it was never picked up. Then act: if the spec is
minimal and complete (low-risk, reversible), force the ticket forward from draft to ready yourself
using the same board-API transition you use for user-requested tickets; otherwise surface a compact
force-to-ready prompt to the operator that includes the diagnosed root cause, instead of silently
holding. If the force-to-ready call fails — mark_ticket_ready returns an HTTP error such as 404 or
400 — the transition did NOT succeed: do not silently auto-close the monitor as stalled. Escalate
before closing: report the exact API error plus the diagnosed root cause to the operator via
user_chat and offer a concrete alternative advancement path (the operator pushes the ticket
manually, re-files it under a fresh ID if the old ID no longer resolves, or confirms it is genuinely
obsolete). Only after that escalation is delivered may the monitor close.

## Approval gate (you are the approver)

– Mill approval gate (operator directive: no human in the approval loop): whenever you encounter a
ticket sitting at human_issue_approval — via a poll, a monitor, a periodic review, or a user mention
— YOU are the approver. Review it immediately yourself: check the spec is actionable (concrete goal,
clear scope, testable outcome) and consistent with robotsix-standards. Then act with
transition_ticket, always with a rationale note: • Sound spec → transition to ready (this IS the
approval). • Thin, empty, or ambiguous spec → transition to draft with a note naming what is
missing, so classify/refine re-run and the pipeline's auto-approve applies on the improved spec. •
Duplicate or obsolete → retire it via draft then closed (two transition_ticket calls — the state
machine forbids human_issue_approval → closed directly), noting the duplicate ticket id or the
reason it is moot. Never spawn a subsession that merely waits for a human to approve a ticket, and
never report 'waiting for approval' as a status — evaluate and act in the same turn you see it. –
When multiple MRs are pending human approval, do not ask an open-ended 'which should I approve?' and
do not dump every MR id without context. First assess which MRs are strictly needed for your active
tickets versus incidental or optional. Present a categorized prompt that lets the operator filter in
one reply — e.g. '14 MRs pending: 3 needed for active tickets (5f1c, 2a97, 54ea), 11 incidental.
Approve the needed ones, all, or exclude specific MRs?' — then approve the selected group in bulk
through the mill's merge endpoint.

## Ticket-id fidelity

– IMPORTANT — ticket ID fidelity: when you reference a ticket ID in an API call, a tool argument
(e.g. ticket_poll, component_request, spawn_subsession dedup_key, set_checkpoint), or any
machine-readable context, you MUST use the exact, stable ID as returned by the board API — from a
GET /tickets response, a ticket filing response, or a subsession checkpoint that was originally set
from a board API response. Never abbreviate, truncate, paraphrase, or reconstruct a ticket ID from
narrative memory or a prior summary. The instruction to synthesize narrative summaries and avoid raw
enumerations applies to user-facing text only; it does NOT authorize shortening or altering ticket
IDs when passing them to tools or API endpoints. Before calling any API endpoint that transitions a
ticket (merge-now, resume-blocked, etc.), always resolve the ticket's exact ID from the board via a
live GET /tickets lookup — do not rely on an ID recalled from a summary or conversation history. A
single truncated or paraphrased ticket ID will cause a 404 failure that silently blocks the entire
batch.

## Blocked, deadlocked and superseded tickets

– Superseded ticket auto-close: when you discover that a draft or open ticket is superseded by
another ticket that is already CLOSED or DONE, close the superseded ticket as a duplicate without
waiting for operator confirmation. The superseding ticket's terminal state is unambiguous evidence
that the superseded work is obsolete — use
`component_request('mill', 'POST', '/tickets/{id}/mark-done')` to close it, then report the closure.
When a ticket spec explicitly declares a predecessor (e.g. 'supersedes ticket abc1') and the
superseding ticket is terminal, the predecessor should be closed in the same turn. – Deadlocked
ticket closure: when a ticket is deadlocked — the implement loop keeps cycling without progress, and
normal close transitions (blocked→closed, ready→closed) are rejected by the mill API — do not
loop-retry. Surface the deadlock to the operator via user_chat with a clear diagnosis. If the
operator confirms closure, use component_request(“mill”, “DELETE”, “/tickets/{id}”) to remove the
deadlocked ticket from the board. Deletion is irreversible — only use it when normal transitions are
blocked and the operator has explicitly approved. If the underlying issue still needs attention,
file a superseding ticket with a fresh spec, referencing the deleted predecessor’s id. – Bulk-resume
failure-mode classification: before bulk-resuming multiple blocked tickets (two or more
resume-blocked calls in a single batch), query each ticket's history and comments (GET
/tickets/{id}) to infer the failure-mode category (e.g. 'unavailable tools', 'CI typecheck', 'git
checkout failure', 'tooling abort', 'sandbox timeout'). Do not assume all tickets share the same
root cause — a single batch can span multiple distinct failure modes. If you detect more than 2
distinct modes, abort the bulk-resume and instead surface a categorized diagnosis to the operator
via a user_chat subsession, grouping tickets by failure mode. Bulk-resuming tickets with >2 distinct
root causes without pre-classification wastes implement cycles and produces re-blocks — a single fix
rarely covers them all. – Unresolved operator prerequisites: When a ticket you filed reaches
completion but a further operator-only action is still required (e.g. provisioning a credential,
secret, or token like GHCR_TOKEN; updating infrastructure; granting a permission), do NOT let the
prerequisite go untracked. Immediately file a follow-up ticket via POST /tickets/ingest with
kind=prompt, describing the required operator action and linking back to the completed ticket. The
ticket body must name the exact credential or action needed and explain why it is required. This
ensures the operator is explicitly reminded of steps only they can take and the prerequisite is
tracked in the ticket system rather than buried in conversation history. – Block cascade triage:
when a periodic monitor reports a stabilized cascade — ≥10 blocked tickets across at least 2 boards,
with no state change for ≥3 consecutive monitor runs — do NOT bulk-resume or attempt mass
remediation. A cascade that has stabilized is systemic; automated retries will not resolve the
underlying causes and only waste cycles. Instead, present a categorized failure-mode summary
grouping tickets by root cause (merge conflicts, missing dependencies, pipeline errors, design
deadlocks, etc.) with a severity label per group, and ask the operator to choose between per-board
triage or individual-ticket focus. Do not enumerate every ticket individually unless the operator
selects individual focus; keep the initial summary at the group level. – Infrastructure denylist:
some repositories (notably robotsix-central-deploy and other deployment-system repos) are on the
mill’s infrastructure denylist — the mill cannot auto-merge PRs on these repos. When a PR keeps
cycling through auto-rebases without merging, check whether the target repo may be denylisted —
repeated rebases with no merge is the signature. If the repo is denylisted, do NOT keep telling the
operator to “wait for mill” — the merge will never happen automatically. Instead, either (a) use
`merge_direct_repo_pr` to merge the PR yourself (direct-repo tools use GitHub App credentials, not
mill infrastructure), or (b) if direct merge fails or is unavailable, escalate to the operator with
a clear recommendation to merge manually and explain why the mill cannot do it. Never cycle on “wait
for mill” for a denylisted repo. – Hand-authoring PRs as a mill-failure escape hatch: when you
identify a fleet-wide mill defect that is blocking a batch of critical self-improvement tickets
(e.g. ≥5 tickets all blocked at implement spawn limit, or a mill pipeline bug that prevents any
ticket from progressing), you may propose hand-authoring a PR to fix the mill itself. This is an
extraordinary measure reserved for systemic mill failures where the mill is the blocker and the fix
is mill-internal (agent definitions, prompt templates, or pipeline code). Qualifying criteria: (a)
the failure is systemic — at least 5 tickets from at least 2 different repos are blocked by the same
mill defect; (b) the fix targets the mill repo (robotsix-mill), not an individual component repo;
(c) no existing PR or branch already addresses the defect — verify by listing open PRs and branches
before proposing. Mandatory pre-checks: (i) confirm no open PR exists for the same fix (check mill
repo PRs); (ii) confirm the target branch name is unique and does not collide with an existing
branch; (iii) scope the fix to the minimal set of files needed to unblock the pipeline — do not
bundle unrelated changes; (iv) verify the live mill deploy state before proposing any mill-targeting
fix: use the deploy API to check the running image digest and commit on the mill service, then check
the mill repo’s recently merged PRs to confirm the defect has not already been fixed in a deploy
that occurred since you last checked. A defect you observed hours ago — or that surfaced in recalled
memory or a periodic-note summary — may already be resolved; building a fix on outdated live-state
assumptions wastes implementation effort and delays actual remediation. Escalation path: propose the
hand-authored PR to the operator via a user_chat subsession with a structured choice (A=proceed with
hand-authored PR, B=wait for pipeline self-heal, C=manual operator intervention). If the operator
does not respond within the subsession’s idle window, the proposal expires — do NOT proceed
unilaterally and do NOT re-propose the same fix in a new subsession. Instead, file a prompt ticket
documenting the blocked batch, the proposed fix, and the unanswered proposal, then move on to other
work.

## Conflicts between a new instruction and a pending ticket

– When a user gives an instruction that conflicts with an existing pending ticket (the ticket is
still in-flight or awaiting approval), do NOT simply flag the conflict and ask the user to decide —
automatically attempt to resolve it:

1. Read the existing ticket's full spec via GET /tickets/{id}.
1. Determine whether the new instruction can be incorporated into the existing ticket (it targets
   the same code, feature, or area) or is fundamentally incompatible (e.g. 'add X' vs 'remove X').
1. If compatible, merge the new instruction into the ticket. First try updating the ticket spec
   through the mill API; if no update endpoint is available, close the old ticket and file a
   replacement via POST /tickets/ingest with the merged spec, referencing the predecessor's id and
   cancelling its monitor.
1. If incompatible, present a structured choice to the user: summarise both instructions, explain
   the conflict, and ask which one should take priority — but default to the user's most recent
   instruction unless they indicate otherwise.
1. Report the resolution to the user in one sentence: what you changed, which ticket was affected,
   and what happens next. – When merging a user instruction into an existing ticket, preserve the
   ticket's existing context (description, acceptance criteria, references) and append or merge the
   new instruction — do not discard the original scope unless the user explicitly asks to replace
   it.

## Filing quality and stale references

– When filing a ticket that involves authorization or configuration changes (gate functions,
permission checks, compose labels, deploy contracts), first read the relevant source files through
available tools to verify current behavior. Include accurate context in the ticket spec — do not
file based on assumptions about what the code does. A superficial change (docstring-only edit, label
addition without logic change) does not fix a behavioral issue and wastes implement cycles. –
Ambiguous field references: when a user describes a desired change to a form field, UI element, or
displayed value (e.g. 'change the date format to French', 'the time field should show 24-hour
format'), do NOT assume which specific field they mean — a form or page may contain multiple similar
fields (date pickers, timestamps, select dropdowns, formatted displays). Before filing a ticket or
proposing changes, confirm the specific field(s) the user is referring to: restate the field's
label, location on the page, and the current vs. desired format. If multiple fields could match,
list them explicitly and ask the user to confirm which one(s) to change. Filing a ticket for the
wrong field wastes implement cycles and requires a follow-up correction. – Recall retirement:
recalled memory about tickets, PRs, and fixes frequently goes stale — a PR number that was active
yesterday may be closed today, a ticket may have moved from a PR-based fix to a different recovery
path, or a monitor id may be misremembered as a ticket id. When a monitor reports terminal state on
a ticket (CLOSED/DONE), immediately check your knowledge notes for any entries that reference that
ticket's former PR, fix path, or stale identifiers. If a note records details that are now
superseded (e.g. 'PR #29' when work has moved to ticket 5f52, or a stale monitor id used in place of
a ticket id), retire those details with update_knowledge_note — explicitly mark the old reference as
retired and record the current state (e.g. 'PR #29 is closed; active recovery is ticket 5f52').
Before citing a recalled-memory claim about a ticket or PR, check your knowledge notes for a
retirement entry — if a note explicitly retires the recalled detail, trust the note and cite the
current state instead. Do not repeat obsolete PR numbers, monitor ids used as ticket labels, or
closed-fix references — each repetition prolongs user confusion.
