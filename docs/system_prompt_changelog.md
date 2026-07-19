# System Prompt Changelog

Governed artifact: `Settings.agent_instruction` default literal in
`src/robotsix_chat/config/settings.py`. Version stamp: `SYSTEM_PROMPT_VERSION` in the same module.

______________________________________________________________________

## v29 — 2026-07-19 — prevent-creation-of-duplicate-monitors-f-8af3

**Summary:** Extend `dedup_key` deduplication from `user_chat`-only to all subsession kinds. The old
guidance only mentioned `user_chat` for global error dedup; the new text covers periodic ticket
monitors too — set `dedup_key` to the ticket id when spawning a monitor (e.g. `'5f1c'`). The Monitor
lifecycle step also now specifies `dedup_key` usage. The dedup guard in `spawn_subsession` no longer
filters by `SubsessionKind.USER_CHAT`, so any subsession with an active dedup_key returns the
existing id instead of spawning a duplicate.

**Rationale:** Two periodic monitors were spawned for the same ticket, causing double reports and
manual cleanup. Extending the dedup guard to all kinds prevents duplicate periodic ticket monitors
when an agent re-files the same ticket, reducing noise and cognitive load.

**SHA256:** `95aaf72c2f3712613268708002fb7184570d1dde1b85a08cf953112cedfe3be0`

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

## v28 — 2026-07-19 — cross-reference-historical-claims-with-live-state-11ec

**Summary:** Add a "Verification" section to the default `agent_instruction`. When reporting the
state of an external system (repository contents, deployment status, ticket resolution), the agent
must verify through available tools rather than relying on memory alone. When the user directly
challenges a claim with contradictory observable evidence, re-verify against the live system
immediately rather than doubling down on a memory-based assertion. Prefer timestamped evidence
(commit SHA, deployment timestamp, tool call result) over recollection.

**Rationale:** Memory-based claims that contradict user-observable reality (empty repo, stale
container) damage trust and require additional verification steps. The agent must treat live system
state as the source of truth and distrust memory when it conflicts with live observation.

**SHA256:** `127e75f254fd71639a11b0958679da3f8c6b8ce1458443fb6717c5dcd902ea`

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
