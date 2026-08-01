"""On-demand deep memory search tool for the agent.

The automatic per-message recall is retrieval-only (``recall_search_type``,
default ``CHUNKS``) so every turn stays cheap.  This module exposes the
expensive, LLM-mediated graph search (``GRAPH_COMPLETION``) as a tool the
model calls deliberately when the cheap snippets are not enough — turning a
per-turn tax into a considered choice.

Exposes :func:`build_memory_tools` — returns ``[]`` for memory backends
without a deep-recall capability (e.g. ``NullMemory``), so wiring it up is
unconditional at the call site.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["build_memory_tools"]


def build_memory_tools(memory: Any) -> list[Callable[..., Any]]:
    """Return the deep memory-search tool, or ``[]`` when unavailable.

    *memory* is the agent's :class:`~robotsix_chat.memory.base.ChatMemory`;
    only backends exposing ``recall_deep`` (currently ``CogneeMemory``) get
    the tool.  ``NullMemory`` and test doubles yield ``[]`` so the agent's
    tool surface is unchanged when memory is off.
    """
    recall_deep = getattr(memory, "recall_deep", None)
    if recall_deep is None:
        return []

    async def search_memory(query: str) -> str:
        """Search long-term memory in depth for facts, decisions, and context.

        A cheap automatic recall already runs on every message — short,
        relevant snippets are prepended to the user's turn without any action
        on your part.  Call this tool only when that is not enough:

        * the user refers to a past conversation, decision, preference, or
          fact you don't see in the current context or the recalled snippets;
        * you need synthesised background on a topic ("what do we know
          about X?") rather than verbatim fragments;
        * the recalled snippets look relevant but truncated or ambiguous and
          getting this right matters for the reply.

        This runs an LLM-mediated search over the whole knowledge graph — it
        is slower (can take tens of seconds) and costs an extra model call,
        so don't call it reflexively on every turn, and don't call it when
        the automatic snippets or the visible conversation already answer
        the question.

        Args:
            query: What to look for, phrased as a focused question or topic
                ("user's preference for X", "decision about Y in July").
                Narrow beats broad — one specific question per call.

        Returns:
            Synthesised memory relevant to the query, or an explanatory
            message when nothing was found or the search failed.

        """
        return str(await recall_deep(query))

    return [search_memory]
