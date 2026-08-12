"""Parent delivery — routes subsession summaries/results to their parent.

Replaces the old ``ConversationDeliveryChannel``.  Two destinations:

* **Parent is the main chat session** (``parent_id is None``): the main
  agent runs a real turn reacting to the outcome (see
  :meth:`ParentDelivery._react_in_main_chat`) — under the owner's
  :class:`~robotsix_chat.chat.server.routes.RunSerializer` lock so it never
  interleaves with a ``/chat`` run's read-history/record window. The reply
  is recorded to history and, when an event sink is wired, pushed live to a
  connected browser as an ``agent_message`` frame (there is no open
  ``/chat`` request to carry it). Until :meth:`ParentDelivery.set_agent` has
  been called, or if the reaction turn itself fails, this degrades to the
  old passive record (the outcome as a synthetic turn) so it is never lost.

* **Parent is another subsession**: the summary is enqueued into the
  parent's inbox (role ``"parent"``) and shows up at its next turn
  boundary.  When the parent is no longer active, delivery degrades to
  the main-chat path so the outcome is never lost.

Reaction turns are fire-and-forget: the caller (a subsession worker or
HTTP endpoint) schedules the reaction in a background task and returns
immediately — the worker never blocks on a potentially slow LLM call.
The per-owner :class:`RunSerializer` lock still serialises reactions
with user-message turns so they never overlap.

Subsession agents carry the full tool suite themselves, so a *nested*
parent's summary is still delivered as data, not by re-running a second
agent — only the main-chat-parent case gets a live reaction turn, since
that is the one a human is actually watching.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING

from robotsix_chat.autonomous.models import AutonomousState
from robotsix_chat.chat.events import agent_message_frame

from .models import SubsessionInfo, SubsessionKind

if TYPE_CHECKING:
    from robotsix_chat.autonomous.runner import AutonomousRunner
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventSink
    from robotsix_chat.chat.server.routes import ChatAgent, RunSerializer

    from .registry import SubsessionRegistry

logger = logging.getLogger(__name__)

_REACT_PROMPT_TEMPLATE = (
    "CONSOLIDATION RULE (apply FIRST — this overrides all other instructions "
    "below): Scan the conversation above for other subsession outcomes you "
    "have reported.  If ANY exist, you MUST synthesize ALL of them — this "
    "new one PLUS the earlier ones — into ONE cohesive narrative paragraph. "
    "Group by theme (what was checked, what changed, what is recommended), "
    "NOT by subsession id.  Omit trivial NO_CHANGE runs entirely — mention "
    "only outcomes with real progress, blockers, or decisions for the user. "
    "If every outcome is no-change, reply with a single sentence like "
    "'All monitors report no change — nothing requires attention.'  "
    "NEVER reply to each outcome individually when you have already "
    "addressed prior outcomes — one paragraph covers everything.\n\n"
    "[System notice] Subsession {sub_id} ({kind}) '{title}' {reason} while "
    "you were not actively conversing with the user. Outcome:\n\n{outcome}\n\n"
    "FORMAT PROHIBITION (hard rule — violation will confuse the user): "
    "NEVER output any of these patterns: 'kind=', 'status=', '[N] kind=', "
    "'sub_id', 'Subsession summaries:', raw bullet enumerations like "
    "'[1] kind=periodic status=closed', "
    "block IDs (hex strings like 'a3f2', 'block a3f2'), state machine "
    "transitions, spawn counters, internal timeout values, stack traces, "
    "raw API response fragments, "
    "or any line that looks like a dump of internal subsession metadata.  "
    "These read as debug output — users want to know what happened and what "
    "to do, not how the system works internally.\n\n"
    "TRACKING STATUS (apply after consolidation): For every monitor you "
    "mention, make clear WHY it stopped and whether the user's tracking "
    "goal was met.  Use these categories:\n"
    "  FULFILLED — the monitored ticket reached a terminal state (closed, "
    "resolved, merged).  The user's tracking request was SATISFIED.  "
    "Phrase as 'Tracking complete for ticket X — it was resolved/closed.'\n"
    "  INTERRUPTED — the monitor auto-stopped, hit a run limit, or failed "
    "before the ticket finished.  Tracking was CUT SHORT.  Phrase as "
    "'Monitor for ticket X auto-stopped — the ticket may still need "
    "attention.'\n"
    "  ACTIVE — the monitor is paused/sleeping but still alive.  Tracking "
    "is ONGOING and resumes when the ticket changes or when the user sends "
    "it a message.  Phrase as 'Monitor for ticket X is paused — it will "
    "resume when the ticket updates or on a new message.'\n"
    "Always end with a NEXT STEP for the user: either 'No action needed — "
    "all tracking goals were met' or a concrete suggestion like 'You may "
    "want to check ticket X manually since its monitor stopped.'\n\n"
    "If there are NO earlier subsession outcomes to consolidate (this is the "
    "only one): If the outcome reports no change (the subsession auto-stopped, "
    "auto-paused, or reported NO_CHANGE / nothing changed), reply with a "
    "single brief acknowledgment that reflects the TERMINAL state and WHY: "
    "for auto-stopped (terminal, tracking interrupted) use phrasing like "
    "'No change — monitor auto-stopped. The ticket may still need attention.'; "
    "for auto-paused (reversible, tracking active) "
    "use 'No change — monitor paused (will resume when the ticket updates "
    "or on a new message).'; "
    "for ticket_terminal (tracking fulfilled) use "
    "'Tracking complete — the monitored ticket was resolved/closed.'  "
    "Never conflate a terminal ticket (fulfilled tracking) with an "
    "auto-stopped monitor (interrupted tracking) — the user's tracking "
    "request WAS satisfied when the ticket reached its end state.  "
    "Never "
    "conflate paused with closed — a closed/stopped monitor will NOT "
    "reappear on its own, while a paused monitor can be woken.  "
    "Do NOT re-list the ticket "
    "ID, current state, timestamp, or expected next path — the user already "
    "knows the monitored state and restating it adds noise.  If there is "
    "something new or actionable, jump "
    "straight to the delta: what changed, what the user should know, or what "
    "to do next. Never start with 'Acknowledged' or echo the subsession's "
    "full summary. This is a real turn: your reply will be shown to the user.\n\n"
    "FILTERING RULE: The outcome text above may contain internal technical "
    "details that the subsession agent included in its summary — block IDs, "
    "event numbers, state machine transitions, spawn counters, internal "
    "timeout values, stack traces, raw API response fragments, or debug "
    "output.  STRIP ALL of these before presenting to the user.  Your job "
    "is to extract the MEANING — what decision was reached, what action is "
    "needed — and present ONLY that.  If the outcome says 'event 35 triggered "
    "stall guard escalation after spawn counter reset at block a3f2', the "
    "user-facing version is 'a monitor auto-stopped because it detected a "
    "stall.'  When the reason is 'ticket_terminal' or 'completed', the "
    "tracking goal was FULFILLED — the ticket reached its natural end state "
    "and the user's request was satisfied.  Never pass raw internal "
    "identifiers through to the user."
)

# Template used when the main chat session has an active autonomous plan
# (proposal awaiting approval or execution in progress).  The agent must
# acknowledge the subsession outcome WITHOUT requesting re-approval or
# abandoning the plan.
_REACT_PROMPT_ACTIVE_PLAN_TEMPLATE = (
    "[System notice] Subsession {sub_id} ({kind}) '{title}' {reason} while "
    "you were {autonomous_state_phrase}.\n\n"
    "Your current plan:\n{plan_text}\n\n"
    "Outcome:\n{outcome}\n\n"
    "You are {autonomous_state_phrase}.  Briefly acknowledge this "
    "notification — incorporate any relevant information from it into your "
    "work — but DO NOT re-request approval, restart planning, or abandon "
    "your current plan.  If the outcome is not relevant to your current "
    "task, acknowledge it in one sentence and move on.  This is a note, not "
    "a blocker: stay on your plan and continue from where you left off.\n\n"
    "FORMAT PROHIBITION: NEVER output patterns like 'kind=', 'status=', "
    "'[N] kind=', 'Subsession summaries:', raw bullet enumerations of "
    "subsession metadata, block IDs (hex strings like 'a3f2'), state "
    "machine transitions, spawn counters, internal timeout values, stack "
    "traces, or raw API fragments.  "
    "These read as debug output — strip all internal detail before "
    "presenting to the user.\n\n"
    "CONSOLIDATION: Scan the conversation above for other subsession outcomes "
    "you have acknowledged.  If ANY exist, you MUST synthesize ALL of them — "
    "this new one PLUS the earlier ones — into ONE brief sentence.  Never "
    "output a raw bullet list or enumeration of bracketed id/kind/status "
    "metadata lines.\n\n"
    "FILTERING RULE: Strip internal technical details (block IDs, event "
    "numbers, state machine transitions, spawn counters, raw API fragments) "
    "from the outcome before incorporating it.  Extract only the MEANING — "
    "what decision was reached, what action is needed.  When the reason is "
    "'ticket_terminal' or 'completed', the tracking goal was FULFILLED "
    "(the ticket reached its end state) — do not report this as if tracking "
    "was interrupted.\n\n"
    "ANTI-RE-EMISSION RULE: If the subsession outcome (table, rollup, "
    "enumeration) was already presented to the user earlier in this "
    "conversation, do not re-emit the full payload verbatim. Reply with "
    "only the delta (what changed since the last presentation) or a "
    "one-sentence synthesis — never re-list the full table, ticket IDs, "
    "or enumeration that the user has already seen."
)

# Template used when multiple subsession outcomes are batched into a single
# reaction turn.  The outcomes are pre-formatted by _react_batched() into
# {outcomes_list} and the count is passed as {count}.
_BATCH_REACT_PROMPT_TEMPLATE = (
    "[System notice] {count} subsession outcomes occurred while you were "
    "not actively conversing with the user:\n\n"
    "{outcomes_list}\n"
    "CONSOLIDATION RULE (MANDATORY — apply before anything else): "
    "You MUST synthesize ALL {count} outcomes above into exactly ONE "
    "cohesive narrative paragraph.  Group by THEME (what was checked, "
    "what changed, what is recommended), NOT by subsession id or "
    "individual outcome.  Omit trivial NO_CHANGE runs entirely — mention "
    "only outcomes with real progress, blockers, or decisions that the "
    "user should know about.  If every outcome is no-change, reply with "
    "a single sentence like 'All monitors report no change — nothing "
    "requires attention.'  NEVER reply to each outcome individually — "
    "one paragraph, one reply.\n\n"
    "FORMAT PROHIBITION (hard rule — violation will confuse the user): "
    "NEVER output any of these patterns: 'kind=', 'status=', '[N] kind=', "
    "'sub_id', 'Subsession summaries:', raw bullet enumerations like "
    "'[1] kind=periodic status=closed', "
    "block IDs (hex strings like 'a3f2', 'block a3f2'), state machine "
    "transitions, spawn counters, internal timeout values, stack traces, "
    "raw API response fragments, "
    "or any line that looks like a dump of internal subsession metadata.  "
    "These read as debug output — users want to know what happened and what "
    "to do, not how the system works internally.\n\n"
    "TRACKING STATUS (apply after consolidation): For every monitor you "
    "mention, make clear WHY it stopped and whether the user's tracking "
    "goal was met.  Use these categories:\n"
    "  FULFILLED — the monitored ticket reached a terminal state (closed, "
    "resolved, merged).  The user's tracking request was SATISFIED.  "
    "Phrase as 'Tracking complete for ticket X — it was resolved/closed.'\n"
    "  INTERRUPTED — the monitor auto-stopped, hit a run limit, or failed "
    "before the ticket finished.  Tracking was CUT SHORT.  Phrase as "
    "'Monitor for ticket X auto-stopped — the ticket may still need "
    "attention.'\n"
    "  ACTIVE — the monitor is paused/sleeping but still alive.  Tracking "
    "is ONGOING and resumes when the ticket changes or when the user sends "
    "it a message.  Phrase as 'Monitor for ticket X is paused — it will "
    "resume when the ticket updates or on a new message.'\n"
    "Always end with a NEXT STEP for the user: either 'No action needed — "
    "all tracking goals were met' or a concrete suggestion like 'You may "
    "want to check ticket X manually since its monitor stopped.'\n\n"
    "FILTERING RULE: The outcome text above may contain internal technical "
    "details that the subsession agent included in its summary — block IDs, "
    "event numbers, state machine transitions, spawn counters, internal "
    "timeout values, stack traces, raw API response fragments, or debug "
    "output.  STRIP ALL of these before presenting to the user.  Your job "
    "is to extract the MEANING — what decision was reached, what action is "
    "needed — and present ONLY that.  Never pass raw internal "
    "identifiers through to the user."
)

# Mapping from internal reason codes to human-readable phrases used in the
# reaction prompt and fallback notification messages.
_REASON_PHRASES: dict[str, str] = {
    "completed": "completed",
    "max_runs": "reached its run limit",
    "human_approval_timeout": "timed out waiting for operator approval",
    "paused": "auto-paused after consecutive no-change runs",
    "no_change_auto_stop": "auto-stopped after consecutive no-change runs",
    "failed": "failed with an error",
    "stale_worker": "was terminated (stale worker)",
    "ticket_terminal": "completed — monitored ticket reached a terminal state",
    "repeated_blocked": "auto-stopped — ticket repeatedly blocked",
    "mill_unreachable": "failed — mill API unreachable",
    "ticket_unreachable": "failed — ticket API unreachable",
    "missing_tool": "closed — required tool unavailable",
    "pre_authorized_approval": "auto-escalated (pre-authorized ticket)",
}

# Hard cap on how many consecutive reaction turns (triggered by subsession
# closures during prior reactions) can nest for the same session.  Once the
# depth reaches this limit, further closures degrade to passive records so
# a broken tool loop can't chain-react unboundedly.
_MAX_REACTION_DEPTH = 3

# Seconds to wait for additional subsession outcomes to arrive before
# delivering a batch.  When multiple outcomes close within this window
# they are delivered in a single agent turn with a consolidation
# instruction, instead of producing N separate reaction turns that each
# emit a near-identical "no change" reply.
_BATCH_WINDOW_SECONDS = 0.5


# -- patterns for sanitizing reaction replies --------------------------------

# Lines that are entirely or predominantly internal subsession metadata.
# Matches e.g. "0 kind=periodic status=closed", "[1] kind=periodic status=closed",
# "No ticket_id in checkpoint".
_RAW_METADATA_LINE_RE = re.compile(
    r"^\s*"
    r"(\[\d+\]\s*)?"  # optional [N] prefix
    r"\d*\s*"  # optional bare digit prefix (0, 1, ...)
    r"("
    r"kind=\w+"  # kind=periodic
    r"|status=\w+"  # status=closed
    r"|No ticket_id"  # "No ticket_id in checkpoint"
    r"|sub_id\s"  # sub_id followed by identifier
    r"|Subsession summaries:"  # feedback-system metadata header
    r")",
    re.IGNORECASE,
)

# Parenthetical inline metadata: "(kind=periodic status=closed)"
_INLINE_PAREN_METADATA_RE = re.compile(
    r"\(\s*"
    r"(\d+\s*)?"  # optional numeric prefix
    r"((kind|status)=\w+\s*)+"  # one or more kind=/status= pairs
    r"\)",
    re.IGNORECASE,
)

# Bare inline "kind=X status=Y" clusters (no surrounding parens).
_INLINE_KIND_STATUS_RE = re.compile(
    r"\b(kind|status)=\w+(\s+(kind|status)=\w+)*\s*",
    re.IGNORECASE,
)

# "[N]" bracket prefix patterns used as internal IDs.
_INLINE_BRACKET_PREFIX_RE = re.compile(r"\[\d+\]\s*")

# Multiple spaces → single space.
_MULTI_SPACE_RE = re.compile(r"\s{2,}")

# Comma cleanup: ", ," → ",", trailing/leading commas.
_COMMA_CLEANUP_RE = re.compile(r",\s*,+")


def _sanitize_reaction_reply(reply: str) -> str:
    """Strip internal subsession metadata that the agent may have leaked.

    The reaction prompt already instructs the agent not to output patterns
    like ``kind=``, ``status=``, ``[N] kind=``, or raw system notices
    (e.g. ``No ticket_id in checkpoint``).  When the LLM ignores those
    instructions this function serves as a programmatic safety net so the
    user never sees raw internal identifiers.

    Returns a generic acknowledgment when the entire reply collapses to
    empty after sanitization.
    """
    if not reply.strip():
        return reply

    lines = reply.splitlines()
    cleaned: list[str] = []
    for line in lines:
        stripped = line.strip()
        # Drop lines that are purely internal metadata noise.
        if _RAW_METADATA_LINE_RE.match(stripped):
            continue
        # Strip inline metadata patterns from remaining lines.
        cleaned.append(_strip_inline_metadata(line))

    result = "\n".join(cleaned).strip()
    # Remove blank lines that may result from stripping entire lines.
    result = re.sub(r"\n{3,}", "\n\n", result)
    if not result:
        result = "I received a status update, but there is nothing new to report."
    return result


def _strip_inline_metadata(line: str) -> str:
    """Remove inline ``kind=`` / ``status=`` / ``[N]`` patterns from *line*."""
    # Parenthetical metadata: "(0 kind=periodic status=closed)" → ""
    line = _INLINE_PAREN_METADATA_RE.sub("", line)
    # Bare "kind=X status=Y" clusters.
    line = _INLINE_KIND_STATUS_RE.sub("", line)
    # "[0]" / "[1]" prefix patterns.
    line = _INLINE_BRACKET_PREFIX_RE.sub("", line)
    # Clean up artifacts like "( )", "()" before collapsing spaces.
    line = line.replace("( )", "").replace("()", "")
    # Clean up ", ," → "," and leading/trailing commas.
    line = _COMMA_CLEANUP_RE.sub(",", line).strip(", ")
    # Collapse multiple spaces.
    line = _MULTI_SPACE_RE.sub(" ", line)
    return line.strip()


def _extract_ticket_id(info: SubsessionInfo) -> str | None:
    """Return ``ticket_id`` from *info*'s checkpoint, or ``None``."""
    cp = info.checkpoint
    if cp is None:
        return None
    ticket_id = cp.get("ticket_id")
    return ticket_id if isinstance(ticket_id, str) else None


def _extract_last_known_state(info: SubsessionInfo) -> str | None:
    """Return ``last_known_state`` from *info*'s checkpoint, or ``None``."""
    cp = info.checkpoint
    if cp is None:
        return None
    state = cp.get("last_known_state")
    return state if isinstance(state, str) else None


def _format_user_chat_outcome(summary: str, transcript: Sequence[object]) -> str:
    """Build a detailed outcome for a user_chat subsession.

    Includes both the agent-written *summary* and the full conversation
    transcript so the parent can act on operator decisions even when the
    summary is terse (e.g. ``"Decisions recorded"``).
    """
    if not transcript:
        return summary
    lines = [summary, "", "Conversation transcript:"]
    for entry in transcript:
        # TranscriptEntry has .role (str) and .text (str).
        role: str = getattr(entry, "role", "unknown")
        text: str = getattr(entry, "text", "")
        lines.append(f"[{role}] {text}")
    return "\n".join(lines)


class ParentDelivery:
    """Deliver subsession outcomes to the conversation that spawned them."""

    def __init__(
        self,
        *,
        conversation_store: ConversationStore,
        registry: SubsessionRegistry,
        run_serializer: RunSerializer,
        event_sink: EventSink | None = None,
        autonomous_runner: AutonomousRunner | None = None,
        batch_window_seconds: float = _BATCH_WINDOW_SECONDS,
    ) -> None:
        """Wire the store, registry, per-owner run serializer, and event sink.

        *event_sink*, when given, receives an ``agent_message`` frame each
        time a main-chat-parent reaction turn (see :meth:`_react_in_main_chat`)
        produces a reply, so a connected browser can show it live instead of
        only picking it up on the next ``GET /history``.

        *autonomous_runner*, when given, is consulted before each reaction
        turn: if the main session has an active autonomous plan (proposal
        or executing state), the reaction prompt reminds the agent to
        acknowledge the outcome as a note and stay on its plan, preventing
        the agent from dropping its work and re-requesting approval.

        *batch_window_seconds* controls how long to wait for additional
        subsession outcomes before delivering a batch.  Set to 0 to
        disable batching (every outcome fires its own reaction turn
        immediately, preserving the pre-batching behavior).
        """
        self._store = conversation_store
        self._registry = registry
        self._run_serializer = run_serializer
        self._event_sink = event_sink
        self._autonomous_runner = autonomous_runner
        self._batch_window_seconds = batch_window_seconds
        # Set after construction via set_agent(): the main ChatAgent is built
        # from a SubsessionEnv that itself needs this ParentDelivery, so the
        # two can't be constructed in agent-first order (see set_agent).
        self._agent: ChatAgent | None = None
        # Per-session depth counter: how many nested reaction turns are
        # currently in flight for this session.  Once the depth reaches
        # _MAX_REACTION_DEPTH further closures degrade to passive records
        # so a broken tool loop cannot chain-react unboundedly.
        self._reaction_depth: dict[str, int] = {}
        # Keep strong references to in-flight background reaction tasks so
        # they aren't garbage-collected mid-run.
        self._reaction_tasks: set[asyncio.Task[None]] = set()
        # Batching: accumulate subsession outcomes that arrive within a
        # short window so they can be delivered in a single consolidated
        # agent turn instead of N separate near-identical replies.
        # _pending_outcomes maps session_id → list of (info, outcome, reason, label).
        self._pending_outcomes: dict[
            str, list[tuple[SubsessionInfo, str, str, str]]
        ] = {}
        # _pending_timers maps session_id → asyncio.Task that flushes the
        # batch after _BATCH_WINDOW_SECONDS.
        self._pending_timers: dict[str, asyncio.Task[None]] = {}

    def set_agent(self, agent: ChatAgent) -> None:
        """Wire the main chat agent used to react to subsession outcomes.

        Call once, after both this ``ParentDelivery`` and the main agent
        exist — the constructor can't take *agent* directly because
        building the main agent requires a ``SubsessionEnv`` that itself
        embeds this ``ParentDelivery`` (chicken-and-egg). Until this is
        called, main-chat-parent delivery degrades to a passive history
        record instead of a live reaction turn.
        """
        self._agent = agent

    def set_autonomous_runner(self, runner: AutonomousRunner) -> None:
        """Wire the autonomous runner for plan-aware reaction prompts.

        Call once, after both ``ParentDelivery`` and the
        ``AutonomousRunner`` exist — the runner depends on an agent
        factory that itself depends on the ``SubsessionEnv`` which
        embeds this ``ParentDelivery``, so the two can't be constructed
        in runner-first order (see :meth:`set_agent` for the same
        pattern).  Until this is called, reaction prompts use the
        default template regardless of autonomous state.
        """
        self._autonomous_runner = runner

    async def deliver_summary(
        self, info: SubsessionInfo, summary: str, reason: str
    ) -> None:
        """Deliver a terminal *summary* to *info*'s parent (see module doc).

        Best-effort: failures are logged, never raised back into a worker.

        Fire-and-forget: the reaction turn is scheduled as a background
        task so the caller (subsession worker / HTTP endpoint) returns
        immediately instead of blocking on the agent's LLM call.
        """
        # Suppress duplicate terminal reports: when a ticket has already
        # been reported as terminal by a prior monitor, skip delivery to
        # avoid a redundant (and often verbose) reaction turn.
        if reason in ("ticket_terminal", "completed"):
            ticket_id = _extract_ticket_id(info)
            if ticket_id is not None and self._registry.is_duplicate_ticket_terminal(
                ticket_id, info.id
            ):
                logger.info(
                    "Suppressing duplicate terminal report for ticket %s "
                    "from subsession %s — already reported by a prior monitor.",
                    ticket_id,
                    info.id[:8],
                )
                return

        # Suppress duplicate auto-pause / no-change reports: when a ticket
        # has already had an auto-pause, no-change, or terminal notice
        # reported by a prior monitor for the same ticket, skip delivery.
        # This prevents redundant "no change" reaction turns when multiple
        # periodic subsessions monitor the same ticket and all auto-pause
        # after consecutive no-change runs.
        if reason in ("paused", "no_change_auto_stop"):
            ticket_id = _extract_ticket_id(info)
            if ticket_id is not None and self._registry.is_duplicate_auto_pause(
                ticket_id, info.id
            ):
                logger.info(
                    "Suppressing duplicate auto-pause report for ticket %s "
                    "from subsession %s — already reported by a prior monitor.",
                    ticket_id,
                    info.id[:8],
                )
                return
            # Suppress auto-pause delivery when the monitored ticket is
            # already in a terminal state — stale monitors for
            # already-closed tickets should not distract the user.
            from .worker_mill import _TICKET_STATE_TERMINAL

            last_known = _extract_last_known_state(info)
            if last_known is not None and last_known.lower() in _TICKET_STATE_TERMINAL:
                logger.info(
                    "Suppressing auto-pause report for ticket %s "
                    "from subsession %s — ticket is already in a "
                    "terminal state (%s).",
                    ticket_id,
                    info.id[:8],
                    last_known,
                )
                return

        # For user_chat subsessions include the full transcript alongside
        # the agent-written summary so the parent can act on operator
        # decisions even when the summary is terse.
        outcome: str = summary
        if info.kind == SubsessionKind.USER_CHAT and info.transcript:
            outcome = _format_user_chat_outcome(summary, info.transcript)

        label = (
            f"[Subsession {info.id[:8]} ({info.kind.value}) '{info.title}' {reason}]"
        )
        try:
            if info.parent_id is not None:
                if not self._parent_is_periodic(info.parent_id):
                    if self._registry.enqueue_message(
                        info.parent_id, "parent", f"{label} {outcome}"
                    ):
                        return
                else:
                    # Parent is periodic — enqueue the completion into
                    # the parent's inbox so the periodic sees it on its
                    # next wake (prevents re-spawning a duplicate
                    # user_chat for the same ticket), AND schedule a
                    # reaction in the main chat so the operator sees the
                    # decision immediately even while the periodic is
                    # sleeping.  When the periodic parent is no longer
                    # active the enqueue is a silent no-op; the reaction
                    # still fires so the outcome is never lost.
                    self._registry.enqueue_message(
                        info.parent_id, "parent", f"{label} {outcome}"
                    )
                    self._schedule_reaction(info, outcome, reason, label)
                    return
            # Main-chat parent (parent_id is None) or nested parent
            # already terminal → relay to the owning session so the
            # outcome is never lost.
            self._schedule_reaction(info, outcome, reason, label)
        except Exception:
            logger.exception(
                "Failed to deliver subsession %s summary to its parent", info.id
            )

    # ------------------------------------------------------------------
    # Parent classification
    # ------------------------------------------------------------------

    def _parent_is_periodic(self, parent_id: str) -> bool:
        """Return True when *parent_id* is a periodic subsession.

        Children of periodic parents get dual delivery: the outcome is
        enqueued into the periodic parent's inbox (so the periodic sees
        completed children on its next wake and can suppress duplicate
        user_chat spawns for the same ticket) AND scheduled as a reaction
        in the main chat (so the operator sees decisions immediately even
        while the periodic is sleeping).  When the periodic parent is no
        longer active the enqueue is a silent no-op; the reaction still
        fires so the outcome is never lost.
        """
        parent = self._registry.get(parent_id)
        return parent is not None and parent.kind == SubsessionKind.PERIODIC

    # ------------------------------------------------------------------
    # Background reaction scheduling (fire-and-forget)
    # ------------------------------------------------------------------

    def _schedule_reaction(
        self, info: SubsessionInfo, outcome: str, reason: str, label: str
    ) -> None:
        """Schedule a background task to react to *outcome* in the main chat.

        Outcomes that arrive within ``_BATCH_WINDOW_SECONDS`` of each other
        are batched: the first outcome starts a timer; subsequent outcomes
        arriving before the timer fires are added to the same batch.  When
        the timer fires all pending outcomes are delivered in a single
        consolidated agent turn instead of N separate near-identical replies.

        The task is fire-and-forget — errors are logged, never surfaced
        to the caller.
        """
        session_id = info.owner_session_id

        # Depth-bounded loop guard: if we're already at max depth for this
        # session, record a passive entry instead of scheduling yet another
        # reaction.  This bounds chains like  close → reaction → spawn →
        # close → reaction → spawn → …  to _MAX_REACTION_DEPTH steps.
        depth = self._reaction_depth.get(session_id, 0)
        if depth >= _MAX_REACTION_DEPTH:
            logger.warning(
                "Reaction depth %d reached for session %s — "
                "degrading subsession %s outcome to passive record.",
                depth,
                session_id,
                info.id,
            )
            # Schedule the passive record under the lock so it doesn't race
            # a concurrent user turn.
            task = asyncio.create_task(self._record_passive(session_id, label, outcome))
            self._reaction_tasks.add(task)
            task.add_done_callback(self._reaction_tasks.discard)
            return

        # Cancel any existing timer — we restart the window so all outcomes
        # that arrive close together land in one batch.
        existing_timer = self._pending_timers.pop(session_id, None)
        if existing_timer is not None:
            existing_timer.cancel()

        # Add this outcome to the pending queue.
        self._pending_outcomes.setdefault(session_id, []).append(
            (info, outcome, reason, label)
        )

        # Schedule a flush after the batch window.
        timer = asyncio.create_task(self._flush_pending_reactions(session_id))
        self._pending_timers[session_id] = timer
        self._reaction_tasks.add(timer)
        timer.add_done_callback(self._reaction_tasks.discard)

    async def _safe_react(
        self, info: SubsessionInfo, outcome: str, reason: str, label: str
    ) -> None:
        """Wrap ``_react_in_main_chat`` with a top-level exception guard.

        Ensures that exceptions inside the background reaction task are
        logged and never silently swallowed (``asyncio.create_task`` does
        not propagate exceptions to the caller).
        """
        try:
            await self._react_in_main_chat(info, outcome, reason, label)
        except Exception:
            logger.exception("Reaction task failed for subsession %s", info.id)

    # ------------------------------------------------------------------
    # Outcome batching (fire-and-forget)
    # ------------------------------------------------------------------

    async def _flush_pending_reactions(self, session_id: str) -> None:
        """Wait for the batch window, then drain all pending outcomes.

        When the timer fires, all outcomes that accumulated for
        *session_id* are delivered in a single consolidated agent turn
        (or a single ``_react_in_main_chat`` call when only one outcome
        arrived).  Depth is incremented here so all outcomes in the batch
        count as one reaction turn.
        """
        if self._batch_window_seconds > 0:
            try:
                await asyncio.sleep(self._batch_window_seconds)
            except asyncio.CancelledError:
                # Timer was cancelled because a new outcome arrived before
                # the window closed — the new outcome started its own timer.
                return

        outcomes = self._pending_outcomes.pop(session_id, [])
        self._pending_timers.pop(session_id, None)

        if not outcomes:
            return

        # Increment depth once for the entire batch.
        depth = self._reaction_depth.get(session_id, 0) + 1
        self._reaction_depth[session_id] = depth

        if len(outcomes) == 1:
            info, outcome, reason, label = outcomes[0]
            await self._safe_react(info, outcome, reason, label)
        else:
            await self._safe_react_batched(session_id, outcomes)

    async def _safe_react_batched(
        self,
        session_id: str,
        outcomes: list[tuple[SubsessionInfo, str, str, str]],
    ) -> None:
        """Wrap ``_react_batched`` with a top-level exception guard."""
        try:
            await self._react_batched(session_id, outcomes)
        except Exception:
            logger.exception(
                "Batched reaction task failed for session %s (%d outcomes)",
                session_id,
                len(outcomes),
            )

    async def _react_batched(
        self,
        session_id: str,
        outcomes: list[tuple[SubsessionInfo, str, str, str]],
    ) -> None:
        """Deliver multiple subsession outcomes in a single consolidated turn.

        Builds one prompt listing all outcomes with a mandatory
        consolidation instruction, then runs a single agent turn.
        Degrades to passive records when no agent is wired or the turn
        fails — each outcome is recorded individually so none are lost.
        """
        try:
            if self._agent is None:
                async with self._run_serializer.for_owner(session_id):
                    for _info, outcome, _reason, label in outcomes:
                        self._store.record_for_session(session_id, label, outcome)
                return

            # When the main session has an active autonomous plan (proposal
            # or executing), degrade to individual _react_in_main_chat calls
            # rather than using the batch template.  _react_in_main_chat
            # already selects the correct active-plan prompt, and this
            # avoids creating a duplicate batch-aware active-plan template.
            # The batching window is an optimisation, not a correctness
            # requirement — individual turns are safe and correct here.
            if self._autonomous_runner is not None:
                aq = self._autonomous_runner.get_session(session_id)
                if aq is not None and aq.state in (
                    AutonomousState.proposal,
                    AutonomousState.executing,
                ):
                    for info, outcome, reason, label in outcomes:
                        await self._react_in_main_chat(info, outcome, reason, label)
                    return

            # Build the numbered outcome list.
            parts: list[str] = []
            for i, (info, outcome, reason, _label) in enumerate(outcomes, start=1):
                reason_text = _REASON_PHRASES.get(reason, reason)
                parts.append(
                    f"{i}. Subsession {info.id[:8]} ({info.kind.value}) "
                    f"'{info.title}' {reason_text}.\n"
                    f"   Outcome: {outcome}"
                )
            outcomes_list = "\n\n".join(parts)

            prompt = _BATCH_REACT_PROMPT_TEMPLATE.format(
                count=len(outcomes),
                outcomes_list=outcomes_list,
            )

            async with self._run_serializer.for_owner(session_id):
                history = self._store.history(session_id)
                try:
                    chunks = [
                        chunk
                        async for chunk in self._agent.stream(
                            prompt,
                            history=history or None,
                            session_id=session_id,
                            client_id=session_id,
                            trace_metadata={"subsession_batch": "true"},
                            trace_name="subsession-reaction-batch",
                        )
                    ]
                except Exception:
                    logger.exception(
                        "Batched reaction turn failed for session %s (%d outcomes)",
                        session_id,
                        len(outcomes),
                    )
                    for _info, outcome, _reason, label in outcomes:
                        self._store.record_for_session(session_id, label, outcome)
                    # Push a fallback notification so the user sees the
                    # outcomes even when the LLM API is unavailable.
                    if self._event_sink is not None:
                        summaries: list[str] = []
                        for info, outcome, reason, _label in outcomes:
                            reason_text = _REASON_PHRASES.get(reason, reason)
                            summaries.append(
                                f"• '{info.title}' ({info.kind.value}) "
                                f"{reason_text}: {outcome}"
                            )
                        fallback_msg = (
                            "[System] Background tasks completed:\n\n"
                            + "\n".join(summaries)
                        )
                        self._event_sink.publish(
                            session_id,
                            agent_message_frame(fallback_msg, time.time()),
                        )
                    return
                reply = "".join(chunks)
                reply = _sanitize_reaction_reply(reply)
                self._store.record_for_session(session_id, prompt, reply)
                if reply and self._event_sink is not None:
                    self._event_sink.publish(
                        session_id, agent_message_frame(reply, time.time())
                    )
        finally:
            depth = self._reaction_depth.get(session_id, 1) - 1
            if depth <= 0:
                self._reaction_depth.pop(session_id, None)
            else:
                self._reaction_depth[session_id] = depth

    async def _record_passive(self, session_id: str, label: str, outcome: str) -> None:
        """Record *outcome* as a passive, system-authored turn under the lock.

        Best-effort: errors are logged, never raised.
        """
        try:
            async with self._run_serializer.for_owner(session_id):
                self._store.record_for_session(session_id, label, outcome)
        except Exception:
            logger.exception(
                "Failed to record passive outcome for session %s", session_id
            )

    # ------------------------------------------------------------------
    # Reaction turn (runs inside a background task)
    # ------------------------------------------------------------------

    async def _react_in_main_chat(
        self, info: SubsessionInfo, outcome: str, reason: str, label: str
    ) -> None:
        """Have the main agent react to *outcome* in its own session.

        Runs a real agent turn (not just a passive history record) so the
        agent actually processes what happened and can comment on or
        continue from it, then pushes the reply live to a connected browser
        via ``agent_message_frame`` — there is no open ``/chat`` request to
        carry it, since this isn't a live user turn.

        Degrades to the old passive record (*label* as the "user" turn,
        *outcome* as the "assistant" reply) when no agent is wired yet (see
        :meth:`set_agent`) or the reaction turn itself fails — the outcome
        must never be silently lost either way.

        The depth counter is decremented in the ``finally`` block so it is
        always cleared, even when the task is cancelled.
        """
        session_id = info.owner_session_id
        try:
            if self._agent is None:
                async with self._run_serializer.for_owner(session_id):
                    self._store.record_for_session(session_id, label, outcome)
                return

            reason_text = _REASON_PHRASES.get(reason, reason)

            # When the main session has an active autonomous plan (awaiting
            # approval or mid-execution), use the active-plan template so
            # the agent acknowledges the subsession as a note and stays on
            # task rather than dropping the plan and re-requesting approval.
            autonomous_state_phrase: str | None = None
            plan_text: str | None = None
            if self._autonomous_runner is not None:
                aq = self._autonomous_runner.get_session(session_id)
                if aq is not None:
                    if aq.state is AutonomousState.proposal:
                        autonomous_state_phrase = (
                            "waiting for operator approval of your proposed plan"
                        )
                        plan_text = aq.plan_text
                    elif aq.state is AutonomousState.executing:
                        autonomous_state_phrase = "executing your approved plan"
                        plan_text = aq.plan_text

            if autonomous_state_phrase is not None and plan_text:
                prompt = _REACT_PROMPT_ACTIVE_PLAN_TEMPLATE.format(
                    sub_id=info.id[:8],
                    kind=info.kind.value,
                    title=info.title,
                    reason=reason_text,
                    autonomous_state_phrase=autonomous_state_phrase,
                    plan_text=plan_text,
                    outcome=outcome,
                )
            else:
                prompt = _REACT_PROMPT_TEMPLATE.format(
                    sub_id=info.id[:8],
                    kind=info.kind.value,
                    title=info.title,
                    reason=reason_text,
                    outcome=outcome,
                )
            async with self._run_serializer.for_owner(session_id):
                history = self._store.history(session_id)
                try:
                    parts = [
                        chunk
                        async for chunk in self._agent.stream(
                            prompt,
                            history=history or None,
                            session_id=session_id,
                            client_id=session_id,
                            trace_metadata={"subsession_id": info.id},
                            trace_name="subsession-reaction",
                        )
                    ]
                except Exception:
                    logger.exception(
                        "Reaction turn failed for subsession %s (session %s)",
                        info.id,
                        session_id,
                    )
                    self._store.record_for_session(session_id, label, outcome)
                    # Push a fallback notification so the user sees the
                    # outcome even when the LLM API is unavailable — the
                    # connected browser renders it as a normal chat bubble.
                    if self._event_sink is not None:
                        kind_label = info.kind.value
                        fallback_msg = (
                            f"[System] Background task '{info.title}' ({kind_label}) "
                            f"{reason_text}.\n\n{outcome}"
                        )
                        self._event_sink.publish(
                            session_id,
                            agent_message_frame(fallback_msg, time.time()),
                        )
                    return
                reply = "".join(parts)
                reply = _sanitize_reaction_reply(reply)
                self._store.record_for_session(session_id, prompt, reply)
                if reply and self._event_sink is not None:
                    self._event_sink.publish(
                        session_id, agent_message_frame(reply, time.time())
                    )
        finally:
            depth = self._reaction_depth.get(session_id, 1) - 1
            if depth <= 0:
                self._reaction_depth.pop(session_id, None)
            else:
                self._reaction_depth[session_id] = depth
