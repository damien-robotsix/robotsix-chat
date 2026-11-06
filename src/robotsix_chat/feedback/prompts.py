"""Prompt builders for the feedback runner."""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

FEEDBACK_SYSTEM_PROMPT = """\
You are a session analysis agent for an LLM-powered chat assistant. \
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
            truncated = asst_msg[:3000] + "\u2026" if len(asst_msg) > 3000 else asst_msg
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


def _build_investigation_panel_prompt(
    ticket: dict[str, Any],
    *,
    session_id: str,
    trigger_type: str,
) -> str:
    """Build the operator panel for an actionable feedback finding.

    A feedback finding no longer opens a pull request on its own.  Instead
    every finding becomes ONE operator panel (``user_chat`` subsession): the
    panel agent investigates the finding **read-only** (it may explore the
    target repo and draft the change) and presents the proposed change to the
    operator, but the PR-opening tool path (``direct_repo`` / merge tools) is
    gated on an explicit operator reply — ``"approve"`` or a branch name.  On
    approval the agent opens the PR and closes the panel with the PR link; on
    rejection it closes the panel with the reason and the finding is not
    re-raised.

    This replaced the autonomous investigation subsession that opened PRs
    directly: on 2026-10-21 the operator triaged 61 such PRs on robotsix-chat
    (all closed, none mergeable on merit), plus three mis-targeted PRs on
    hexarchy — so investigations are now approval-gated panels.
    """
    return (
        "A feedback analysis of a chat session surfaced an actionable "
        "improvement. This is an OPERATOR APPROVAL PANEL — a feedback "
        "investigation must NOT open a pull request on its own. You are "
        "read-only until the operator approves in this panel.\n\n"
        f"Target repo: {ticket.get('target_repo', '') or 'robotsix-chat'}\n"
        f"Kind: {ticket.get('kind', '')}\n"
        f"Title: {ticket['title']}\n\n"
        f"Finding:\n{ticket['description']}\n\n"
        f"(Origin: robotsix-chat feedback run | session: {session_id} | "
        f"trigger: {trigger_type})\n\n"
        "Step 1 — Investigate read-only: explore the target repo and draft "
        "the smallest concrete change that addresses the finding. Do NOT open "
        "a pull request yet and do NOT call any direct_repo / merge "
        "PR-opening tool in this step.\n\n"
        "Step 2 — Present to the operator: summarise the finding, the target "
        "repo, and your proposed change (the files/lines you would touch and "
        "why). Then ask the operator to approve it.\n\n"
        "Step 3 — Act only on the operator's reply:\n"
        '- On approval (the operator replies "approve", or gives a branch '
        "name): open the pull request with the proposed change on the target "
        "repo, then close this panel with the PR link.\n"
        "- On rejection (the operator declines or asks you to drop it): open "
        "no PR, and close this panel with the operator's reason — the finding "
        "will not be re-raised.\n\n"
        "CRITICAL — never open a pull request when the finding's subject is "
        "not present in the target repo. Investigate first; if the thing you "
        "were sent to fix does not exist in this repo (the finding was "
        "mis-targeted), do NOT open a PR. Report back and close with the "
        'outcome "no change needed, wrong target".\n\n'
        "If the finding concerns the chat system's own configuration "
        "(periodic presets, prompts, or session behaviour), it belongs to "
        "the robotsix-chat repo or to a config change — do not route it to "
        "a component repo."
    )


def _build_chat_config_panel_prompt(
    ticket: dict[str, Any],
    *,
    session_id: str,
    trigger_type: str,
) -> str:
    """Build the operator panel for a finding about chat's own config.

    Periodic presets, prompts and session behaviour are configured in the
    deployed chat config (``periodic.sessions[]`` in the config volume), not
    in the repo, so a repo PR cannot change them: the operator decides whether
    to apply the proposed change to the live config, or to dismiss it.
    """
    return (
        "A feedback analysis of a chat session proposed a change to the chat "
        "system's own configuration (a periodic preset, prompt or session "
        "behaviour). That configuration lives in the deployed chat config, "
        "not in the robotsix-chat repository, so no pull request is opened "
        "for it. This is an OPERATOR APPROVAL PANEL: the decision belongs to "
        "the operator, not to you. You are read-only until the operator "
        "approves in this panel — do NOT edit the live config and do NOT "
        "restart any service before the operator has replied.\n\n"
        f"Title: {ticket['title']}\n"
        f"Kind: {ticket.get('kind', '')}\n\n"
        f"Finding:\n{ticket['description']}\n\n"
        f"(Origin: robotsix-chat feedback run | session: {session_id} | "
        f"trigger: {trigger_type})\n\n"
        "Step 1 — Investigate read-only: read the affected preset from the "
        "live config (GET /config?path=periodic) and draft the SMALLEST "
        "concrete edit that addresses the finding. Prefer rewording or "
        "condensing an existing rule over appending a new block: every "
        "applied finding grows the preset's prompt, and a prompt that only "
        "ever grows is a defect in itself. Do NOT call PUT /config, "
        "restart_lifecycle_service or self_restart in this step.\n\n"
        "Step 2 — Present to the operator: summarise the finding, quote the "
        "exact text you would change (before/after, with the prompt's length "
        "before and after), say whether an existing rule already covers it, "
        "and give the operator two options — apply, or dismiss. Then stop "
        "and wait for the reply.\n\n"
        "Step 3 — Act only on the operator's reply:\n"
        '- On approval (the operator replies "apply" or "approve"): apply the '
        "edit via PUT /config with a one-item periodic.sessions list naming "
        "only that preset (named-list merge). Periodic presets hot-reload on "
        "save, so do NOT restart the chat service — a restart kills every "
        "agent turn in flight. Then close this panel stating what changed.\n"
        "- On rejection (the operator declines or asks you to drop it): "
        "close this panel with the reason and change nothing.\n"
        "Either way, mark the panel resolved so the finding is not re-raised. "
        "Never apply the change on your own initiative, and never treat the "
        "absence of a reply as approval."
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
