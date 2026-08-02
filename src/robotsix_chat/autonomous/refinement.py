"""Self-refinement for autonomous session presets.

After a run completes, an LLM step proposes an updated "lessons learned"
addendum that folds in the run's feedback.  The next run uses the refined
prompt (base prompt + accumulated accepted addenda).

Refinements are persisted to disk independently of session state so they
survive server restarts.  An operator can accept, reject, or reset
refinements via the autonomous definitions API.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from robotsix_chat.llm import LlmioChatAgent

logger = logging.getLogger(__name__)

# Maximum number of accepted refinement entries to retain per definition.
# When exceeded the oldest accepted entries are summarised into a single
# condensed entry to bound prompt growth.
_MAX_ACCEPTED_REFINEMENTS = 10

# Maximum character length of the accumulated refinement addendum before
# older lessons are summarised.
_MAX_ADDENDUM_CHARS = 2_000

# Default persist path for refinement state.
_DEFAULT_PERSIST_PATH = "/data/autonomous_refinements.json"


@dataclass
class RefinementEntry:
    """One refinement proposal for a session definition."""

    id: str
    """UUID for this refinement entry."""

    timestamp: float
    """Unix timestamp when the refinement was proposed."""

    base_prompt: str
    """The definition's base prompt at the time of refinement."""

    previous_addendum: str
    """The accumulated addendum BEFORE this refinement."""

    proposed_addendum: str
    """The LLM-proposed new addendum (replaces previous_addendum)."""

    feedback_summary: str
    """Summary of the run outcome that triggered this refinement."""

    session_id: str
    """The session whose completion triggered this refinement."""

    status: str = "pending"
    """``pending``, ``accepted``, or ``rejected``."""


@dataclass
class DefinitionRefinementState:
    """Per-definition refinement state persisted to disk."""

    definition_name: str
    base_prompt: str = ""
    accepted_addendum: str = ""
    entries: list[RefinementEntry] = field(default_factory=list)


class RefinementStore:
    """Persist and manage self-refinement state for autonomous presets."""

    def __init__(
        self,
        persist_path: str = _DEFAULT_PERSIST_PATH,
        *,
        agent_factory: Callable[[], LlmioChatAgent] | None = None,
    ) -> None:
        """Create a refinement store.

        *agent_factory* is optional — when absent, :meth:`propose_refinement`
        will skip the LLM step (used when the extra is not importable).
        """
        self._persist_path = Path(persist_path)
        self._agent_factory = agent_factory
        self._states: dict[str, DefinitionRefinementState] = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> dict[str, DefinitionRefinementState]:
        """Load refinement state from disk; return empty dict on failure."""
        try:
            if not self._persist_path.exists():
                return {}
            raw = json.loads(self._persist_path.read_text())
        except Exception:
            logger.exception(
                "Failed to load refinement state from %s",
                self._persist_path,
            )
            return {}
        states: dict[str, DefinitionRefinementState] = {}
        for name, entry in raw.items():
            try:
                entries = [
                    RefinementEntry(
                        id=e["id"],
                        timestamp=e["timestamp"],
                        base_prompt=e.get("base_prompt", ""),
                        previous_addendum=e.get("previous_addendum", ""),
                        proposed_addendum=e.get("proposed_addendum", ""),
                        feedback_summary=e.get("feedback_summary", ""),
                        session_id=e.get("session_id", ""),
                        status=e.get("status", "pending"),
                    )
                    for e in entry.get("entries", [])
                ]
                states[name] = DefinitionRefinementState(
                    definition_name=name,
                    base_prompt=entry.get("base_prompt", ""),
                    accepted_addendum=entry.get("accepted_addendum", ""),
                    entries=entries,
                )
            except Exception:
                logger.exception("Skipping unparsable refinement state for %s", name)
        logger.info(
            "Loaded refinement state for %d definitions from %s",
            len(states),
            self._persist_path,
        )
        return states

    def _save(self) -> None:
        """Persist refinement state to disk."""
        try:
            data = {}
            for name, state in self._states.items():
                data[name] = {
                    "definition_name": state.definition_name,
                    "base_prompt": state.base_prompt,
                    "accepted_addendum": state.accepted_addendum,
                    "entries": [
                        {
                            "id": e.id,
                            "timestamp": e.timestamp,
                            "base_prompt": e.base_prompt,
                            "previous_addendum": e.previous_addendum,
                            "proposed_addendum": e.proposed_addendum,
                            "feedback_summary": e.feedback_summary,
                            "session_id": e.session_id,
                            "status": e.status,
                        }
                        for e in state.entries
                    ],
                }
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._persist_path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.exception("Failed to persist refinement state")

    # -- state accessors ---------------------------------------------------

    def get_state(
        self, definition_name: str, base_prompt: str = ""
    ) -> DefinitionRefinementState:
        """Return (creating if needed) the refinement state for *definition_name*.

        When *base_prompt* is provided and differs from the stored base,
        the stored base is updated — this catches operator edits to the
        preset's prompt between restarts.
        """
        state = self._states.get(definition_name)
        if state is None:
            state = DefinitionRefinementState(definition_name=definition_name)
            self._states[definition_name] = state
        if base_prompt and state.base_prompt != base_prompt:
            # Operator edited the base prompt — reset accumulated addendum
            # since the old lessons may no longer apply.
            if state.accepted_addendum:
                logger.info(
                    "Base prompt changed for %r — resetting refinement addendum",
                    definition_name,
                )
                state.accepted_addendum = ""
            state.base_prompt = base_prompt
            self._save()
        return state

    def effective_prompt(self, definition_name: str, base_prompt: str) -> str:
        """Return the effective prompt: base + accepted addendum (if any)."""
        state = self.get_state(definition_name, base_prompt)
        if state.accepted_addendum:
            return f"{base_prompt}\n\n{state.accepted_addendum}"
        return base_prompt

    def get_entries(self, definition_name: str) -> list[RefinementEntry]:
        """Return all refinement entries for *definition_name*."""
        state = self._states.get(definition_name)
        if state is None:
            return []
        return list(state.entries)

    # -- refinement lifecycle ----------------------------------------------

    async def propose_refinement(
        self,
        definition_name: str,
        base_prompt: str,
        session_id: str,
        conversation_history: str,
        *,
        auto_accept: bool = False,
    ) -> RefinementEntry | None:
        """Run the LLM refinement step and record a pending/accepted entry.

        *conversation_history* is the full exchange from the completed run.
        When *auto_accept* is ``True`` (no operator approval required), the
        entry is immediately accepted.

        Returns the new :class:`RefinementEntry`, or ``None`` when the
        refinement step fails or the agent factory is unavailable.
        """
        if self._agent_factory is None:
            logger.warning(
                "Refinement skipped for %r — no agent factory available",
                definition_name,
            )
            return None

        state = self.get_state(definition_name, base_prompt)

        # Build the refinement LLM prompt.
        refinement_prompt = _build_refinement_prompt(
            base_prompt=base_prompt,
            current_addendum=state.accepted_addendum,
            conversation_history=conversation_history,
        )

        # Call the LLM.
        try:
            agent = self._agent_factory()
            reply_parts: list[str] = []
            async for token in agent.stream(
                refinement_prompt,
                history=None,
                session_id=session_id,
                client_id=None,
            ):
                reply_parts.append(token)
            proposed_addendum = "".join(reply_parts).strip()
        except Exception:
            logger.exception(
                "Refinement LLM call failed for definition %r", definition_name
            )
            return None

        if not proposed_addendum:
            logger.info(
                "Refinement LLM returned empty addendum for %r — skipping",
                definition_name,
            )
            return None

        # Build the entry.
        status = "accepted" if auto_accept else "pending"
        entry = RefinementEntry(
            id=uuid.uuid4().hex,
            timestamp=time.time(),
            base_prompt=base_prompt,
            previous_addendum=state.accepted_addendum,
            proposed_addendum=proposed_addendum,
            feedback_summary=_summarise_history(conversation_history),
            session_id=session_id,
            status=status,
        )
        state.entries.append(entry)

        if auto_accept:
            state.accepted_addendum = proposed_addendum
            self._compact(state)
            logger.info(
                "Auto-accepted refinement %s for %r (addendum %d → %d chars)",
                entry.id,
                definition_name,
                len(entry.previous_addendum),
                len(proposed_addendum),
            )
        else:
            logger.info(
                "Pending refinement %s for %r (awaiting operator approval)",
                entry.id,
                definition_name,
            )

        self._save()
        return entry

    def accept_refinement(self, definition_name: str, refinement_id: str) -> bool:
        """Accept a pending refinement by *refinement_id*.

        Returns ``True`` when the refinement was found and accepted.
        """
        state = self._states.get(definition_name)
        if state is None:
            return False
        for entry in state.entries:
            if entry.id == refinement_id and entry.status == "pending":
                entry.status = "accepted"
                state.accepted_addendum = entry.proposed_addendum
                self._compact(state)
                logger.info(
                    "Accepted refinement %s for %r",
                    refinement_id,
                    definition_name,
                )
                self._save()
                return True
        return False

    def reject_refinement(self, definition_name: str, refinement_id: str) -> bool:
        """Reject a pending refinement by *refinement_id*.

        Returns ``True`` when the refinement was found and rejected.
        """
        state = self._states.get(definition_name)
        if state is None:
            return False
        for entry in state.entries:
            if entry.id == refinement_id and entry.status == "pending":
                entry.status = "rejected"
                logger.info(
                    "Rejected refinement %s for %r",
                    refinement_id,
                    definition_name,
                )
                self._save()
                return True
        return False

    def reset_refinements(self, definition_name: str) -> bool:
        """Reset all refinements for *definition_name* — clears addendum.

        Returns ``True`` when state existed and was reset.
        """
        state = self._states.pop(definition_name, None)
        if state is None:
            return False
        logger.info("Reset all refinements for %r", definition_name)
        self._save()
        return True

    # -- internal ----------------------------------------------------------

    def _compact(self, state: DefinitionRefinementState) -> None:
        """Bound the number of accepted entries and addendum length.

        When the accepted count exceeds ``_MAX_ACCEPTED_REFINEMENTS``, the
        oldest entries are summarised into one condensed entry.  When the
        addendum exceeds ``_MAX_ADDENDUM_CHARS`` it is truncated with a
        note.
        """
        accepted = [e for e in state.entries if e.status == "accepted"]
        if len(accepted) > _MAX_ACCEPTED_REFINEMENTS:
            # Keep only the most recent entries; drop oldest.
            to_drop = accepted[: len(accepted) - _MAX_ACCEPTED_REFINEMENTS]
            for e in to_drop:
                state.entries.remove(e)
            logger.info(
                "Compacted %d oldest accepted refinements for %r",
                len(to_drop),
                state.definition_name,
            )

        if len(state.accepted_addendum) > _MAX_ADDENDUM_CHARS:
            state.accepted_addendum = (
                state.accepted_addendum[:_MAX_ADDENDUM_CHARS]
                + "\n\n[addendum truncated — older lessons summarised]"
            )
            logger.info(
                "Truncated addendum for %r (%d chars)",
                state.definition_name,
                len(state.accepted_addendum),
            )


# -- prompt builders -------------------------------------------------------


def _build_refinement_prompt(
    base_prompt: str,
    current_addendum: str,
    conversation_history: str,
) -> str:
    """Build the LLM prompt for the refinement step."""
    return f"""You are a prompt-refinement assistant for an autonomous AI agent.
Your job: analyse the completed run below and propose an updated
"lessons learned" addendum that will be appended to the agent's base
prompt for FUTURE runs.

## Rules

1. The BASE PROMPT is IMMUTABLE — never change, rephrase, or contradict
   it.  Your output is ONLY the addendum.
2. The addendum should capture actionable lessons: what went well, what
   failed, what the agent should do differently next time, what pitfalls
   to avoid.
3. Keep the addendum CONCISE and ACTIONABLE — bullet points preferred.
   Cap at ~500 words.
4. If the current addendum already covers a lesson, preserve the still-
   relevant parts and fold in the new lesson.
5. If the run was unremarkable (no clear lessons), return an empty
   response.
6. Output ONLY the addendum text — no preamble, no commentary, no
   markdown fences.

## Base prompt (immutable)
{base_prompt}

## Current addendum (may be empty)
{current_addendum or "(none)"}

## Completed run transcript
{conversation_history}

## Task
Produce the updated addendum text (or empty if no lessons learned):"""


def _summarise_history(conversation_history: str) -> str:
    """Produce a short summary of the run outcome from its transcript."""
    if not conversation_history:
        return "(empty run)"
    # Take the last 300 chars as a rough outcome summary.
    tail = conversation_history[-300:].strip()
    if len(conversation_history) > 300:
        tail = f"...\n{tail}"
    return tail
