"""Deterministic auto-approve extension for chat-agent-filed tickets.

The installed refine stage's ``_AUTO_APPROVE_SOURCES`` set shortlists the
ticket ``source`` values that skip the human approval gate after
refinement.  Chat-agent-filed improvement tickets (``source="robotsix-chat"``
— the ``source_tag`` the robotsix-chat assistant stamps on every
``POST /tickets/ingest`` call) were missing from that set, so every ticket
the assistant filed for itself landed in ``human_issue_approval`` and
stalled until a human operator nudged it forward.

The shadow package's ``__init__.py`` merges the constant below into the
installed set at import time, so chat-agent-filed tickets flow
``draft -> refine -> ready`` without a human click — matching the existing
deterministic treatment of the mill's own periodic-agent sources
(``audit``, ``agent_check``, ``bc_check``, …).

Refinement is deliberately preserved: the ticket still passes through the
refine stage (dedup + spec building), and only the approval gate is
skipped.
"""

from __future__ import annotations

#: Sources that should deterministically skip the human approval gate
#: after refinement.  ``robotsix-chat`` is the ``source_tag`` the
#: robotsix-chat assistant uses when filing tickets via the ingest
#: endpoint (``POST /tickets/ingest``).
EXTRA_AUTO_APPROVE_SOURCES: frozenset[str] = frozenset({"robotsix-chat"})
