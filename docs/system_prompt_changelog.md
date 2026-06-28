# System Prompt Changelog

Governed artifact: `Settings.agent_instruction` default literal in
`src/robotsix_chat/config.py`.  Version stamp: `SYSTEM_PROMPT_VERSION`
in the same module.

---

## Governance policy

Every change to `Settings.agent_instruction` (the pydantic field default
literal in `src/robotsix_chat/config.py`) **MUST**:

1. **Bump** `SYSTEM_PROMPT_VERSION` to the next integer.
2. **Add a new entry** at the top of this file (reverse-chronological,
   newest first) with the header `## v<N> — <YYYY-MM-DD> — <ticket-id>`.
3. **Record the SHA256** of the new `agent_instruction` default literal
   (computed as `hashlib.sha256(default.encode()).hexdigest()`) in the entry.
4. **Mirror** the updated default literal verbatim in the `agent.instruction`
   row of `docs/configuration.md`.

A CI test (`tests/config/test_system_prompt_governance.py`) enforces that
the latest entry's version matches `SYSTEM_PROMPT_VERSION` and its recorded
hash matches the live default — edits that skip this file **will fail CI**.

### Rollback procedure

Rollback is a **forward-moving new version** — never reuse a version
number.  To revert to a previous prompt:

1. Pick the target prior version's entry in this changelog.
2. Restore its prompt text via git, e.g.:
   `git revert <commit>` or `git show <commit>:src/robotsix_chat/config.py`
   (extract the `agent_instruction` block).
3. Bump `SYSTEM_PROMPT_VERSION` to the next number.
4. Add a new changelog entry `## v<N> — <YYYY-MM-DD> — <ticket-id>` with:
   - **Summary**: `rollback to v<K>`
   - **Rationale**: why the rollback is needed and which ticket authorises it.
   - **SHA256**: the hash of the restored literal (must match the prior
     version's recorded hash).
5. Mirror the restored literal in `docs/configuration.md`.

---

## v13 — 2026-06-28 — false_default_repo_claim

**Summary:** Remove the false universal claim that "new tickets default to
robotsix-mill regardless of source" — the board manager (via `consult_mill`)
may route tickets to `robotsix-mill` by default, but `create_board_ticket`
has no such default (the agent provides `repo_id` explicitly).  The
verification rule now attributes the default correctly to the board manager
rather than asserting it as a universal fact.

**Rationale:** The universal claim misleads the agent into thinking direct
`create_board_ticket` calls might silently land on the wrong board, inviting
unnecessary verification steps.  Fixing the wording eliminates this
confusion while keeping the verification instruction universal (both
`create_board_ticket` and `consult_mill` paths benefit from post-creation
verification).

**SHA256:** `ddc129c8c333f50cfc17064d815a471eeab7cf982da6206243d798dd3ad2c480`

## v12 — 2026-06-28 — board_rules_contradict_create_ticket

**Summary:** Resolve contradictory Board/mill rule for ticket creation.  The
old Rule 1 directed ALL write operations (including ticket creation) to
`consult_mill`, but Rule 4 told the agent to use `create_board_ticket`
(which calls the board reader endpoint directly).  Rule 1 is now scoped to
complex write operations (migrate, transition, triage) only; simple ticket
creation uses `create_board_ticket`.  The verification rule is also
generalised from "via consult_mill" to cover both paths.

**Rationale:** Two rules gave conflicting directives for the same action,
causing the model to guess which tool to use.  Aligning them eliminates
the contradiction.

**SHA256:** `81cc03108729b4e4fe46c2b191c5863a8b4ce018f5c99fda4d2927f4bd722a0c`

## v11 — 2026-06-27 — agent_guard_bypasses_governance

**Summary:** Fold the runtime `_AGENT_GUARD` hardening layer (previously
appended in `agent.py`) into the `agent_instruction` default literal so it
is governed by `SYSTEM_PROMPT_VERSION`, this changelog, SHA256 tracking, and
CI enforcement.

**Rationale:** The guard text — which tells the model it cannot run shell
commands, read/edit files, browse the web, or access the host — was injected
at runtime without any governance coverage.  Any edit to it would change what
the model receives with no version bump, no changelog entry, and no CI
failure.  Moving it into the versioned default closes this gap.

**SHA256:** `3aeecd5f472970cd59cd4b92a889c83e5c0608b89c99137c14f7c96fc45523c6`

## v10 — 2026-06-25 — 20260625T123055Z-system-prompt-contains-internal-python-i-d0a8

**Summary:** Replace calendar/task tools paragraph — remove three
developer-facing identifiers (``build_calendar_tools()``,
``CalendarSettings.enabled=False``, ``CALENDAR_BROKER_TOKEN``) that don't
belong in an LLM system prompt, replacing them with LLM-appropriate language.

**Rationale:** The prior text was copy-pasted from a development ticket
without adaptation for the LLM audience.  The new text describes the tools'
purpose and behaviour in terms the model can act on (availability,
disabling, and the instruction to briefly note unavailability rather than
proposing alternatives).

**SHA256:** `1d0ec5213cf5931aff7ec8e7abe4f46f8320ac4c13bbba1ef1aa96040d3f4c37`

## v9 — 2026-06-25 — 20260624T212653Z-chat-agent-hard-block-delegate-task-for-0619

**Summary:** Add enforcement note — ``delegate_task`` now refuses board/ticket
work and directs the agent to use ``consult_mill`` instead.

**Rationale:** Previously the prompt warned that delegate-task results are
never returned and a ticket filed that way may silently fail.  The
programmatic gate is now in place — ``delegate_task`` actively rejects
board/ticket requests — and the prompt reflects this enforcement so the agent
does not attempt the now-blocked path.  Ticket:
20260624T212653Z-chat-agent-hard-block-delegate-task-for-0619.

**SHA256:** `15cad9cc4a5854fa5c4682f0921cd534d21fb4542b89b722c28dfa44476257de`

## v8 — 2026-06-24 — 20260624T212711Z-chat-agent-stop-redundant-tool-loading-n-a0f3

**Summary:** Remove the misleading "Load tools once / run a capability check"
directive and replace it with an instruction that all tools are already
available and narration of loading/preparing/fetching tools is forbidden.

**Rationale:** The previous directive ("Load tools once at the start of a
session. Before branching into a complex workflow, run a single generic
capability check.") actively invited dead narration like "I'll load the
tools…" / "Let me load the task management tool first" — wasting ~150–200
output tokens per trace. Tools are assembled once at startup and never
reloaded per-turn; the prompt now reflects reality and explicitly forbids
the narration. Ticket: 20260624T212711Z-chat-agent-stop-redundant-tool-loading-n-a0f3.

**SHA256:** `8aaf695d4004ff37872c8f183954324aacc1f6bb6d6e8a3b91033f0463d81ef2`

## v7 — 2026-06-24 — 20260624T212702Z-chat-agent-dedup-ticket-filing-before-su-6ed5

**Summary:** Add dedup rule before ticket filing — the agent must check for
existing open tickets covering the same intent before creating a new one, and
act on `create_board_ticket`'s built-in duplicate warnings.

**Rationale:** Prevents duplicate ticket creation by enforcing a pre-flight
board read and requiring the agent to reuse existing tickets rather than filing
duplicates.

**SHA256:** `ae70fa569e48c2ef71d35a731a112ad0e4b490434ef2ddc8d0a19173e8a4099e`

## v6 — 2026-06-24 — 20260624T212708Z-chat-agents-enforce-the-three-sentences-236a

Tighten conciseness rule: name prohibited output shapes explicitly.

- Extended the three-sentence bullet to explicitly gate multi-row markdown
  tables, timeline/audit dumps, and recap lists behind an explicit user
  request.
- Added prohibition on repeating content already shown in the same
  conversation.

**SHA256:** `344e725c838591049557069cd1aa654422d886e13ece396b2016b9aeb4657dc7`

## v5 — 2026-06-24 — 20260623T210918Z-gate-sub-agent-status-output-behind-a-ma-e2f0

**Summary:** Added a bullet to the Board/mill rules instructing the foreground
agent to set ``verify_via_board=True`` when launching a check loop that monitors
mill/board/thread/ticket status, and to never assert board status without a
fresh ``consult_mill`` read.

**Rationale:** Prevents fabricated status reports from check-loop sub-agents
by enforcing a board-read gate.  The new bullet is defense-in-depth alongside
the programmatic gate in ``loops.py``.

**SHA256:** `2188c9422da9d5a9db8cf024095d8717b0e779391b25903c91482109ceff75ff`

---

## v4 — 2026-06-24 — 20260623T204239Z-robotsix-chat-give-the-assistant-a-writa-ff6c

**Summary:** Add knowledge-base instructions — the agent now has a local,
durable knowledge base (add/append/update/list/read_knowledge_note tools)
for operational notes and lessons; it must consult it at the start of every
session and write durable findings to it.

**SHA256:** `efd64ca3849a4f0872754fa86119a18511edc0c4a1816a94206e24dc618f1e8b`

## v3 — 2026-06-24 — 20260623T210933Z-tighten-sub-agent-prompt-efficiency-chec-5a52

Add sub-agent efficiency rules (cost-analysis proposals #9 and #10):

- #9: Check tool availability before describing a plan; if a required
  tool is missing, state it in one sentence and stop.  Answer in three
  sentences or fewer unless the user explicitly asks for elaboration.
- #10: Load tools once at the start of a session.  Before branching into
  a complex workflow, run a single generic capability check.  Do not
  re-load the same tool descriptions across turns.

**SHA256:** `323b644912809fda2d4ed9f80cf0e01d6742f6b6c05d5ff85d440e83e65aba52`

## v2 — 2026-06-24 — 20260624T020652Z-give-the-assistant-direct-1628

Add board-reader tool guidance:

- New rule: use `list_board_tickets` / `read_board_ticket` for reading
  board state (HTTP endpoint, same as user's UI); use `consult_mill` for
  writes.  Never fabricate ticket states.
- Verification: after creating a ticket via `consult_mill`, verify it
  landed on the right board with `list_board_tickets`.

**SHA256:** `33af94596b21c0f64908d3aa93eb2c8c8d1f491ed52dcab1c6287ff3c36128c5`

## v1 — 2026-06-23 — 20260623T204251Z-robotsix-chat-governance-for-assistant-s-45f3

**Summary:** Baseline — the current `agent_instruction` default literal as
established by ticket `20260623T203856Z-robotsix-chat-update-the-assistant-s-own-838a`
and recorded when this governance layer was introduced.

**Rationale:** Ticket …-838a appended board/mill operational guidance
(delegate-vs-inline, board-placement verification, draft→ready auto-pickup)
and calendar/task tool guidance to the pre-existing "You are a helpful
assistant." prefix.  This entry locks in that known-good state.

**Diff:** `git show 7b890de -- src/robotsix_chat/config.py` (the …-838a
merge commit), or `git log -p -- src/robotsix_chat/config.py` scoped to the
`agent_instruction` block.

**SHA256:** `09b73c46b24449484a5e2e9484137b85d73cfe210aa31eac05c81ca4f0698674`
