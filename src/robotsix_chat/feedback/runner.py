"""Feedback runner — analyses a session and files improvement tickets."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx

from robotsix_chat.common.http import safe_http_request

try:
    from robotsix_llmio.core.tracing import (
        GEN_AI_TOOL_NAME,
        OP_EXECUTE_TOOL,
        get_recording_span,
        get_tracer,
        start_span,
        start_trace,
    )
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    start_trace = None  # type: ignore[assignment]
    get_recording_span = None  # type: ignore[assignment]
    start_span = None  # type: ignore[assignment]
    get_tracer = None  # type: ignore[assignment]
    GEN_AI_TOOL_NAME = None  # type: ignore[assignment]
    OP_EXECUTE_TOOL = None  # type: ignore[assignment]

try:
    from opentelemetry.trace import Status, StatusCode
except ImportError:  # pragma: no cover — tracing extra absent in minimal installs
    Status = None  # type: ignore[assignment, misc]
    StatusCode = None  # type: ignore[assignment, misc]

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.config.models import FeedbackSettings
    from robotsix_chat.knowledge.store import KnowledgeStore
    from robotsix_chat.llm import LlmioChatAgent
    from robotsix_chat.subsessions import SubsessionRegistry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowed-repo resolution (dynamic — no static config)
# ---------------------------------------------------------------------------

# In-memory cache keyed by "repos": (fetched_at_monotonic, list_of_repo_ids).
_repo_cache: dict[str, tuple[float, list[str]]] = {}
_REPO_CACHE_TTL: float = 60.0  # seconds — short enough to pick up access changes

#: Base delay (seconds) for exponential backoff between idempotent
#: ``/tickets/ingest`` retries.  Attempt *n* waits ``base * 2**n``.
_INGEST_RETRY_BACKOFF_BASE: float = 1.0


#: Used when no ``central_deploy.url`` is configured. The ``central-deploy``
#: hostname only resolves on the deploy stack's *internal* compose network;
#: a component attached solely to ``central-deploy-proxy`` (which is how chat
#: runs) cannot resolve it and gets "Name or service not known". Keeping it as
#: the fallback preserves behaviour for deployments where it does resolve,
#: but any real deployment should set ``central_deploy.url``.
_DEFAULT_DEPLOY_BASE_URL = "http://central-deploy:8100"


async def _resolve_allowed_repos(
    deploy_api_key: str, deploy_base_url: str = ""
) -> list[str]:
    """Resolve the set of allowed feedback target repos dynamically.

    Queries the deploy server's chat-component roster and the mill board's
    repo registry, then intersects the two on component/repo id.  The result
    is cached briefly (``_REPO_CACHE_TTL``) to avoid hammering deploy on
    every feedback run.

    *deploy_base_url* should be ``central_deploy.url`` — the address this
    deployment already knows reaches the deploy server. Empty falls back to
    :data:`_DEFAULT_DEPLOY_BASE_URL`.

    Falls back to ``["robotsix-chat"]`` when deploy is unreachable and logs
    a warning.
    """
    now = time.monotonic()
    entry = _repo_cache.get("repos")
    if entry is not None and (now - entry[0]) < _REPO_CACHE_TTL:
        return entry[1]

    result = await _do_resolve_allowed_repos(deploy_api_key, deploy_base_url)
    _repo_cache["repos"] = (now, result)
    return result


async def _do_resolve_allowed_repos(
    deploy_api_key: str, deploy_base_url: str = ""
) -> list[str]:
    """Resolve allowed repos by querying deploy and mill (no caching)."""
    # 1. Fetch components from deploy.
    base = (deploy_base_url or _DEFAULT_DEPLOY_BASE_URL).rstrip("/")
    deploy_url = f"{base}/chat/components"
    deploy_headers: dict[str, str] = {}
    if deploy_api_key:
        deploy_headers["X-API-Key"] = deploy_api_key

    deploy_result = await safe_http_request(
        "GET", deploy_url, headers=deploy_headers, label="Deploy roster"
    )
    if deploy_result.error:
        logger.warning(
            "Deploy roster unreachable (%s) — falling back to [robotsix-chat] only",
            deploy_result.error,
        )
        return ["robotsix-chat"]

    try:
        deploy_entries: list[dict[str, Any]] = json.loads(deploy_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Deploy roster response is not valid JSON — falling back")
        return ["robotsix-chat"]

    deploy_ids: set[str] = {
        e["id"] for e in deploy_entries if isinstance(e, dict) and "id" in e
    }
    if not deploy_ids:
        logger.warning("Deploy roster is empty — falling back to [robotsix-chat] only")
        return ["robotsix-chat"]

    # 2. Fetch repos from mill board.
    mill_url = "http://mill:8077/repos"
    mill_result = await safe_http_request("GET", mill_url, label="Mill repos")
    if mill_result.error:
        logger.warning(
            "Mill repos unreachable (%s) — falling back to [robotsix-chat] only",
            mill_result.error,
        )
        return ["robotsix-chat"]

    try:
        mill_repos: list[dict[str, Any]] = json.loads(mill_result.text or "[]")
    except json.JSONDecodeError:
        logger.warning("Mill repos response is not valid JSON — falling back")
        return ["robotsix-chat"]

    # Mill's GET /repos returns entries keyed ``repo_id`` (with ``board_id``
    # and ``forge_remote_url``) — NOT ``id``. Reading only ``id`` made this
    # set permanently empty, so the deploy∩mill intersection always fell back
    # to [robotsix-chat] and every cross-repo feedback ticket was silently
    # dropped (observed 2026-09-01: file-hub tickets skipped on every run).
    # Tolerate both keys in case the mill API ever renames it back.
    mill_ids: set[str] = {
        r.get("repo_id") or r["id"]
        for r in mill_repos
        if isinstance(r, dict) and ("repo_id" in r or "id" in r)
    }

    # 3. Intersect — only repos that are both in the deploy roster AND
    #    registered on the mill board are valid targets.
    allowed = sorted(deploy_ids & mill_ids)
    if not allowed:
        logger.warning(
            "No repos in deploy/mill intersection (deploy=%s, mill=%s) — "
            "falling back to [robotsix-chat] only",
            sorted(deploy_ids),
            sorted(mill_ids),
        )
        allowed = ["robotsix-chat"]

    return allowed


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

FEEDBACK_SYSTEM_PROMPT = """\nYou are a session analysis agent for an LLM-powered chat assistant. \
Your job is to review a conversation session and identify concrete, \
actionable improvements. Output ONLY a JSON object — no markdown \
fences, no preamble, no commentary outside the JSON.

The JSON must have exactly this structure:
{
  "analysis": "Brief prose analysis of the session (2-4 sentences).",
  "tickets": [
    {
      "title": "Short, specific title",
      "description": "Detailed description with context — include what happened, \
why it matters, and a concrete suggestion.",
      "kind": "prompt",
      "target_repo": "robotsix-chat"
    }
  ]
}

``kind`` must be one of: ``prompt``, ``tool``, ``config``, ``code``.
``target_repo`` must be one of the valid target repos listed in the prompt.

Rules:
- Only include a ticket when there is a **concrete, actionable** \
improvement — something a developer could implement.
- Do NOT file tickets for one-off flukes, transient API errors, or \
user typos. Focus on patterns: repeated failures, missing capabilities, \
unclear guidance, slow paths, config gaps.
- For uneventful sessions where nothing went wrong and no capability \
gaps were exposed, return an empty ``tickets`` list.
- The ``description`` must be self-contained and actionable — someone \
reading it later should understand the problem and have a clear idea \
of what to change.
- Choose ``target_repo`` based on which codebase the improvement \
concerns — if the issue is about the chat system itself, use the chat \
repo; if it is about a downstream component, use that component's repo.
- **CI failure visibility:** If the session contains evidence of a CI \
failure after a merge (e.g. CI status mentioned in subsession summaries, \
metadata, or internal notes) but the assistant never proactively informed \
the user about the regression in the main conversation, file a ``prompt`` \
ticket.  The operator's goal is to keep main green — CI regressions that \
are silently buried in internal metadata defeat that goal.  The ticket \
should describe the specific failure, note that the assistant only \
reported it internally, and recommend that the periodic prompt \
instruct the assistant to surface CI failures proactively in the \
main conversation."""


def _build_feedback_prompt(
    trigger_type: str,
    session_id: str,
    turns: list[tuple[str, str]],
    subsession_summaries: list[dict[str, Any]],
    repo_ids: list[str],
) -> str:
    """Build the feedback analysis prompt from session data."""
    transcript_parts: list[str] = []
    for user_msg, asst_msg in turns:
        transcript_parts.append(f"User: {user_msg}")
        if asst_msg:
            truncated = asst_msg[:3000] + "…" if len(asst_msg) > 3000 else asst_msg
            transcript_parts.append(f"Assistant: {truncated}")
    transcript = "\n".join(transcript_parts) if transcript_parts else "(empty)"

    subsession_text = ""
    if subsession_summaries:
        parts: list[str] = []
        for i, s in enumerate(subsession_summaries):
            kind = s.get("kind", "unknown")
            summary = s.get("summary", "") or "(no summary)"
            status = s.get("status", "unknown")
            parts.append(f"  [{i}] kind={kind} status={status}\n      {summary}")
        subsession_text = (
            "=== INTERNAL METADATA — NOT part of the conversation, NEVER shown "
            "to the user ===\n"
            "Subsession summaries:\n"
            + "\n".join(parts)
            + "\n=== END INTERNAL METADATA ==="
        )
    else:
        subsession_text = (
            "=== INTERNAL METADATA — NOT part of the conversation ===\n"
            "Subsession summaries: (none)\n"
            "=== END INTERNAL METADATA ==="
        )

    valid_repos = ", ".join(repo_ids)

    return (
        f"Trigger: {trigger_type}\n"
        f"Session ID: {session_id}\n\n"
        f"Conversation transcript:\n{transcript}\n"
        f"=== TRANSCRIPT END ===\n\n"
        f"{subsession_text}\n\n"
        f"Valid target repos: {valid_repos}\n\n"
        "METADATA RULE: The `=== INTERNAL METADATA` block above was assembled "
        "by the feedback system, NOT by the assistant.  It was never printed "
        "to the user.  Do NOT file tickets claiming the assistant emitted raw "
        "subsession identifiers, `kind=… status=…` lines, or metadata headers "
        "unless those patterns appear inside the `Conversation transcript` "
        "section itself.\n\n"
        "Output the JSON analysis now."
    )


# ---------------------------------------------------------------------------
# Board admission-policy handling
# ---------------------------------------------------------------------------

#: Markers in a ``POST /tickets/ingest`` HTTP 400 body that identify the
#: board's *admission policy* rejection (mill #3093 permanently removed the
#: investigation-ticket capability: the board now admits deployment tickets
#: only and rejects ``source_tag=robotsix-chat-feedback`` with guidance to
#: run the investigation as a chat subsession agent instead).  Matched
#: case-insensitively against the ``detail`` field (or raw body) so a
#: policy rejection is recognised as an outcome, not a transient failure.
_ADMISSION_POLICY_MARKERS: tuple[str, ...] = (
    "is not admitted on this board",
    "ingest_blocked_source_tags",
    "chat subsession agent",
)


def _is_admission_policy_block(resp: httpx.Response) -> bool:
    """Return True when *resp* is the board's admission-policy 400 rejection.

    Only an HTTP 400 whose body advertises one of
    :data:`_ADMISSION_POLICY_MARKERS` qualifies — a generic 400 (malformed
    payload, validation error) is still treated as a real failure.
    """
    if resp.status_code != 400:
        return False
    detail = ""
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        body = None
    if isinstance(body, dict):
        raw_detail = body.get("detail")
        if isinstance(raw_detail, str):
            detail = raw_detail
    haystack = (detail or resp.text or "").lower()
    return any(marker in haystack for marker in _ADMISSION_POLICY_MARKERS)


#: Repo id that owns the chat system's own configuration.  A finding about
#: chat's periodic presets, prompts, or session behaviour belongs to this
#: repo (or to a config change) — it must never be routed to a component
#: repo.  Routing such a finding to a component produced mis-targeted PRs on
#: ``hexarchy`` (#304/#311/#312) for a finding about chat's ``board-gates-drain``
#: periodic preset, which lives only in chat's config volume under
#: ``periodic.sessions`` and is not in any repo tree.
_CHAT_SELF_REPO: str = "robotsix-chat"

#: Lower-cased subject markers that identify a finding as concerning the chat
#: system's own periodic presets / prompts / session behaviour.  Such findings
#: are owned by the chat repo regardless of what the analysis LLM guessed for
#: ``target_repo``.  Kept deliberately narrow — a broad match (e.g. any
#: ``config`` kind) would mis-route genuine component findings.
_CHAT_CONFIG_SUBJECT_MARKERS: tuple[str, ...] = (
    "periodic",
    "preset",
    "session behaviour",
    "session behavior",
    "system prompt",
    "drain-the-mill",
)


def _subject_is_chat_config(ticket: dict[str, Any]) -> bool:
    """Return ``True`` when *ticket* concerns chat's own periodic/session config.

    Matches the finding's title and description against
    :data:`_CHAT_CONFIG_SUBJECT_MARKERS`.  These findings belong to the chat
    repo (or a config change) — they must never be routed to a component repo.
    """
    subject = (
        (f"{ticket.get('title', '')}\n{ticket.get('description', '')}").strip().lower()
    )
    return any(marker in subject for marker in _CHAT_CONFIG_SUBJECT_MARKERS)


def _build_investigation_prompt(
    ticket: dict[str, Any],
    *,
    session_id: str,
    trigger_type: str,
) -> str:
    """Build a self-contained instruction for the investigation subsession.

    The board no longer accepts feedback findings as tickets; the mill 400
    prescribes running the investigation as a chat subsession agent.  This
    prompt hands the agent the full finding so it can investigate and act
    on it directly in the target repo.
    """
    return (
        "A feedback analysis of a chat session surfaced an actionable "
        "improvement. The board no longer accepts these as ingest tickets "
        "(it admits deployment tickets only), so you must investigate and "
        "act on this finding directly as a chat subsession agent.\n\n"
        f"Target repo: {ticket.get('target_repo', '') or 'robotsix-chat'}\n"
        f"Kind: {ticket.get('kind', '')}\n"
        f"Title: {ticket['title']}\n\n"
        f"Finding:\n{ticket['description']}\n\n"
        f"(Origin: robotsix-chat feedback run | session: {session_id} | "
        f"trigger: {trigger_type})\n\n"
        "Investigate the finding in the target repo. When your investigation "
        "determines that a concrete action is needed (e.g., a code change via "
        "pull request, a deployment ticket, or a configuration update), you "
        "MUST follow this approval workflow BEFORE taking any action:\n\n"
        "1. PREPARATION: Fully investigate the finding, determine the precise "
        "changes needed, and prepare a detailed proposal. Include:\n"
        "   - What the problem is and why it needs fixing\n"
        "   - The exact changes you propose to make\n"
        "   - Which files will be modified and how\n"
        "   - Why this is the smallest/simplest solution\n\n"
        "2. REQUEST APPROVAL: Spawn a user_chat subsession to present your "
        "proposed solution to the operator with the full context from step 1. "
        "Use spawn_subsession(kind='user_chat', title='[feedback] Approve: "
        "<finding title>', instructions='Present the proposed solution and "
        "ask: Should I proceed with this change? [Yes/No]'). Wait for the "
        "operator's explicit answer.\n\n"
        "3. EXECUTE: Only after receiving explicit operator approval (a clear "
        "'Yes' or affirmative answer), proceed with the concrete action — open "
        "the pull request, file the deployment ticket, or make the configuration "
        "change. Never execute any state-mutating action without operator approval.\n\n"
        "4. REPORT: When the action is complete, report a short summary of what "
        "you did and why. Close the subsession with complete_subsession.\n\n"
        "If the operator declines (answers 'No'), document their decision and "
        "close the subsession explaining why the change was not made.\n\n"
        "CRITICAL — never open a pull request when the finding's subject is "
        "not present in the target repo. Investigate first; if the thing you "
        "were sent to fix does not exist in this repo (the finding was "
        "mis-targeted), skip the approval workflow and close with the outcome "
        '"no change needed, wrong target".\n\n'
        "If the finding concerns the chat system's own configuration "
        "(periodic presets, prompts, or session behaviour), it belongs to "
        "the robotsix-chat repo or to a config change — do not route it to "
        "a component repo."
    )


def _build_escalation_prompt(
    ticket: dict[str, Any],
    *,
    session_id: str,
    trigger_type: str,
) -> str:
    """Build a user-facing panel prompt that escalates an unresolvable finding.

    Used when a policy-blocked finding's target repo cannot be resolved with
    confidence — the finding is surfaced to the operator as a ``user_chat``
    panel so a human decides where it belongs, instead of guessing and
    risking a mis-targeted PR.
    """
    return (
        "A feedback analysis of a chat session surfaced an actionable "
        "improvement whose target repo could not be resolved with confidence. "
        "The board no longer accepts these as ingest tickets (it admits "
        "deployment tickets only).\n\n"
        f"Title: {ticket['title']}\n"
        f"Kind: {ticket.get('kind', '')}\n\n"
        f"Finding:\n{ticket['description']}\n\n"
        f"(Origin: robotsix-chat feedback run | session: {session_id} | "
        f"trigger: {trigger_type})\n\n"
        "The finding has no confidently-resolvable target repo, so rather "
        "than guessing (which risks a mis-targeted PR in the wrong "
        "repository), it is escalated to you. Decide where this finding "
        "belongs — the robotsix-chat repo, a config change, or a specific "
        "component repo — and route it there. If it is not actionable, mark "
        "it as resolved so it is not re-raised."
    )
