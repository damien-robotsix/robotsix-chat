"""Background watcher that surfaces central-deploy caretaker findings.

This is a deterministic, **model-free** escalation path: it polls
``GET {central_deploy.url}/caretaker/status`` on a short interval with no
agent/LLM turn involved and broadcasts an SSE desktop notification to
every connected operator browser for each error-severity finding whose
``kind`` is in ``caretaker.surface_kinds``.  Because no model turn is
required, the path keeps working while the fleet has no usable model —
exactly the outage an LLM-driven escalation cannot report.

De-duplication is per ``(kind, component)`` pair: a finding is announced
once, re-announced when its body text changes, and followed by a
"resolved" notification when it disappears from the endpoint.

A persistent ``user_chat`` operator panel (open/update/auto-close) is a
planned follow-up — see ``tasks/TASKS.md`` — because a fleet-wide panel
needs a session-ownership/UI design decision this path does not.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from robotsix_chat.caretaker.client import fetch_caretaker_status
from robotsix_chat.chat.events import SSE_NOTIFICATION_TYPE

if TYPE_CHECKING:
    from robotsix_chat.caretaker.models import CaretakerFinding
    from robotsix_chat.subsessions.worker import SubsessionEnv

logger = logging.getLogger(__name__)

# Floor on the poll interval so a misconfigured tiny value cannot hammer
# central-deploy.
_MIN_POLL_INTERVAL_SECONDS: float = 15.0


def _publish_notification(
    env: SubsessionEnv,
    *,
    title: str,
    body: str,
) -> None:
    """Broadcast an SSE desktop notification to every connected browser."""
    if env.event_sink is None:
        return
    env.event_sink.publish_all(
        {
            "type": SSE_NOTIFICATION_TYPE,
            "title": title,
            "body": body,
            "urgency": "high",
        }
    )


def _notify_finding(env: SubsessionEnv, finding: CaretakerFinding) -> None:
    """Announce a new or changed finding to the operator."""
    _publish_notification(env, title=finding.title, body=finding.render_body())
    logger.warning(
        "Caretaker: surfaced %s finding for component %r — %s",
        finding.kind,
        finding.component or "fleet-wide",
        finding.detail or "(no detail)",
    )


def _notify_resolved(env: SubsessionEnv, kind: str, component: str, title: str) -> None:
    """Announce that a previously-surfaced finding has cleared."""
    scope = component or "fleet-wide"
    _publish_notification(
        env,
        title=f"Resolved: {title}",
        body=(f"[{scope}] The central-deploy caretaker {kind} finding has cleared."),
    )
    logger.info("Caretaker: %s finding for component %r resolved.", kind, scope)


async def watch_caretaker_findings(env: SubsessionEnv) -> None:
    """Poll central-deploy caretaker status and surface findings to the operator.

    Runs forever as an asyncio task until cancelled.  A no-op (returns
    immediately) when the feature is disabled or no central-deploy URL is
    configured.
    """
    cfg = getattr(env.settings, "caretaker", None)
    if cfg is None or not cfg.enabled:
        logger.info("Caretaker watcher: disabled (caretaker.enabled=false).")
        return

    central = env.settings.central_deploy
    if not central.url:
        logger.info(
            "Caretaker watcher: no central_deploy.url configured — not starting."
        )
        return

    surfaced = {kind.lower() for kind in cfg.surface_kinds}
    poll_interval = max(cfg.poll_interval_seconds, _MIN_POLL_INTERVAL_SECONDS)

    # Last-announced notification body per (kind, component) pair — the
    # title is retained so the resolution notice can name the finding.
    active: dict[tuple[str, str], tuple[str, str]] = {}

    logger.info(
        "Caretaker watcher started (interval=%.0fs, kinds=%s).",
        poll_interval,
        ", ".join(sorted(surfaced)) or "(none)",
    )

    while True:
        try:
            findings = await fetch_caretaker_status(
                central, timeout=cfg.request_timeout
            )
        except asyncio.CancelledError:
            logger.info("Caretaker watcher: cancelled — shutting down.")
            raise
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Caretaker watcher: poll failed (%s) — retrying.", exc)
            await asyncio.sleep(poll_interval)
            continue
        except Exception:
            logger.exception(
                "Caretaker watcher: unexpected error in poll loop — retrying."
            )
            await asyncio.sleep(poll_interval)
            continue

        current: dict[tuple[str, str], CaretakerFinding] = {}
        for finding in findings:
            if not finding.is_error:
                continue
            if finding.kind not in surfaced:
                continue
            current[finding.dedup_key] = finding

        # New or changed findings → notify.
        for key, finding in current.items():
            body = finding.render_body()
            previous = active.get(key)
            if previous is not None and previous[1] == body:
                continue
            _notify_finding(env, finding)
            active[key] = (finding.title, body)

        # Findings that disappeared since last poll → resolution notice.
        for key in list(active):
            if key not in current:
                title, _body = active.pop(key)
                kind, component = key
                _notify_resolved(env, kind, component, title)

        await asyncio.sleep(poll_interval)
