"""Deterministic, side-effect-free duplicate-detection helpers.

Used by :func:`~robotsix_chat.board.create_board_ticket` to guard against
near-duplicate filings before POSTing to the board API.  All functions are
pure and importable in tests.

Narrowing heuristic, not semantic guarantee — the LLM retains final intent
judgement on any surfaced candidates.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "SIMILARITY_THRESHOLD",
    "normalize_title",
    "title_similarity",
    "find_duplicate_candidates",
]

#: Token-overlap cutoff for flagging a candidate as a likely duplicate.
SIMILARITY_THRESHOLD: float = 0.5

# Characters to strip from titles during normalization.
_STRIP_RE = re.compile(r"[^\w\s]")


def normalize_title(title: str) -> str:
    """Normalize *title* for comparison.

    Lowercase, strip punctuation, collapse whitespace.  Returns the
    normalized string (may be empty if *title* contains only
    punctuation / whitespace).
    """
    lowered = title.lower()
    stripped = _STRIP_RE.sub(" ", lowered)
    return " ".join(stripped.split())


def title_similarity(a: str, b: str) -> float:
    """Jaccard token overlap between two titles.

    Normalizes both titles via :func:`normalize_title`, splits into
    tokens, and returns ``|A ∩ B| / |A ∪ B|``.

    Returns ``0.0`` when either side produces no tokens (e.g. both
    titles are pure punctuation).
    """
    tokens_a = set(normalize_title(a).split())
    tokens_b = set(normalize_title(b).split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def find_duplicate_candidates(
    new_title: str,
    tickets: list[dict[str, Any]],
    *,
    threshold: float = SIMILARITY_THRESHOLD,
) -> list[dict[str, Any]]:
    """Return the subset of *tickets* whose ``title`` is a likely duplicate.

    A ticket is flagged when any of these holds:

    - normalized titles are equal (case- and punctuation-insensitive),
    - one normalized title is a substring of the other, OR
    - :func:`title_similarity` ≥ *threshold*.

    Tickets missing or with an empty ``title`` key are silently skipped.

    Results are sorted by descending similarity to *new_title* so the
    strongest match appears first.
    """
    candidates: list[tuple[float, dict[str, Any]]] = []
    norm_new = normalize_title(new_title)
    if not norm_new:
        return []

    for t in tickets:
        title = t.get("title")
        if not title or not isinstance(title, str):
            continue
        norm_title = normalize_title(title)
        if not norm_title:
            continue

        # Exact normalized match → strongest possible signal.
        if norm_title == norm_new:
            candidates.append((1.0, t))
            continue

        # Substring check: one side fully contained in the other.
        if norm_new in norm_title or norm_title in norm_new:
            candidates.append((1.0, t))
            continue

        sim = title_similarity(new_title, title)
        if sim >= threshold:
            candidates.append((sim, t))

    # Sort descending by score then stable by original order.
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [t for _score, t in candidates]
