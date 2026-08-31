"""Ref Doc Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class RefDocsSettings(BaseModel):
    """Read-only reference-docs tool for the agent.

    Lets the agent fetch documentation from allowlisted GitHub repos on
    demand. Primarily used to consult the board-workflow reference repo
    when deciding whether a ticket needs manual human action. The tool is
    strictly read-only, fetches are on-demand (no bulk ingestion), and only
    repos in the *repos* allowlist are reachable.

    Attributes:
        enabled: Master switch. When ``False``, no refdocs tools are offered.
        repos: Allowlist of ``owner/name`` GitHub repos the agent may read.
            The board-workflow reference repo goes here. The tool refuses
            any repo not in this list.
        ref: Default git ref/branch to read from (``"main"``).
        base_url: Overridable base URL for GitHub Enterprise.
        timeout: Per-request HTTP timeout in seconds.

    Authentication reuses the ``direct_repo`` GitHub App credentials when
    they are configured; without them only public repositories are reachable.

    """

    enabled: bool = False
    repos: list[str] = Field(default_factory=list)
    ref: str = "main"
    base_url: str = "https://api.github.com"
    timeout: float = 30.0
    model_config = ConfigDict(extra="forbid")


class VersionCheckSettings(BaseModel):
    """Self-version-check tool: compare running version vs latest GitHub release.

    Disabled by default. When enabled, the agent gains a tool that reports the
    running ``robotsix_chat.__version__`` and the latest published release of
    the configured GitHub repo, and flags when the deployment is out of date.

    Attributes:
        enabled: Master switch. When ``False``, no version-check tool is offered.
        repo: GitHub ``owner/name`` (e.g. ``robotsix/robotsix-chat``). Required
            when *enabled*.
        base_url: Overridable base URL for GitHub Enterprise.
        timeout: Per-request HTTP timeout in seconds.
        cache_ttl: Seconds to cache the latest-release lookup (monotonic clock).

    Authentication reuses the ``direct_repo`` GitHub App credentials when
    they are configured; without them the lookup is unauthenticated (subject
    to lower rate limits).

    Note: the check is only meaningful when releases bump
    ``robotsix_chat.__version__`` in lockstep with the GitHub release tag.

    """

    enabled: bool = False
    repo: str = ""
    base_url: str = "https://api.github.com"
    timeout: float = 30.0
    cache_ttl: float = 300.0
    model_config = ConfigDict(extra="forbid")


class DiagnosticsSettings(BaseModel):
    """Diagnostics capture and systemic fix surfacing.

    When enabled, the agent captures diagnostic bundles for failure events
    and can detect recurring failure categories.  When a category crosses
    the recurrence threshold a ``FixProposal`` is auto-generated (but NOT
    auto-applied) for agent or human review.

    Applied fixes are tracked in the effectiveness store; after the
    observation window elapses a ``FixEffectivenessReport`` is generated
    comparing pre-fix and post-fix recurrence counts.

    Attributes:
        enabled: Master switch.  Default ``True``.
        store_path: Path to the diagnostic-event JSON persistence file.
            Default ``/data/diagnostics.json``.
        proposals_path: Path to the fix-proposal JSON persistence file.
            Default ``/data/fix_proposals.json``.
        effectiveness_path: Path to the effectiveness-report JSON
            persistence file.  Default ``/data/diagnostics_effectiveness.json``.
        recurrence_threshold: Minimum number of occurrences within the
            window to trigger a recurrence alert.  Default ``3``.
        recurrence_window_days: Look-back window in days for recurrence
            detection.  Default ``30``.
        observation_window_days: Days after a fix is applied to wait before
            generating an effectiveness report.  The pre-fix and post-fix
            windows are both this many days.  Default ``30``.
        mill_events_path: Path to the mill's JSONL diagnostic event store,
            used by ``read_diagnostic_events`` to inspect events emitted
            by the mill.  Default ``/data/robotsix-mill/diagnostic_events.jsonl``.

    """

    enabled: bool = True
    store_path: str = "/data/diagnostics.json"
    proposals_path: str = "/data/fix_proposals.json"
    effectiveness_path: str = "/data/diagnostics_effectiveness.json"
    recurrence_threshold: int = 3
    recurrence_window_days: int = 30
    observation_window_days: int = 30
    mill_events_path: str = "/data/robotsix-mill/diagnostic_events.jsonl"
    model_config = ConfigDict(extra="forbid")
