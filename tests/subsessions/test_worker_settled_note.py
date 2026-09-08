"""A clicked ```suggestions option settles a user_chat decision (2026-09-08)."""

from __future__ import annotations

from robotsix_chat.subsessions.models import SubsessionKind
from robotsix_chat.subsessions.registry import SubsessionRegistry
from robotsix_chat.subsessions.worker import _operator_picked_suggestion


def _panel(registry: SubsessionRegistry):
    return registry.create(
        kind=SubsessionKind.USER_CHAT,
        owner_session_id="sess-1",
        parent_id=None,
        depth=1,
        title="Decision",
        prompt="Approve or reject?",
        model_level=1,
    )


def test_operator_picked_suggestion_matches_offered_option() -> None:
    registry = SubsessionRegistry(store_path=None)
    info = _panel(registry)
    registry.append_transcript(
        info.id,
        "assistant",
        "Recommend Option A.\n```suggestions\nOption A — approve now\n"
        "Option B — send back\n```\n",
    )
    registry.enqueue_message(info.id, "user", "option a — approve now")
    pending = registry.drain_inbox(info.id)
    assert _operator_picked_suggestion(registry.get(info.id), pending)


def test_operator_free_text_is_not_a_settled_click() -> None:
    registry = SubsessionRegistry(store_path=None)
    info = _panel(registry)
    registry.append_transcript(
        info.id, "assistant", "```suggestions\nOption A — approve now\n```"
    )
    registry.enqueue_message(
        info.id, "user", "hmm, what does option A imply for costs?"
    )
    pending = registry.drain_inbox(info.id)
    assert not _operator_picked_suggestion(registry.get(info.id), pending)
