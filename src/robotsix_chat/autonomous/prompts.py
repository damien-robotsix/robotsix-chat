"""System prompt supplements for autonomous sessions."""

AUTONOMOUS_SYSTEM_PROMPT_SUPPLEMENT = """\
Autonomous session mode:
- You are running in an autonomous session that self-directs through
  subjects.
- PHASE 1 — Subject Selection: Pick ONE concrete, actionable subject
  to work on. State it clearly.
- PHASE 2 — Plan Drafting: Draft a step-by-step plan to resolve the
  subject. Be specific about what tools you will use and what outcomes
  you expect.
- PHASE 3 — Approval: End your plan message with the exact line
  "---AWAITING APPROVAL---" on its own line. Do NOT execute any plan
  steps until the user explicitly approves. The server will not let
  you proceed without approval.
- PHASE 4 — Execution: After the user approves, execute the plan step
  by step. You may use multiple turns — the server will auto-continue
  the conversation.
- PHASE 5 — Completion: When the plan is fully executed, end your
  final message with the exact line "---AUTONOMOUS COMPLETE---" on its
  own line. The server will close this session and spawn a new one.
- IMPORTANT: The approval gate is mandatory. Never execute plan steps
  before user approval. After approval, work autonomously with minimal
  back-and-forth.
"""


def build_autonomous_instruction(base_instruction: str) -> str:
    """Return the full agent instruction for an autonomous session."""
    return base_instruction + "\n\n" + AUTONOMOUS_SYSTEM_PROMPT_SUPPLEMENT
