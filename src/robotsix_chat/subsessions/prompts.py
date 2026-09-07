"""Fixed prompt fragments the subsession worker prepends to turns.

Kept in a leaf module (no intra-package imports) so other packages — notably
the memory recall query scrubber in :mod:`robotsix_chat.periodic.prompts` —
can import the exact text without pulling in the worker.
"""

from __future__ import annotations

# System note prepended (followed by a blank line) to the first turn of every
# user_chat subsession so the agent always restates option definitions inline
# instead of surfacing bare labels ("Option B") the operator cannot
# disambiguate, and emits the ```suggestions block for discrete choices.
USER_CHAT_FIRST_TURN_NOTE = (
    "[System note: this is a side-chat with the operator. "
    "Your instructions may define a menu of options (Option A, Option B, …). "
    "The operator sees ONLY what you write in this panel — they do NOT see "
    "your instructions.  Every time you reference an option label you MUST "
    "restate its full definition inline so the operator can understand it "
    "without switching context.  For example, instead of writing "
    '"Option B is the right call," write '
    '"Option B (phased: cleanup now, warning-first gate, fail-closed only '
    'after auto-mail migrates) is the right call."  This applies to every '
    "turn — the initial recommendation and any follow-up confirmation-gate "
    "turns.  If you present multiple options, show ALL of them with their "
    "definitions so the operator can compare.  Whenever a turn asks the "
    "operator to pick between discrete options (Option A/B/C, approve/reject, "
    "yes/no, pick-one-of-N), ALSO end that message with a fenced block:\n"
    "```suggestions\n"
    "<one option per line>\n"
    "```\n"
    "one self-contained option per line (2-5 options, each <= ~80 chars, "
    "actionable as a verbatim reply) so the operator can answer with a single "
    "click; keep the surrounding prose so a typed free-text answer is equally "
    "valid.]"
)
