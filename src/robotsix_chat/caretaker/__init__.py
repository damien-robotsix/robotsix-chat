"""Model-free surfacing of central-deploy caretaker findings to the operator.

See :func:`watch_caretaker_findings` for the deterministic poll/notify loop
and :mod:`robotsix_chat.caretaker.models` for the tolerant payload parser.
"""

from __future__ import annotations

from robotsix_chat.caretaker.client import fetch_caretaker_status
from robotsix_chat.caretaker.models import CaretakerFinding, parse_findings
from robotsix_chat.caretaker.watcher import watch_caretaker_findings

__all__ = [
    "CaretakerFinding",
    "fetch_caretaker_status",
    "parse_findings",
    "watch_caretaker_findings",
]
