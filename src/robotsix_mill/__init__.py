"""Shadow package: override ``stages/towncrier.py`` with a patched version.

Python finds this ``__init__.py`` first (``src/`` is at position 1 in
``sys.path`` via ``PYTHONPATH``).  We delegate to the installed
``robotsix_mill`` package while injecting our local overrides into
the ``stages`` sub-package's ``__path__`` so that ``stages/towncrier``
is loaded from here.

All other submodules (including ``_resources``) resolve from the
installed package, so ``importlib.resources.files`` and ``__file__``-
relative lookups continue to work correctly.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL_DIR = Path(__file__).parent
_LOCAL_STAGES = str(_LOCAL_DIR / "stages")

# ---------------------------------------------------------------------------
# 1.  Temporarily remove ``src/`` from sys.path so ``import robotsix_mill``
#     finds the installed package, not ourselves.
# ---------------------------------------------------------------------------
_src_parent = str(_LOCAL_DIR.parent)  # the ``src/`` directory
_src_entries = [p for p in sys.path if p == _src_parent]
for p in _src_entries:
    sys.path.remove(p)

# ---------------------------------------------------------------------------
# 2.  Discard our half-built module object and import the real package.
# ---------------------------------------------------------------------------
del sys.modules["robotsix_mill"]
import robotsix_mill  # noqa: E402 — must happen after sys.path manipulation

# ---------------------------------------------------------------------------
# 3.  Restore sys.path so the rest of the process can find src/ modules.
# ---------------------------------------------------------------------------
for p in reversed(_src_entries):
    sys.path.insert(0, p)

# ---------------------------------------------------------------------------
# 4.  Eagerly import ``stages`` and inject our local override directory
#     at the front of its ``__path__`` so ``towncrier.py`` is loaded from
#     our local copy.  Must happen BEFORE any code imports
#     ``robotsix_mill.stages.towncrier``.
# ---------------------------------------------------------------------------
import robotsix_mill.stages  # noqa: E402

if _LOCAL_STAGES not in robotsix_mill.stages.__path__:
    robotsix_mill.stages.__path__.insert(0, _LOCAL_STAGES)

# ---------------------------------------------------------------------------
# 4.5.  Eagerly import ``agents.runners`` and inject our local override
#       directory at the front of its ``__path__`` so
#       ``diagnostic_events.py`` is loaded from our local copy.  Must
#       happen BEFORE any code imports
#       ``robotsix_mill.agents.runners.diagnostic_events``.
#
#       The ``agents`` package eagerly imports ``runners`` (and
#       ``runners`` eagerly imports ``diagnostic_events``), so by the
#       time we reach this point ``diagnostic_events`` is already
#       cached in ``sys.modules`` from the site-packages copy.  We
#       eject the cached module and re-import after injecting the
#       local path so our shadow is used instead.
# ---------------------------------------------------------------------------
_LOCAL_AGENTS_RUNNERS = str(_LOCAL_DIR / "agents" / "runners")

# The ``agents`` package load above (via ``import robotsix_mill``) already
# pulled in ``runners`` → ``diagnostic_events``.  Force our local path into
# ``__path__`` and then reload the shadowed runner modules so the local
# copies win regardless of import order.
import robotsix_mill.agents.runners  # noqa: E402

if _LOCAL_AGENTS_RUNNERS not in robotsix_mill.agents.runners.__path__:
    robotsix_mill.agents.runners.__path__.insert(0, _LOCAL_AGENTS_RUNNERS)

if "robotsix_mill.agents.runners.diagnostic_events" in sys.modules:
    del sys.modules["robotsix_mill.agents.runners.diagnostic_events"]
import robotsix_mill.agents.runners.diagnostic_events  # noqa: E402

# ``diagnostic_check_recurring_ci`` registers itself (register_check
# side-effect) against the installed ``diagnostic_checks`` registry when
# ``diagnostic_runner`` imports it.  Eject any cached installed copy and
# re-import from the shadow so the new ``MultiCauseCIFailureCheck`` is
# registered instead of the installed check module.
if "robotsix_mill.agents.runners.diagnostic_check_recurring_ci" in sys.modules:
    del sys.modules["robotsix_mill.agents.runners.diagnostic_check_recurring_ci"]
import robotsix_mill.agents.runners.diagnostic_check_recurring_ci  # noqa: E402

# ``diagnostic_check_oversized_ticket`` registers itself (register_check
# side-effect) against the installed ``diagnostic_checks`` registry.
# Eject any cached installed copy and re-import from the shadow so the
# new ``OversizedTicketCheck`` is registered.
if "robotsix_mill.agents.runners.diagnostic_check_oversized_ticket" in sys.modules:
    del sys.modules["robotsix_mill.agents.runners.diagnostic_check_oversized_ticket"]
import robotsix_mill.agents.runners.diagnostic_check_oversized_ticket  # noqa: E402

# ---------------------------------------------------------------------------
# 5.  Patch ``load_agent_definition`` to prefer local overrides in
#     ``agent_definitions/``.  When a YAML file exists under our local
#     ``src/robotsix_mill/agent_definitions/`` directory, use it instead
#     of the installed copy.  This allows the repo to extend or override
#     agent guidance (e.g. add a CI workflow edit checklist) without
#     forking the entire mill package.
# ---------------------------------------------------------------------------
import robotsix_mill.agents.yaml_loader  # noqa: E402

_original_load_agent_definition = robotsix_mill.agents.yaml_loader.load_agent_definition
_LOCAL_AGENT_DEFINITIONS = str(_LOCAL_DIR / "agent_definitions")


from robotsix_mill.agents.yaml_loader import AgentDefinition  # noqa: E402


def _load_with_local_overrides(path: Path) -> AgentDefinition:
    local_path = Path(_LOCAL_AGENT_DEFINITIONS) / path.name
    if local_path.is_file():
        return _original_load_agent_definition(local_path)
    return _original_load_agent_definition(path)


robotsix_mill.agents.yaml_loader.load_agent_definition = _load_with_local_overrides

# ---------------------------------------------------------------------------
# 6.  Patch the implement stage's post-summary verification gate to also run
#     the repo's ``scripts/verify_changelog_fragment.py <ticket_id>`` before
#     the ready transition is emitted.  This replaces the advisory prompt
#     instruction with a programmatic gate: a non-zero exit blocks the ready
#     transition and sends the agent back into the implement loop with the
#     script's error.  Internal-only changes short-circuit via the script's
#     ``--skip`` flag, so the gate passes through for them.
# ---------------------------------------------------------------------------
import robotsix_mill.stages.changelog_gate  # noqa: E402
from robotsix_mill.core.states import State  # noqa: E402
from robotsix_mill.stages.base import Outcome  # noqa: E402
from robotsix_mill.stages.changelog_gate import (  # noqa: E402
    run_changelog_fragment_gate,
)
from robotsix_mill.stages.implement import implementation_logic  # noqa: E402
from robotsix_mill.stages.implement._shared import (  # noqa: E402
    _ImplementContext,
    _SinglePassResult,
)

_original_run_summary_verification = (
    implementation_logic.ImplementationLogicMixin._run_summary_verification
)


@classmethod
def _run_summary_verification_with_changelog_gate(
    cls,
    ticket,
    repo_dir,
    summary,
    ic,
    updated_ref_files,
    updated_prev_summary,
    new_msgs,
    target_branch="main",
):
    """Run the original claim check, then the changelog-fragment gate."""
    result = _original_run_summary_verification.__func__(
        cls,
        ticket,
        repo_dir,
        summary,
        ic,
        updated_ref_files,
        updated_prev_summary,
        new_msgs,
        target_branch,
    )
    if result is not None:
        return result

    error = run_changelog_fragment_gate(repo_dir, ticket.id)
    if error is None:
        return None

    feedback = f"[CHANGELOG] {error}"
    if (ic.feedback or "").startswith("[CHANGELOG]"):
        robotsix_mill.stages.changelog_gate.log.warning(
            "%s: changelog fragment verification failed again — %s; blocking",
            ticket.id,
            error,
        )
        return _SinglePassResult(
            next_action="return",
            outcome=Outcome(
                State.BLOCKED,
                f"changelog fragment verification failed after retry: {error}",
            ),
        )

    robotsix_mill.stages.changelog_gate.log.warning(
        "%s: changelog fragment verification failed — %s; re-prompting",
        ticket.id,
        error,
    )
    verify_ic = _ImplementContext(
        spec=ic.spec,
        memory_text=ic.memory_text,
        reference_files=updated_ref_files,
        file_map=ic.file_map,
        feedback=feedback,
        previous_attempt_summary=updated_prev_summary,
        open_thread_ids=ic.open_thread_ids,
    )
    return _SinglePassResult(
        next_action="retry",
        feedback=feedback,
        ic=verify_ic,
        new_msgs=new_msgs,
    )


implementation_logic.ImplementationLogicMixin._run_summary_verification = (
    _run_summary_verification_with_changelog_gate
)

# ---------------------------------------------------------------------------
# 7.  Auto-approve chat-agent-filed tickets.  The installed refine stage's
#     ``_AUTO_APPROVE_SOURCES`` set is the deterministic shortlist of ticket
#     sources that skip the human approval gate after refinement.  Tickets
#     filed by the robotsix-chat assistant via ``POST /tickets/ingest``
#     (``source="robotsix-chat"``) were missing from it, so the assistant's
#     own improvement tickets stalled in ``human_issue_approval`` until a
#     human nudged them forward.  Merge the local extension so those
#     tickets flow ``draft -> refine -> ready`` on their own.
# ---------------------------------------------------------------------------
import robotsix_mill.stages.refine.helpers as _refine_helpers  # noqa: E402
from robotsix_mill.stages.refine_autoapprove import merge_auto_approve  # noqa: E402

if not merge_auto_approve(_refine_helpers):
    # Fail soft: a mill upgrade renamed ``_AUTO_APPROVE_SOURCES`` or
    # changed its type (e.g. to a frozenset).  Log and continue instead
    # of breaking every startup at import time — tickets stay behind the
    # human approval gate until the patch is updated for the new mill API.
    robotsix_mill.stages.changelog_gate.log.warning(
        "refine helpers._AUTO_APPROVE_SOURCES is %r, not a mutable set; "
        "skipping auto-approve merge for robotsix-chat tickets",
        getattr(_refine_helpers, "_AUTO_APPROVE_SOURCES", None),
    )
# ---------------------------------------------------------------------------
# 7.5.  Patch the refine stage's inflight-advisory step to also run an
#       auto-merge pre-flight check.  When the target repo is on the
#       infra denylist, auto-merge is globally disabled, or the draft
#       references files matching auto-merge-sensitive globs, an advisory
#       comment is posted during refinement so the operator is alerted
#       early — rather than discovering the block only after
#       implementation and CI pass.
# ---------------------------------------------------------------------------
from robotsix_mill.stages.refine.gates import RefineGatesMixin  # noqa: E402
from robotsix_mill.stages.refine_auto_merge_preflight import (  # noqa: E402
    run_auto_merge_preflight,
)

_original_run_inflight_advisory = RefineGatesMixin._run_inflight_advisory


@staticmethod
def _run_inflight_advisory_with_auto_merge_preflight(ctx, ticket, draft, ws, s):
    draft = _original_run_inflight_advisory(ctx, ticket, draft, ws, s)
    run_auto_merge_preflight(ctx, ticket, draft, s)
    return draft


RefineGatesMixin._run_inflight_advisory = staticmethod(
    _run_inflight_advisory_with_auto_merge_preflight
)
# ---------------------------------------------------------------------------
# 8.  Patch the review stage's verdict handling to re-verify any changelog
#     fragment claimed in the implement rebuttal (``implement.md``) against
#     the committed branch diff.  The pre-ready gate above checks the
#     working tree before ``ready``; this closes the post-ready gap where an
#     implement agent claims a fragment during review but it is never
#     committed — the reviewer must not accept the verbal claim.
# ---------------------------------------------------------------------------
import robotsix_mill.stages.review as _review  # noqa: E402
from robotsix_mill.agents.reviewing import ReviewAsk  # noqa: E402
from robotsix_mill.stages.changelog_gate import (  # noqa: E402
    run_review_changelog_fragment_gate,
)

_original_handle_review_verdict = _review.ReviewStage._handle_review_verdict


def _handle_review_verdict_with_fragment_gate(
    self,
    verdict,
    ticket,
    ctx,
    ws,
    s,
    input_hash,
    modified_paths,
    repo_dir,
):
    """Verify a claimed changelog fragment before accepting a verdict.

    When the implement rebuttal claims a fragment was added but the
    committed diff does not contain it, force REQUEST_CHANGES (and enrich
    the verdict's asks/comments) instead of trusting the claim.  A
    NEEDS_DISCUSSION verdict is left untouched — it pauses for a human
    decision rather than an implement rebuttal.
    """
    if verdict.verdict in ("APPROVE", "REQUEST_CHANGES"):
        implement_md = ws.artifacts_dir / "implement.md"
        rebuttal = (
            implement_md.read_text(encoding="utf-8") if implement_md.exists() else ""
        )
        error = run_review_changelog_fragment_gate(
            ticket.id,
            repo_dir,
            rebuttal,
            modified_paths,
        )
        if error is not None:
            _review.log.warning(
                "%s: review changelog fragment gate failed — %s; "
                "forcing REQUEST_CHANGES",
                ticket.id,
                error,
            )
            verdict.verdict = "REQUEST_CHANGES"
            verdict.auto_merge_eligible = False
            verdict.comments = (
                f"Changelog fragment verification failed: {error}\n\n"
                + (verdict.comments or "")
            )
            # Empty ``files_touched`` keeps the ask in-scope (file-less), so
            # the ticket bounces back to implement instead of spawning a
            # dependency ticket for the missing fragment.
            verdict.request_changes = [
                ReviewAsk(
                    title=f"Commit changelog fragment for {ticket.id}",
                    description=error,
                    files_touched=[],
                ),
                *verdict.request_changes,
            ]
    return _original_handle_review_verdict(
        self,
        verdict,
        ticket,
        ctx,
        ws,
        s,
        input_hash,
        modified_paths,
        repo_dir,
    )


_review.ReviewStage._handle_review_verdict = _handle_review_verdict_with_fragment_gate

# ---------------------------------------------------------------------------
# 9.  Patch the implement stage's agent-spawn abort handler so that a spawn
#     that aborts before/without producing LLM work still leaves visible
#     breadcrumbs.  ``_invoke_implement_agent`` re-raises transient causes
#     without calling ``_finalize`` (deliberately — no spec fingerprint on
#     an env failure), and any exception escaping its handlers is only
#     caught by the worker's generic "processing crashed" log line.  Both
#     paths leave no error on the ticket: the next preflight increments the
#     spawn counter, and the ticket silently exhausts its spawn limit with
#     no diagnosable root cause.  Record the exception in
#     ``implement_summary.md`` (surfaced in the spawn-limit block note),
#     emit a diagnostic event, and add a ticket comment — then re-raise so
#     the worker's transient/fatal classification still owns retry/block
#     routing.
# ---------------------------------------------------------------------------
import hashlib  # noqa: E402
import logging  # noqa: E402

from robotsix_mill.agents.runners.diagnostic_events import (  # noqa: E402
    emit_diagnostic_event,
)
from robotsix_mill.stages.implement import phase_coordinator  # noqa: E402

_spawn_abort_log = logging.getLogger("robotsix_mill.implement_spawn_abort")

_original_invoke_implement_agent = (
    implementation_logic.ImplementationLogicMixin._invoke_implement_agent
)


@classmethod
def _invoke_implement_agent_with_abort_breadcrumbs(
    cls,
    ctx,
    ticket,
    repo_dir,
    branch,
    settings,
    ic,
    language_instructions,
    agent_level,
    resume_history,
    extra_roots,
    memory_board_id,
    ws=None,
    target_branch="main",
):
    """Run the original spawn, recording the abort before re-raising.

    The original method handles ``AgentBudgetError`` / ``AgentRunError``
    internally (``_finalize`` + BLOCKED outcome), but re-raises transient
    causes — and any exception escaping its handlers propagates all the
    way to the worker's bare log line.  Both escapes leave the ticket
    untouched, so the next poll re-spawns and burns another spawn slot
    with no visible error.  Record breadcrumbs on every exception that
    escapes, then re-raise unchanged.
    """
    try:
        return _original_invoke_implement_agent.__func__(
            cls,
            ctx,
            ticket,
            repo_dir,
            branch,
            settings,
            ic,
            language_instructions,
            agent_level,
            resume_history,
            extra_roots,
            memory_board_id,
            ws,
            target_branch,
        )
    except Exception as exc:
        _record_implement_spawn_abort(ctx, ticket, exc, ws)
        raise


def _record_implement_spawn_abort(
    ctx,
    ticket,
    exception,
    ws=None,
    *,
    write_summary: bool = True,
    dedup_comment: bool = False,
) -> None:
    """Log *exception* into the ticket's block note + emit a trace event.

    Best-effort on all three sinks — an abort bookkeeping failure must
    never mask the original exception.  The original exception is
    re-raised by the caller so the worker's ``_handle_stage_error``
    still decides retry vs block.

    ``write_summary`` — append a ``[SPAWN ABORT]`` entry to
    ``implement_summary.md`` so the spawn-limit block note's "Last
    attempt summary tail" surfaces the genuine cause.  Disabled for
    preflight aborts, which burn no spawn slot and should not touch
    pass artifacts.

    ``dedup_comment`` — skip the ticket comment when an identical
    mill-authored comment already exists.  Preflight runs on every
    poll cycle, so an outage must not pile one comment per cycle.
    """
    error_text = f"{type(exception).__name__}: {exception!s}"[:500]

    # 1. Append to implement_summary.md so the spawn-limit block note's
    #    "Last attempt summary tail" surfaces the genuine cause.
    if write_summary:
        try:
            if ws is None:
                ws = ctx.service.workspace(ticket)
            summary_path = ws.artifacts_dir / "implement_summary.md"
            existing = ""
            try:
                existing = summary_path.read_text(encoding="utf-8") or ""
            except OSError:
                existing = ""
            summary_path.write_text(
                existing + f"\n[SPAWN ABORT] {error_text}\n",
                encoding="utf-8",
            )
        except Exception:
            _spawn_abort_log.warning(
                "%s: failed to write spawn-abort summary", ticket.id, exc_info=True
            )

    # 2. Emit a structured diagnostic event so the failure is
    #    discoverable programmatically (e.g. by the periodic diagnostic
    #    agent).  Deduplicated on (ticket_id, normalized_key).
    try:
        board_id = ctx.memory_board_id(ticket)
        normalized_key = (
            "IMPLEMENT_SPAWN_ABORT:"
            + hashlib.sha256(
                f"{ticket.id}:{type(exception).__name__}".encode()
            ).hexdigest()[:16]
        )
        emit_diagnostic_event(
            ctx.settings,
            board_id,
            category="IMPLEMENT_SPAWN_ABORT",
            ticket_id=ticket.id,
            reason=error_text,
            normalized_key=normalized_key,
        )
    except Exception:
        _spawn_abort_log.warning(
            "%s: failed to emit spawn-abort diagnostic event",
            ticket.id,
            exc_info=True,
        )

    # 3. Add a ticket comment so the error is visible on the board
    #    immediately, without waiting for the spawn-limit block note.
    #    Authored by "mill" so it is filtered from implement feedback.
    try:
        comment_body = f"[implement-spawn-abort] {error_text}"
        if dedup_comment:
            for comment in ctx.service.list_comments(ticket.id):
                if comment.body == comment_body and comment.author == "mill":
                    break
            else:
                ctx.service.add_comment(ticket.id, comment_body, author="mill")
        else:
            ctx.service.add_comment(ticket.id, comment_body, author="mill")
    except Exception:
        _spawn_abort_log.warning(
            "%s: failed to add spawn-abort comment", ticket.id, exc_info=True
        )


implementation_logic.ImplementationLogicMixin._invoke_implement_agent = (
    _invoke_implement_agent_with_abort_breadcrumbs
)

# --- preflight abort handler ---------------------------------------------
# The worker calls ``stage.preflight`` BEFORE the ticket root span opens
# and outside its stage-error try/except.  An exception there escapes to
# the consumer loop's bare "processing crashed" log line: no Langfuse
# trace, no retry breadcrumb, no error on the ticket — and the next poll
# re-runs preflight identically.  Record the abort, then route: transient
# failures re-raise (preserving the silent self-retry that keeps an
# outage from mass-blocking the board), while fatal failures return a
# BLOCKED outcome whose note carries the exception so a human can act.
_original_implement_preflight = phase_coordinator.PhaseCoordinatorMixin.preflight


def _preflight_with_abort_breadcrumbs(self, ticket, ctx):
    try:
        return _original_implement_preflight(self, ticket, ctx)
    except Exception as exc:
        _record_implement_spawn_abort(
            ctx,
            ticket,
            exc,
            write_summary=False,
            dedup_comment=True,
        )
        try:
            from robotsix_mill.runtime.transient_errors import classify_stage_error

            transient = classify_stage_error(exc) == "transient"
        except Exception:
            # Unknown classification — keep the established silent-retry
            # behaviour rather than mass-blocking on a classifier bug.
            transient = True
        if transient:
            raise
        return Outcome(
            State.BLOCKED,
            f"implement preflight crashed: {type(exc).__name__}: {exc}"[:200],
        )


phase_coordinator.PhaseCoordinatorMixin.preflight = _preflight_with_abort_breadcrumbs

# ---------------------------------------------------------------------------
# 10. Patch resume_blocked to refuse resuming tickets whose block event
#     lacks an error summary or trace.  A ticket blocked by spawn-limit
#     exhaustion from silent aborts (no LLM work produced, e.g. preflight
#     or implement-spawn crashes caught only as bare exceptions) carries
#     only generic boilerplate with no diagnosis; resuming it clears the
#     counter and re-exhausts the budget invisibly.  The guard inspects
#     the most recent BLOCKED event's note: if it lacks a diagnostic
#     summary tail, exception marker, or spawn-abort breadcrumb, a
#     "[needs-investigation]" comment is posted and the resume is refused
#     (TransitionError) — keeping the ticket BLOCKED for manual review.
# ---------------------------------------------------------------------------
import re  # noqa: E402

from robotsix_mill.core.models import Ticket  # noqa: E402
from robotsix_mill.core.service import TransitionError  # noqa: E402
from robotsix_mill.core.service._transition_mixin import (  # noqa: E402
    _TransitionMixin,
)

_original_resume_blocked = _TransitionMixin.resume_blocked

# Regex matching a note that carries a substantive diagnosis: a non-empty
# summary tail, a spawn-abort breadcrumb, a ``SomethingError:/Exception:``
# pattern, or a traceback marker.
_DIAGNOSIS_RE = re.compile(
    r"Last attempt summary tail:\s*\n\s*\S"  # non-empty summary tail
    r"|\[SPAWN ABORT\]"  # spawn-abort breadcrumb line
    r"|\b\w+(?:Error|Exception):\s"  # exception class + colon + message
    r"|Traceback\b"  # Python traceback marker
)

_NEEDS_INVESTIGATION_BODY = (
    "[needs-investigation] This blocked ticket has no error summary or trace "
    "in its block event — resuming would re-exhaust the spawn budget "
    "invisibly.  The ticket needs manual investigation to identify the "
    "root cause before resuming."
)


def _resume_blocked_with_diagnosis_guard(
    self, ticket_id: str, note: str = ""
) -> Ticket:
    """Refuse resume when the block event carries no diagnosis.

    For BLOCKED tickets only (retry-attempt tickets are exempt), fetch
    the most recent ``TicketEvent`` with ``state == BLOCKED`` and check
    whether its ``note`` contains a substantive error summary or trace.
    When no diagnosis is found the ticket is left BLOCKED, a
    ``[needs-investigation]`` comment is posted, and a
    :class:`TransitionError` is raised.
    """
    ticket = self.get(ticket_id)
    if ticket is not None and ticket.state is State.BLOCKED:
        events = self.history(ticket_id, order="desc", limit=50)
        block_event = next((e for e in events if e.state == State.BLOCKED), None)
        if block_event is None or not _DIAGNOSIS_RE.search(block_event.note or ""):
            self.add_comment(
                ticket_id,
                _NEEDS_INVESTIGATION_BODY,
                author="system",
            )
            raise TransitionError(
                f"{ticket_id}: resume refused — the block event contains "
                "no error summary or trace (likely a silent spawn abort "
                "with no diagnosis).  See the [needs-investigation] comment "
                "on the ticket."
            )
    return _original_resume_blocked(self, ticket_id, note=note)


_TransitionMixin.resume_blocked = _resume_blocked_with_diagnosis_guard


# ---------------------------------------------------------------------------
# 7.  Fix-ticket close cross-check.  A CI-fix ticket (spawned to repair a
#     red main CI) that reaches DONE without any evidence of landing — no
#     merged PR and no change on its branch — is a false close: the
#     implement worker reported success but the repo never changed.  Before
#     the automated DONE transition fires, verify one of those two signals;
#     otherwise raise TransitionError so the worker escalates to BLOCKED
#     instead of reporting done.
# ---------------------------------------------------------------------------
import robotsix_mill.stages.fix_close_verify  # noqa: E402
from robotsix_mill.stages.fix_close_verify import (  # noqa: E402
    is_fix_ticket,
    verify_fix_ticket_landed,
)

_original_transition = _TransitionMixin.transition


def _forge_for_board(settings, board_id):
    """Best-effort forge adapter for *board_id* (``None`` when unresolvable)."""
    try:
        from robotsix_mill.config import get_repos_config
        from robotsix_mill.forge import get_forge

        repo_config = next(
            (rc for rc in get_repos_config().repos.values() if rc.board_id == board_id),
            None,
        )
        return get_forge(settings, repo_config=repo_config)
    except Exception:
        return None


def _transition_with_fix_close_guard(
    self, ticket_id: str, dst: State, note: str | None = None
) -> Ticket:
    """Run the fix-ticket close cross-check before a DONE transition."""
    if dst is State.DONE:
        ticket = self.get(ticket_id)
        if ticket is not None and is_fix_ticket(ticket):
            error = verify_fix_ticket_landed(
                ticket,
                self.workspace(ticket).repo_dir,
                branch_prefix=self.settings.branch_prefix,
                target_branch=self.settings.forge_target_branch,
                forge=_forge_for_board(self.settings, ticket.board_id or ""),
            )
            if error is not None:
                raise TransitionError(error)
    return _original_transition(self, ticket_id, dst, note)


_TransitionMixin.transition = _transition_with_fix_close_guard


# ---------------------------------------------------------------------------
# 8.  Capability-gap detection in the implement loop's _finalize.
#
#     When the implement agent blocks with a diagnosis matching a known
#     unfixable-by-code pattern (version-solve failure, toolchain
#     incompatibility, cascading build-system errors), tag the ticket with
#     "capability-gap" and record a step event so the operator sees the
#     escalation immediately rather than after the agent exhausts its
#     retry budget.
# ---------------------------------------------------------------------------
import json  # noqa: E402
import re  # noqa: E402

from robotsix_mill.stages.implement import phase_coordinator  # noqa: E402

# Patterns that indicate a fundamental toolchain / dependency problem
# that code edits cannot resolve.  Matched against the ``summary``
# parameter of ``_finalize`` (the BLOCKED note from the implement loop).
_CAPABILITY_GAP_PATTERNS: list[re.Pattern[str]] = [
    # Version solve / dependency resolution failures (pub, pip, npm, cargo)
    re.compile(
        r"version.?solve|dependency.?resolution.?fail|could not resolve.*dependenc",
        re.IGNORECASE,
    ),
    # Toolchain / SDK incompatibilities
    re.compile(
        r"toolchain.?incompatib|sdk.?version.?incompatib"
        r"|incompatible.*(?:sdk|toolchain|ndk|jdk)",
        re.IGNORECASE,
    ),
    # Build system version mismatches (Gradle, CMake)
    re.compile(
        r"(?:gradle|cmake).*(?:version.?mismatch|incompatible.?version"
        r"|requires.*version)",
        re.IGNORECASE,
    ),
    # Kotlin/Java JVM version conflicts
    re.compile(
        r"(?:kotlin|java).*(?:jvm.?version|version.?conflict|incompatible.?jvm)",
        re.IGNORECASE,
    ),
    # Package manager version conflicts (npm ERESOLVE, pip resolution)
    re.compile(
        r"ERESOLVE|resolution.?impossible|no.?matching.?version"
        r"|Cannot.?install.*incompatible",
        re.IGNORECASE,
    ),
]


def _matches_capability_gap(summary: str) -> re.Match[str] | None:
    """Return the first matching capability-gap pattern, or ``None``."""
    for pat in _CAPABILITY_GAP_PATTERNS:
        m = pat.search(summary)
        if m is not None:
            return m
    return None


_original_finalize = phase_coordinator.PhaseCoordinatorMixin._finalize


@classmethod  # type: ignore[misc]
def _finalize_with_capability_gap(
    cls,
    ctx,
    ticket,
    repo_dir,
    branch,
    summary,
    *,
    ok: bool,
    reference_files=None,
    extra_roots=None,
    transient: bool = False,
    write_spec_fingerprint: bool = True,
):
    """Run the original ``_finalize``, then tag capability-gap blocks."""
    _original_finalize.__func__(
        cls,
        ctx,
        ticket,
        repo_dir,
        branch,
        summary,
        ok=ok,
        reference_files=reference_files,
        extra_roots=extra_roots,
        transient=transient,
        write_spec_fingerprint=write_spec_fingerprint,
    )
    # Only tag BLOCKED (non-transient) outcomes that match a known
    # capability-gap pattern.
    if ok or transient:
        return
    match = _matches_capability_gap(summary or "")
    if match is None:
        return

    # --- tag the ticket with "capability-gap" label ---
    try:
        existing: list[str] = json.loads(ticket.labels) if ticket.labels else []
    except json.JSONDecodeError, TypeError:
        existing = []
    if "capability-gap" not in existing:
        existing.append("capability-gap")
        try:
            ctx.service.set_labels(ticket.id, existing)
        except Exception:
            phase_coordinator.log.warning(
                "%s: failed to set capability-gap label",
                ticket.id,
                exc_info=True,
            )

    # --- record a step event with the escalation summary ---
    gap_summary = f"capability-gap detected: {match.group()!r} — {summary[:300]}"
    try:
        ctx.service.add_step_event(ticket.id, gap_summary)
    except Exception:
        phase_coordinator.log.warning(
            "%s: failed to add capability-gap step event",
            ticket.id,
            exc_info=True,
        )


phase_coordinator.PhaseCoordinatorMixin._finalize = _finalize_with_capability_gap

# ---------------------------------------------------------------------------
# 9.  Expand request-implementation-changes to accept IMPLEMENT_COMPLETE
#     and FIXING_CI, not just HUMAN_MR_APPROVAL.
#
#     When auto-merge is enabled the merge stage can drive a ticket from
#     IMPLEMENT_COMPLETE straight to DONE without ever touching
#     HUMAN_MR_APPROVAL, so an operator who wants rework has no window
#     to call request-implementation-changes.  Adding IMPLEMENT_COMPLETE
#     and FIXING_CI as accepted source states gives the operator a
#     rework window at every merge-pipeline stage.
#
#     Two patches:
#     a) Add State.READY to the TRANSITIONS entries for IMPLEMENT_COMPLETE
#        and FIXING_CI so the state machine allows the transition.
#     b) Replace request_implementation_changes on _TransitionMixin so it
#        accepts the expanded set of source states.
# ---------------------------------------------------------------------------
from datetime import UTC, datetime  # noqa: E402

import robotsix_mill.core.states as _states  # noqa: E402

# 9a. Expand the TRANSITIONS dict.
_states.TRANSITIONS[State.IMPLEMENT_COMPLETE].add(State.READY)
_states.TRANSITIONS[State.FIXING_CI].add(State.READY)

# 9b. Patch request_implementation_changes.
from robotsix_mill.core.service._transition_mixin import (  # noqa: E402
    _clear_stale_implement_guard,
    _reset_implement_spawn_counter,
)

# Kept for debugging / rollback: allows reverting to the original method
# by assigning ``_TransitionMixin.request_implementation_changes =
# _original_request_impl_changes``.
_original_request_impl_changes = _TransitionMixin.request_implementation_changes

# Source states from which an operator can request implementation changes.
_REQUEST_IMPL_CHANGES_STATES: frozenset[State] = frozenset(
    {
        State.HUMAN_MR_APPROVAL,
        State.IMPLEMENT_COMPLETE,
        State.FIXING_CI,
    }
)


def _request_implementation_changes_expanded(
    self, ticket_id: str, body: str, author: str = "user"
):
    """Send a ticket back to implement for rework.

    Accepts tickets in ``human_mr_approval``, ``implement_complete``, or
    ``fixing_ci``.  The original method only accepted
    ``human_mr_approval``, which left no rework window when auto-merge
    drove the ticket straight from ``implement_complete`` to ``done``.
    """
    if not body.strip():
        from robotsix_mill.core.service._helpers import TransitionError

        raise TransitionError(
            f"{ticket_id}: cannot request implementation changes — "
            "a non-empty body is required; it is the only thing that "
            "tells the implement agent what to change"
        )
    from robotsix_mill.core.db import retry_on_db_full
    from robotsix_mill.core.service._helpers import (
        TransitionError,
        _get_ticket,
        _make_event,
    )

    with retry_on_db_full(self.settings, self._board_for(ticket_id)) as s:
        ticket = _get_ticket(s, ticket_id)
        if ticket.state not in _REQUEST_IMPL_CHANGES_STATES:
            allowed = ", ".join(sorted(sv.value for sv in _REQUEST_IMPL_CHANGES_STATES))
            raise TransitionError(
                f"{ticket_id}: cannot request implementation changes — "
                f"not in an accepted state (currently {ticket.state}; "
                f"accepted: {allowed})"
            )
        from robotsix_mill.core.models import Comment

        comment = Comment(ticket_id=ticket_id, body=body, author=author)
        s.add(comment)
        note = f"implementation changes requested: {body}"
        old_state = ticket.state.value
        ticket.state = State.READY
        ticket.updated_at = datetime.now(UTC)
        s.add(ticket)
        s.flush()
        s.add(_make_event(s, ticket_id=ticket_id, state=State.READY, note=note))
        s.commit()
        s.refresh(comment)
        s.refresh(ticket)
    ws = self.workspace(ticket)
    _clear_stale_implement_guard(ws)
    _reset_implement_spawn_counter(ws)
    if self._on_transition is not None:
        self._on_transition(ticket, old_state)
    return comment, ticket


_TransitionMixin.request_implementation_changes = (
    _request_implementation_changes_expanded
)

# ---------------------------------------------------------------------------
# 10.  Enforce human approval when triage flags a ticket as filed against
#      the wrong repository.
#
#      The installed ``_resolve_next_state`` function checks whether the
#      triage note contains any ``_TRIAGE_REJECTION_PATTERNS`` substring
#      (including "wrong repo", "different repositor", etc.) and sets a
#      ``suspicious`` flag.  For deterministic-auto-approve sources this
#      blocks the shortcut, but the function still falls through to the
#      LLM classifier (``triage_auto_approve``), which has a permissive
#      bias ("when unsure, return APPROVE") and may approve the ticket
#      anyway — defeating the triage signal.
#
#      The fix: when the triage note matches a *wrong-repository* pattern
#      specifically, skip the LLM classifier entirely and return
#      HUMAN_ISSUE_APPROVAL with a structured note.  Other rejection
#      patterns (e.g. "no change needed", "does not exist") still fall
#      through to the classifier, which can distinguish false positives
#      from genuine rejections in those cases.
# ---------------------------------------------------------------------------
from robotsix_mill.core.states import State as _State  # noqa: E402

_original_resolve_next_state = _refine_helpers._resolve_next_state

# Patterns that unambiguously indicate the ticket was filed against the
# wrong repository.  These are a strict subset of
# ``_TRIAGE_REJECTION_PATTERNS`` — only the repo-misroute signals, not
# the broader "no change needed" / "does not exist" family.
_WRONG_REPO_PATTERNS: frozenset[str] = frozenset(
    {
        "different repositor",
        "different repo",
        "wrong repositor",
        "wrong repo",
        "belongs to a different",
        "lives in a different",
        "not present in this",
    }
)


def _resolve_next_state_with_wrong_repo_guard(
    ctx,
    spec,
    ticket_id,
    source=None,
    *,
    triage_note=None,
):
    """Wrap ``_resolve_next_state`` to block auto-approve on wrong-repo triage.

    When the triage note contains a wrong-repository pattern, return
    HUMAN_ISSUE_APPROVAL immediately — the LLM classifier's permissive
    bias would otherwise approve the ticket despite the triage signal.
    All other cases delegate to the original function unchanged.

    The guard respects ``require_approval``: when approval is not required
    (e.g. internal pipelines), the guard is skipped and the original
    function's fast-path (READY) is taken.
    """
    if getattr(ctx.settings, "require_approval", True) and triage_note:
        triage_lower = triage_note.lower()
        if any(p in triage_lower for p in _WRONG_REPO_PATTERNS):
            return (
                _State.HUMAN_ISSUE_APPROVAL,
                "auto-approve: NEEDS_APPROVAL — triage flagged the ticket as "
                "filed against the wrong repository; human review required "
                "to confirm scope or re-file against the correct repo",
            )
    return _original_resolve_next_state(
        ctx, spec, ticket_id, source=source, triage_note=triage_note
    )


_refine_helpers._resolve_next_state = _resolve_next_state_with_wrong_repo_guard
