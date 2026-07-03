"""Shared base for brokered agent-comm clients.

Provides the lazy import of :class:`~robotsix_agent_comm.sdk.BrokeredRequester`,
the common ``__init__`` pattern, and a ``consult()`` method that offloads the
blocking broker call to a thread so it never stalls the async server.

robotsix-agent-comm is imported lazily (the optional ``broker`` extra); failures
degrade to a message the agent can relay, never an exception into the chat path.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

__all__ = ["BaseBrokeredClient", "BrokerUnavailableError"]

# Short timeout for the pre-flight reachability probe (``GET /agents``). It
# only does a cheap registry lookup, so a few seconds is plenty — the point is
# to fail fast when the broker/recipient is down instead of waiting out the
# full (long) request timeout.
_PREFLIGHT_TIMEOUT_SECONDS = 5.0


class BrokerUnavailableError(RuntimeError):
    """Raised when the broker cannot reach the target agent.

    This is a transient infrastructure error — the target agent (e.g. the
    board manager) is temporarily unreachable through no fault of the caller.
    """


_BROKER_UNAVAILABLE_FRAGMENTS: tuple[str, ...] = (
    "unknown recipient",
    "connection refused",
    "connection timeout",
    "timed out",
)


def _is_broker_unavailable(exc: BaseException) -> bool:
    """Return ``True`` when *exc* indicates a broker/recipient reachability failure.

    Checks both known SDK exception types and message-fragment heuristics so
    that :class:`AgentNotFoundError`, :class:`DeliveryError`, and
    :class:`TransportTimeoutError` (from ``robotsix_agent_comm``) are treated
    as transient infrastructure errors.
    """
    # Check for known SDK exception types first (lazy import to avoid
    # requiring the broker extra at module level).
    try:
        from robotsix_agent_comm.sdk.agent import (  # noqa: I001
            AgentNotFoundError,
            DeliveryError,
        )
        from robotsix_agent_comm.transport.errors import TransportTimeoutError
    except ImportError:
        AgentNotFoundError = ()
        DeliveryError = ()
        TransportTimeoutError = ()

    if isinstance(exc, (AgentNotFoundError, DeliveryError, TransportTimeoutError)):
        return True

    msg = str(exc).lower()
    return any(frag in msg for frag in _BROKER_UNAVAILABLE_FRAGMENTS)


class BaseBrokeredClient:
    """Base for brokered clients that forward requests to an agent over the broker.

    Subclasses pass *target_agent_id* (the broker-registered ID of the
    recipient agent) and *default_reply* (the fallback when the broker
    returns an empty reply).  ``consult()`` forwards any ``**extra_payload``
    keys into the request dict alongside the request text.

    The request text is sent under ``_request_key`` (default ``"message"``,
    matching the mill board-manager's contract).  Subclasses whose recipient
    expects a different key override it — e.g. the calendar agent requires
    ``"instruction"``.
    """

    _request_key: str = "message"

    def __init__(
        self,
        settings: Any,
        *,
        target_agent_id: str,
        default_reply: str,
    ) -> None:
        """Store the broker settings and build a brokered requester."""
        # Lazy import: robotsix-agent-comm is the optional `broker` extra.
        from robotsix_agent_comm.sdk import BrokeredRequester  # noqa: I001

        self._s = settings
        self._target_agent_id = target_agent_id
        self._requester = BrokeredRequester(
            settings.agent_id,
            target_agent_id,
            broker_host=settings.broker_host,
            broker_port=settings.broker_port,
            broker_scheme=settings.broker_scheme,
            broker_token=settings.broker_token.get_secret_value(),
            timeout=settings.timeout,
            default_reply=default_reply,
        )

    def _check_reachable(self) -> tuple[bool, str]:
        """Pre-flight check that the broker is reachable and target registered.

        Does a cheap authenticated ``GET /agents`` (short timeout) so a
        genuinely-down broker or an offline recipient fails in a few seconds
        instead of waiting out the full request timeout.  Returns
        ``(ok, reason)``: ``ok=False`` means fail fast with *reason*.

        Best-effort: any ambiguous outcome (non-200, unparsable body,
        unexpected error) returns ``(True, "")`` so a flaky probe never blocks
        an otherwise-valid request.  Blocking (uses ``httpx``); call via
        :func:`asyncio.to_thread`.
        """
        s = self._s
        url = f"{s.broker_scheme}://{s.broker_host}:{s.broker_port}/agents"
        try:
            resp = httpx.get(
                url,
                headers={
                    "Authorization": f"Bearer {s.broker_token.get_secret_value()}"
                },
                timeout=_PREFLIGHT_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            return False, f"broker unreachable ({exc})"

        if resp.status_code != 200:
            # Auth/rate-limit/other hiccup — don't block the real request on it.
            return True, ""
        try:
            agents = {a.get("agent_id") for a in resp.json().get("agents", [])}
        except ValueError, AttributeError, TypeError:
            return True, ""
        if self._target_agent_id not in agents:
            return False, (
                f"agent '{self._target_agent_id}' is not registered on the broker"
            )
        return True, ""

    async def consult(
        self,
        request: str,
        *,
        empty_reply: str,
        error_label: str,
        **extra_payload: object,
    ) -> str:
        """Send *request* to the target agent, forwarding **extra_payload.

        Runs a fast pre-flight reachability check first; raises
        :class:`BrokerUnavailableError` (in seconds) when the broker is down or
        the target agent is not registered.  Otherwise sends the request with
        the configured (generous) timeout.  Other broker/timeout/recipient
        errors are caught and returned as a short message string.
        """
        if not request.strip():
            return empty_reply

        # Fail fast on a down broker / offline recipient instead of hanging for
        # the full request timeout.
        ok, reason = await asyncio.to_thread(self._check_reachable)
        if not ok:
            logger.warning("%s preflight failed: %s", error_label, reason)
            raise BrokerUnavailableError(reason)

        try:
            payload: dict[str, object] = {self._request_key: request, **extra_payload}
            return await asyncio.to_thread(self._requester.request, payload)
        except Exception as exc:  # noqa: BLE001 — surface as text, never crash chat
            if _is_broker_unavailable(exc):
                logger.warning(
                    "%s consult failed (broker unavailable): %s", error_label, exc
                )
                raise BrokerUnavailableError(str(exc)) from exc
            logger.warning("%s consult failed: %s", error_label, exc)
            return f"The {error_label} request could not be completed: {exc}"
