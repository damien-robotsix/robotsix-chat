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
# ``__path__`` and then reload ``diagnostic_events`` from the shadow.
import robotsix_mill.agents.runners  # noqa: E402

if _LOCAL_AGENTS_RUNNERS not in robotsix_mill.agents.runners.__path__:
    robotsix_mill.agents.runners.__path__.insert(0, _LOCAL_AGENTS_RUNNERS)

if "robotsix_mill.agents.runners.diagnostic_events" in sys.modules:
    del sys.modules["robotsix_mill.agents.runners.diagnostic_events"]
import robotsix_mill.agents.runners.diagnostic_events  # noqa: E402

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
