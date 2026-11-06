"""Resolved / orphan finding state for the feedback runner."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)


class FindingStateMixin:
    """Durable resolved/orphan finding records (knowledge-store backed).

    Mixed into :class:`~robotsix_chat.feedback.runner.FeedbackRunner`,
    which sets the instance attributes declared below.
    """

    _conversation_store: ConversationStore | None
    _knowledge_store: KnowledgeStore | None

    #: Knowledge-store topic under which findings for a deleted owner session
    #: are parked (so a later periodic review can still read them) instead of
    #: being routed to an orphan investigation subsession that would deliver
    #: its summary to a conversation that no longer exists.
    _ORPHAN_FINDINGS_TOPIC: str = "feedback-orphan-findings"

    #: Knowledge-store topic under which findings that were handed to an
    #: investigation subsession (or escalated to the operator) are recorded
    #: as resolved, keyed by finding title.  Recording the resolution durably
    #: means the same finding is not re-investigated on the next session-end
    #: run — the ticket path that used to make this durable is closed by the
    #: board admission policy, so routing must now record its own outcome.
    _RESOLVED_FINDINGS_TOPIC: str = "feedback-resolved-findings"

    #: Keep only the most recent N resolved findings in the shared note so it
    #: cannot grow without bound.
    _RESOLVED_FINDINGS_CAP: int = 200

    #: Keep only the most recent N orphan findings in the shared note so it
    #: cannot grow without bound.
    _ORPHAN_FINDINGS_CAP: int = 50

    def _finding_resolved(self, ticket: dict[str, Any]) -> bool:
        """Return ``True`` when *ticket* (by normalized title) is already resolved.

        Consults the :data:`_RESOLVED_FINDINGS_TOPIC` knowledge note, which
        records findings that were handed to an investigation subsession or
        escalated to the operator.  Best-effort: a missing store or any
        persistence error is logged and returns ``False`` so routing proceeds
        (the in-process dedup still guards the immediate re-run).
        """
        store = self._knowledge_store
        if store is None:
            return False
        norm_title = (ticket.get("title") or "").strip().lower()
        if not norm_title:
            return False
        try:
            existing = store.list(topic=self._RESOLVED_FINDINGS_TOPIC)
        except Exception:
            logger.exception(
                "Failed to read resolved-findings note — treating %r as unresolved",
                ticket.get("title"),
            )
            return False
        for entry in existing:
            try:
                payload = json.loads(entry.content)
            except json.JSONDecodeError, ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            for record in payload.get("resolved", []):
                if not isinstance(record, dict):
                    continue
                if (record.get("title") or "").strip().lower() == norm_title:
                    return True
        return False

    def _record_resolved_finding(
        self,
        ticket: dict[str, Any],
        *,
        session_id: str,
        trigger_type: str,
    ) -> None:
        """Durably record *ticket* as resolved (keyed on its title).

        Appended to the single :data:`_RESOLVED_FINDINGS_TOPIC` knowledge
        note, keeping only the most recent :data:`_RESOLVED_FINDINGS_CAP`
        entries.  Best-effort: a missing store or any persistence error is
        logged and swallowed — the in-process ``_last_filed_at`` dedup still
        guards the immediate re-run.
        """
        store = self._knowledge_store
        if store is None:
            logger.debug(
                "No knowledge store wired — resolved feedback finding %r not recorded",
                ticket["title"],
            )
            return
        record = {
            "title": ticket["title"],
            "body": ticket["description"],
            "session_id": session_id,
            "trigger": trigger_type,
            "target_repo": ticket.get("target_repo", ""),
        }
        norm_title = ticket["title"].strip().lower()
        try:
            existing = store.list(topic=self._RESOLVED_FINDINGS_TOPIC)
            resolved: list[dict[str, Any]] = []
            note_id: str | None = None
            if existing:
                note_id = existing[0].id
                try:
                    payload = json.loads(existing[0].content)
                except json.JSONDecodeError, ValueError:
                    payload = None
                if isinstance(payload, dict) and isinstance(
                    payload.get("resolved"), list
                ):
                    resolved = list(payload["resolved"])
            if not any(
                isinstance(r, dict)
                and (r.get("title") or "").strip().lower() == norm_title
                for r in resolved
            ):
                resolved.append(record)
            resolved = resolved[-self._RESOLVED_FINDINGS_CAP :]
            content = json.dumps({"resolved": resolved}, indent=2)
            if note_id is not None:
                store.update(note_id, content)
            else:
                store.add(self._RESOLVED_FINDINGS_TOPIC, content)
        except Exception:
            logger.exception(
                "Failed to record resolved feedback finding %r as a knowledge note",
                ticket["title"],
            )

    def _owner_session_deleted(self, session_id: str) -> bool:
        """Return ``True`` when *session_id* is absent from the conversation store.

        A finding whose owner session has been deleted (or evicted) has no
        parent conversation left to deliver an investigation to.  A
        closed-but-present session (``POST /sessions/{id}/close``) still
        exists in the store, so ``get_session`` returns it and this returns
        ``False`` — those findings are still routed.

        When no conversation store is wired we cannot tell, so we return
        ``False`` and let routing proceed unchanged.
        """
        store = self._conversation_store
        if store is None:
            return False
        try:
            return store.get_session(session_id) is None
        except Exception:
            logger.exception(
                "Failed to check owner session %s existence — proceeding to route",
                session_id,
            )
            return False

    def _record_orphan_finding(
        self,
        ticket: dict[str, Any],
        *,
        session_id: str,
        trigger_type: str,
    ) -> None:
        """Park a deleted-owner finding as a single capped knowledge note.

        The findings are stored under the :data:`_ORPHAN_FINDINGS_TOPIC`
        topic as a JSON ``{"findings": [...]}`` payload, keeping only the
        most recent :data:`_ORPHAN_FINDINGS_CAP` entries so the note cannot
        grow without bound.  Best-effort: a missing store or any persistence
        error is logged and swallowed.
        """
        store = self._knowledge_store
        if store is None:
            logger.debug(
                "No knowledge store wired — orphan feedback finding %r not parked",
                ticket["title"],
            )
            return
        finding = {
            "title": ticket["title"],
            "body": ticket["description"],
            "session_id": session_id,
            "trigger": trigger_type,
            "target_repo": ticket.get("target_repo", ""),
        }
        try:
            existing = store.list(topic=self._ORPHAN_FINDINGS_TOPIC)
            findings: list[dict[str, Any]] = []
            note_id: str | None = None
            if existing:
                note_id = existing[0].id
                try:
                    payload = json.loads(existing[0].content)
                except json.JSONDecodeError, ValueError:
                    payload = None
                if isinstance(payload, dict) and isinstance(
                    payload.get("findings"), list
                ):
                    findings = list(payload["findings"])
            findings.append(finding)
            findings = findings[-self._ORPHAN_FINDINGS_CAP :]
            content = json.dumps({"findings": findings}, indent=2)
            if note_id is not None:
                store.update(note_id, content)
            else:
                store.add(self._ORPHAN_FINDINGS_TOPIC, content)
        except Exception:
            logger.exception(
                "Failed to park orphan feedback finding %r as a knowledge note",
                ticket["title"],
            )
