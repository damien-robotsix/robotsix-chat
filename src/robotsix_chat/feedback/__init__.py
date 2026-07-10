"""Automated feedback analysis for continuous self-improvement.

At compaction and session-end boundaries a feedback run analyses the
conversation and files improvement tickets via the board's
``POST /tickets/ingest`` endpoint.
"""

from __future__ import annotations

from .runner import FEEDBACK_SYSTEM_PROMPT, FeedbackRunner

__all__ = ["FEEDBACK_SYSTEM_PROMPT", "FeedbackRunner"]
