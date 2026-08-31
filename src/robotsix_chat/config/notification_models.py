"""Notification Settings Models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from robotsix_chat.config.constants import drop_blank_numeric_sentinels


class NotificationSettings(BaseModel):
    """Browser notification settings — lets the agent alert the user proactively.

    When enabled, the agent gains a ``notify_user`` tool that publishes a
    notification event to connected clients over the existing SSE channel
    (EventBus).  The user's browser renders the event via the native
    Notifications API.

    Delivery only reaches clients that are currently connected — the
    notification is silently dropped when no browser is listening.

    Attributes:
        enabled: Master switch.  When ``False``, no notify_user tool is
            offered.

    """

    enabled: bool = True
    model_config = ConfigDict(extra="forbid")


class FeedbackSettings(BaseModel):
    """Automated feedback analysis for continuous self-improvement.

    When enabled, a feedback run analyses the conversation at compaction
    and session-end boundaries, then files improvement tickets via the
    board's ``POST /tickets/ingest`` endpoint.  Tickets flow through the
    normal human-approval workflow — the feedback run never auto-approves.

    Attributes:
        enabled: Master switch.  When ``False``, no feedback runs occur.
        model_level: llmio capability level for the feedback-analysis
            agent (a cheap, single-turn extraction call).  Default ``1``.
        board_url: Base URL of the board HTTP API (no trailing slash).
            Required when *enabled* — the runner POSTs to
            ``{board_url}/tickets/ingest``.
        board_api_token: Optional Bearer token for the board API.
        deploy_api_key: Bearer / X-API-Key token for the central-deploy
            roster endpoint (``GET /chat/components``). Required when
            the feedback runner needs to resolve allowed repos via the
            deploy roster.
        timeout: Per-request HTTP timeout in seconds for ingest calls.
            The set of allowed target repos is resolved dynamically at
            run-time from the deploy server's chat-component roster
            intersected with the mill board's repo registry — no static
            allowlist is needed.
        max_tickets_per_run: Ceiling on tickets filed by one feedback run.
            A run fires at every compaction and session-end boundary, and
            was previously unbounded: across 37 observed runs it filed 114
            tickets, mean 3.08, peaking at 9 from a single run. Excess
            tickets are dropped with a warning naming each one. ``0``
            disables filing while leaving analysis on.
        dedup_window_seconds: Time window (in seconds) for suppressing
            duplicate feedback runs and duplicate ticket titles.  When
            two feedback runs for the same session are scheduled within
            this window, the second is skipped.  Similarly, when two
            tickets with the same normalized title (lowercased, stripped)
            are filed within this window, the second is skipped.  Default
            ``60.0`` balances promptness against the risk of near-
            simultaneous duplicate creation during concurrent monitor
            runs.
        ingest_max_retries: Number of automatic retries when a
            ``POST /tickets/ingest`` call fails with a transport-level
            error (e.g. ``ReadTimeout``).  Between retries the runner
            queries the board idempotently to confirm whether the
            timed-out request already created the ticket, so a retry
            never files a duplicate.  ``0`` disables retrying (a single
            attempt).  Default ``2`` (up to three attempts total).

    """

    enabled: bool = False
    model_level: int = 1
    board_url: str = ""
    board_api_token: SecretStr = SecretStr("")
    deploy_api_key: SecretStr = SecretStr("")
    timeout: float = 60.0
    max_tickets_per_run: int = 3
    dedup_window_seconds: float = 60.0
    ingest_max_retries: int = 2
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _strip_blank_numeric(cls, data: Any) -> Any:
        """Drop legacy ``""`` sentinels for numeric fields so old configs load."""
        return drop_blank_numeric_sentinels(cls, data)
