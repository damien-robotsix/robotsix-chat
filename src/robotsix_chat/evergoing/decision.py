"""Subject-aware trim decision for the evergoing session.

Given the current post-trim transcript, ask a cheap summary-tier agent
whether the conversation's subject has clearly moved on and, if so, how
many *finished* leading turns can be safely dropped.  Conservative by
design: when the subject is unchanged or the model is unsure, nothing is
trimmed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from robotsix_chat.chat.server.routes.chat import ChatAgent

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrimDecision:
    """Outcome of a subject-aware trim decision.

    Attributes:
        subject_changed: ``True`` when the model judged the earlier leading
            turns to belong to a clearly finished, different subject.
        drop_leading: How many leading (oldest) *visible* turns may be
            dropped.  Always ``0`` when ``subject_changed`` is ``False``.
        reason: Short human-readable rationale for the audit log.

    """

    subject_changed: bool
    drop_leading: int
    reason: str


_DECISION_PROMPT = (
    "You maintain a single never-ending chat session by trimming away only "
    "turns that belong to an EARLIER, clearly FINISHED, DIFFERENT subject "
    "from what the conversation is about now.\n\n"
    "Rules — be conservative:\n"
    "- If the current subject is unchanged, or you are unsure, drop 0.\n"
    "- Only drop leading turns that plainly belong to a completed topic "
    "distinct from the most recent turns.\n"
    "- Never drop the most recent turns (work may be in flight).\n\n"
    "The transcript below has {n} turns, numbered 1 (oldest) to {n} "
    "(newest). You may drop at most {max_drop} leading turns.\n"
    "Reply with EXACTLY these two lines and nothing else:\n"
    "SUBJECT_CHANGED: yes|no\n"
    "DROP_LEADING: <integer between 0 and {max_drop}>\n\n"
    "Transcript:\n"
)

_SUBJECT_RE = re.compile(r"SUBJECT_CHANGED:\s*(yes|no)", re.IGNORECASE)
_DROP_RE = re.compile(r"DROP_LEADING:\s*(-?\d+)", re.IGNORECASE)


def build_trim_prompt(transcript: str, *, visible_count: int, max_drop: int) -> str:
    """Return the decision prompt for *transcript*.

    *visible_count* is the number of post-trim turns shown; *max_drop* is
    the largest number of leading turns that may be dropped while still
    keeping the required recent tail.
    """
    header = _DECISION_PROMPT.format(n=visible_count, max_drop=max_drop)
    return f"{header}{transcript}"


def parse_trim_decision(text: str, *, max_drop: int) -> TrimDecision:
    """Parse the model reply into a conservative :class:`TrimDecision`.

    Missing/garbled fields default to *no change* (``subject_changed=False``,
    ``drop_leading=0``).  ``drop_leading`` is clamped to ``[0, max_drop]`` and
    forced to ``0`` whenever the subject did not change.
    """
    subject_match = _SUBJECT_RE.search(text)
    subject_changed = (
        subject_match is not None and subject_match.group(1).lower() == "yes"
    )

    drop = 0
    drop_match = _DROP_RE.search(text)
    if drop_match is not None:
        try:
            drop = int(drop_match.group(1))
        except ValueError:
            drop = 0

    if not subject_changed:
        drop = 0
    drop = max(0, min(drop, max_drop))

    if drop == 0:
        reason = "subject unchanged" if not subject_changed else "no droppable turns"
    else:
        reason = f"subject changed; dropping {drop} finished leading turn(s)"
    return TrimDecision(
        subject_changed=subject_changed, drop_leading=drop, reason=reason
    )


async def decide_trim(
    agent: ChatAgent,
    transcript: str,
    *,
    visible_count: int,
    max_drop: int,
) -> TrimDecision:
    """Ask *agent* whether the subject changed and how many turns to drop.

    Streams the cheap summary-tier agent and parses its reply.  Any failure
    is caught and mapped to the conservative *no change* decision so the
    trim pass never raises into the scheduler.
    """
    prompt = build_trim_prompt(
        transcript, visible_count=visible_count, max_drop=max_drop
    )
    reply_parts: list[str] = []
    try:
        async for token in agent.stream(
            prompt,
            history=None,
            session_id=None,
            client_id=None,
            trace_name="evergoing-trim-decision",
        ):
            reply_parts.append(token)
    except Exception:
        logger.exception("Evergoing trim decision failed — keeping all turns")
        return TrimDecision(
            subject_changed=False, drop_leading=0, reason="decision failed"
        )

    return parse_trim_decision("".join(reply_parts), max_drop=max_drop)
