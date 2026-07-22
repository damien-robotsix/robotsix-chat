# System Prompt Changelog

Governed artifact: `Settings.agent_instruction` default literal in
`src/robotsix_chat/config/settings.py`. Version stamp: `SYSTEM_PROMPT_VERSION` in the same module.

______________________________________________________________________

## v46 — 2026-07-22 — add-guidance-to-system-prompt-for-handli-8e03

**Summary:** Add a "Repo creation bootstrap" paragraph to the Autonomy section. When creating a
new repository or working with a freshly created empty repo, tool-chains that require an existing
commit or branch to push to (e.g. push_direct_repo_branch, open_direct_repo_pr) will deadlock if
the repo has no commits. The assistant must proactively seed an initial commit during repo creation
(a README.md, .gitignore, or minimal template file) so that subsequent tool-chains have a branch
and commit to target.

**Rationale:** The assistant encountered a structural deadlock where a tool needed to push the first
commit to a newly created empty repo but had no tool that could create an initial commit (only push
to an existing branch). The assistant responded by repeatedly asking the user to manually initialize
the repo rather than proposing a tool-based workaround. The new rule enforces that repo creation
must always include seeding an initial commit to prevent this class of bootstrap deadlock.

**SHA256:** `493610cf12e8deed31276c50ead948fdebb7893142ddbbabeb274107b04b3871`

## v46 — 2026-07-22 — add-cross-session-persistent-knowledge-r-b5bb

**Summary:** Add `search_knowledge_notes` to the knowledge-base tool list in the system prompt. The
knowledge store now exposes a search tool that finds notes by querying their topic and content
(case-insensitive substring match), ranked by relevance. This lets the agent retrieve prior
diagnostic notes, deployment statuses, and other key facts without needing to recall exact note ids.

**Rationale:** The assistant wasted time re-discovering that empty-diff bug fixes were already
merged because it could not reliably retrieve prior diagnostic notes — note ids were truncated or
missing from its context. The new search capability eliminates the fragile-id-recall dependency.

**SHA256:** `b0e205017f02e8e2a90707f2b6fbaf51f356e5ab7362124803eb79602ba13050`

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

______________________________________________________________________

## v40 — 2026-07-21 — incorporate-user-statements-as-ground-truth-86d1 / avoid-filing-tickets-for-issues-that-do-6fe3

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

## v39 — 2026-07-20 — add-deploy-server-restart-capability-for-144c

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
2. **Add a new entry** at the top of this file (reverse-chronological, newest first) with the header
   `## v<N> — <YYYY-MM-DD> — <ticket-id>`.
3. **Record the SHA256** of the new `agent_instruction` default literal (computed as
   `hashlib.sha256(default.encode()).hexdigest()`) in the entry.
4. The `agent.instruction` row of `docs/configuration.md` uses the placeholder `(long default)` in
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
2. Restore its prompt text via git, e.g.: `git revert <commit>` or
   `git show <commit>:src/robotsix_chat/config/settings.py` (extract the `agent_instruction` block).
3. Bump `SYSTEM_PROMPT_VERSION` to the next number.
4. Add a new changelog entry `## v<N> — <YYYY-MM-DD> — <ticket-id>` with:
   - **Summary**: `rollback to v<K>`
   - **Rationale**: why the rollback is needed and which ticket authorises it.
   - **SHA256**: the hash of the restored literal (must match the prior version's recorded hash).
5. The `agent.instruction` row of `docs/configuration.md` uses the placeholder `(long default)` — no
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
