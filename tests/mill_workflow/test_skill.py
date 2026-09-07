"""The mill-workflow skill carries the board rules moved out of the prompt (v162)."""

from __future__ import annotations

from robotsix_chat.config import Settings
from robotsix_chat.mill_workflow import load_mill_workflow_skill


def test_skill_loads_with_title_and_sections() -> None:
    body = load_mill_workflow_skill()
    assert body.startswith("# Mill ticket workflow")
    for heading in (
        "## Live board state first",
        "## Mill API quick reference and filing hygiene",
        "## Ticket lifecycle (default for every ticket you create)",
        "## Approval gate (you are the approver)",
        "## Ticket-id fidelity",
        "## Blocked, deadlocked and superseded tickets",
        "## Conflicts between a new instruction and a pending ticket",
    ):
        assert heading in body, heading


def test_ticket_lifecycle_steps_are_contiguous() -> None:
    """v161 interleaved the lifecycle steps with other bullets.

    The skill keeps steps 1..6 as ordered headings.
    """
    body = load_mill_workflow_skill()
    names = ["Initiate", "Monitor", "Remediate", "Complete", "Exit", "Reload"]
    positions = [body.index(f"### {n}. {name}") for n, name in enumerate(names, 1)]
    assert positions == sorted(positions)
    assert "file the ticket via POST /tickets/ingest" in body


def test_rules_left_the_system_prompt() -> None:
    """The moved rules must not be paid for on every turn any more."""
    default = Settings.model_fields["agent_instruction"].default
    for moved in (
        "Mill approval gate",
        "Ticket lifecycle (default for every ticket you create)",
        "Bulk-resume failure-mode classification",
        "POST /tickets/ingest — file a new ticket",
        "Deploy preflight",
        "direct_fix (LAST RESORT ONLY)",
    ):
        assert moved not in default, moved
    assert "read_skill" in default
    assert "'mill_workflow'" in default
