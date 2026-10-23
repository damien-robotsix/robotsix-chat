"""Shared runtime types for subsession spawning and workers.

:class:`SubsessionContext`, :class:`CloseState` and :class:`SubsessionEnv`
live here (rather than in :mod:`.worker`) so the worker's helper modules can
import them without an import cycle.  :mod:`.worker` re-exports them.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .delivery import ParentDelivery
from .registry import SubsessionRegistry
from .slot_budget import SlotBudget

if TYPE_CHECKING:
    from robotsix_chat.chat.conversation import ConversationStore
    from robotsix_chat.chat.events import EventSink
    from robotsix_chat.chat.server.routes import ChatAgent
    from robotsix_chat.config import Settings


@dataclass(frozen=True)
class SubsessionContext:
    """Identity captured lexically in agent tool closures.

    ``subsession_id is None`` means the agent IS the main chat agent
    (depth 0); its spawned children then have ``parent_id=None`` and
    their summaries deliver straight to the owning chat session.
    """

    owner_session_id: str
    subsession_id: str | None
    depth: int


@dataclass
class CloseState:
    """Mutable close-request holder shared between a worker and its agent.

    The ``complete_subsession`` tool sets :attr:`requested` (and the
    summary); the worker checks it after every turn.

    When the tool itself already delivered the summary (e.g. to survive
    a race with an external close), :attr:`delivery_done` is set to
    ``True`` so the worker does not deliver a second time.
    """

    requested: bool = False
    summary: str | None = None
    delivery_done: bool = False


@dataclass
class SubsessionEnv:
    """Shared runtime dependencies for spawning and running subsessions.

    ``agent_factory(settings, model_level, ctx, close_state)`` must build
    a fully-tooled agent for the subsession: the standard tool suite plus
    the depth-aware subsession tools (spawn/message/close/list when depth
    allows, ``complete_subsession`` always).
    """

    settings: Settings
    registry: SubsessionRegistry
    delivery: ParentDelivery
    conversation_store: ConversationStore
    agent_factory: Callable[[Settings, int, SubsessionContext, CloseState], ChatAgent]
    event_sink: EventSink | None = None
    # Strong refs to worker tasks spawned via spawn_subsession (belt and
    # braces alongside the registry's _running map).
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    # Per-conversation slot-budget manager (optional).  When set (and
    # enabled), spawn_subsession consults it before admitting a new
    # monitor; when None, all spawns proceed as before.  Wired by
    # :func:`attach_slot_budget`.
    slot_budget: SlotBudget | None = None
