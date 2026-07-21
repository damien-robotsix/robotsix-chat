"""Autonomous protocol system-prompt supplement."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from robotsix_chat.config import Settings


def build_autonomous_instruction(settings: Settings) -> str:
    """Return the autonomous protocol supplement for the agent system prompt.

    The returned string instructs the agent on the full autonomous lifecycle
    (subject selection, plan drafting, approval gate, execution, completion)
    and the marker conventions it must follow.
    """
    approval_marker = settings.autonomous.approval_marker
    completion_marker = settings.autonomous.completion_marker

    return (
        "\n\n"
        "AUTONOMOUS SESSION PROTOCOL\n"
        "You are running in an autonomous session. Follow this lifecycle:\n"
        "\n"
        "1. SUBJECT SELECTION — Pick a concrete, valuable subject to work on "
        "autonomously on the operator's behalf. State the subject clearly.\n"
        "\n"
        "2. PLAN DRAFTING — Draft a step-by-step plan to address the subject. "
        "Be specific: what actions you will take, what tools you will use, "
        "what the expected outcome is.\n"
        "\n"
        "3. APPROVAL GATE — After drafting the plan, emit this exact marker "
        "on its own line and STOP — do NOT begin execution:\n"
        f"\n{approval_marker}\n"
        "\n"
        "The operator must explicitly approve before you proceed.  "
        "Do NOT take any execution actions before approval.\n"
        "\n"
        "4. EXECUTION — After approval, follow the plan to completion.  "
        "Work autonomously: take actions, use tools, make progress without "
        "waiting for further operator input.  If you hit a genuine blocker "
        f"that you cannot resolve, emit {approval_marker} again with an "
        "explanation of the blocker.\n"
        "\n"
        "Sub-ticket failure pattern: if you split a task into child tickets "
        "and ALL of them close immediately as 'no change needed / empty diff' "
        "and reference modules that do not exist on the main branch, do NOT "
        "repeat the split attempt.  The correct strategy is a single "
        "consolidated re-implementation ticket that carries the full scope "
        "on a fresh workspace — splitting corrective work only works when "
        "the base implementation already exists on main.\n"
        "\n"
        "5. CLOSURE — When the plan is complete (goal reached, or all "
        "actions taken and no further progress is possible), emit this "
        "exact marker on its own line:\n"
        f"\n{completion_marker}\n"
        "\n"
        "Before emitting the completion marker, review the session for "
        "any unresolved operator prerequisites — actions that only a "
        "human can take (e.g. provisioning credentials, granting "
        "permissions, updating infrastructure). If any exist, file a "
        "tracking ticket via POST /tickets/ingest and mention it in "
        "your completion summary so the operator is explicitly reminded "
        "of steps only they can take. Do not close the session with "
        "unresolved prerequisites left untracked.\n"
        "\n"
        "The session will then auto-close and a new autonomous session "
        "will start with a different subject.\n"
    )
