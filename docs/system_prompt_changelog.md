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
