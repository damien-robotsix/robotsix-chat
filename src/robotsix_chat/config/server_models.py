"""Server Settings Models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from robotsix_chat.config.constants import drop_blank_numeric_sentinels


class VolumeToolsSettings(BaseModel):
    """Local volume-directory listing — discover files on mounted volumes.

    When enabled, the agent gains a ``list_volume_files`` tool that returns
    the contents of a directory under the configured *root_path*.  This
    lets the agent discover available files (e.g. a mounted knowledge
    store, investigation data) without guessing individual file paths.

    This is a read-only, local-filesystem-only primitive — no remote
    access, no write capability, no traversal outside the root.

    Attributes:
        enabled: Master switch.  Default ``True`` — this is a purely
            local, read-only, no-credential primitive.
        root_path: The directory that all listing operations are scoped
            under.  Paths outside this root are refused.  Default
            ``/data``.

    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    root_path: str = "/data"


class ContinuationSettings(BaseModel):
    """Post-restart continuation — auto-resume a conversation after a restart.

    When enabled, the agent gains tools to schedule a continuation that
    fires automatically on the next boot, so work-in-progress resumes
    without human intervention.  The primary use case is a self-restart
    to pick up a newly-deployed capability: before calling
    ``self_restart``, the agent schedules a continuation with the current
    session id and a resume prompt.

    Attributes:
        enabled: Master switch.  Default ``False``.
        store_path: Path to the JSON persistence file.  Must be on a
            persistent volume so the continuation survives container
            recreation.  Default ``/data/continuation.json``.
        max_consecutive: Maximum number of consecutive auto-continuations
            before the guardrail blocks further automatic firing.  This
            prevents a restart→continue→restart→continue loop from running
            indefinitely.  Default ``3``.

    """

    enabled: bool = False
    store_path: str = "/data/continuation.json"
    max_consecutive: int = Field(default=3, ge=1)
    model_config = ConfigDict(extra="forbid")


class HealthSettings(BaseModel):
    """Periodic health-check settings.

    When enabled, a background scheduler runs every *check_interval_seconds*
    (default 300 s / 5 min) and verifies that critical subsystems are
    reachable and producing expected output:
    memory (long-term recall), knowledge store, feedback runner, and
    diagnostics store.  It also watches the container's cgroup memory
    usage and warns *before* the OOM killer fires.  Results are exposed
    via ``GET /health`` and logged.

    Attributes:
        enabled: Master switch.  When ``False``, no health checks run.
        check_interval_seconds: Seconds between scheduled health-check
            cycles.  Default ``300`` (5 minutes).
        memory_warn_fraction: Fraction of the container's cgroup memory
            limit at which the ``container_memory`` check flips to
            ``WARNING`` (logged as a pre-OOM alert).  Default ``0.85``
            (85 %).  Must be in ``(0, 1]``.

    """

    enabled: bool = True
    check_interval_seconds: float = Field(default=300.0, gt=0)
    memory_warn_fraction: float = Field(default=0.85, gt=0, le=1)
    model_config = ConfigDict(extra="forbid")


class MemoryComponentSettings(BaseModel):
    """robotsix-memory component integration (long-term fleet memory).

    The evergoing summary scheduler pushes every session summary it writes
    to the memory component's ``/remember`` endpoint, and a final summary
    is pushed when a conversation is closed. Pushes reuse a stable
    per-session ``document_id`` with ``update_mode="replace"``, so
    re-summaries supersede rather than duplicate. Writes are best-effort:
    a memory outage never breaks chat.

    Attributes:
        enabled: Master switch for the summary → memory pushes. The chat
            agent's direct skill access to the component (via the deploy
            roster) is independent of this flag.
        url: Base URL of the memory component on the internal network.
        timeout_seconds: Per-push HTTP timeout.

    """

    enabled: bool = True
    url: str = "http://memory:8080"
    timeout_seconds: float = Field(default=60.0, gt=0)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _strip_blank_numeric(cls, data: Any) -> Any:
        """Strip blank ``""`` numeric sentinels so stored configs load."""
        return drop_blank_numeric_sentinels(cls, data)


class EvergoingSettings(BaseModel):
    """Evergoing-session settings — the single never-ending chat session.

    When enabled, exactly one *evergoing* session is created on boot and
    kept alive across restarts: it is never auto-closed or auto-evicted and
    always appears in the operator's session list flagged ``evergoing``.  A
    background scheduler runs every *trim_interval_seconds* over **every
    session** (the single context-reduction mechanism — idle compaction was
    removed).  The gate is deterministic (no decision LLM): a session is
    compacted only when new turns arrived since the last pass AND more
    than *keep_recent_runs* completed runs accumulated beyond the previous
    summary.  Everything before the last *keep_recent_runs* runs is folded
    into the session's summary; the recent runs stay verbatim and are not
    shown to the summariser.  A no-input interval performs zero LLM calls.

    Attributes:
        enabled: Master switch.  When ``False`` (default) no evergoing
            session is created and the summary scheduler does not run.  Set
            to ``true`` to activate the feature.
        trim_interval_seconds: Seconds between scheduled compaction passes.
            Default ``1800`` (30 minutes) — a session is summarised at most
            once per interval.
        keep_recent_runs: Number of most-recent completed runs (operator
            message + assistant final answer) kept verbatim in the replay
            and excluded from the summariser input.  A session is only
            compacted when MORE than this many fresh runs exist beyond the
            previous summary.  Default ``5``.

    """

    enabled: bool = False
    trim_interval_seconds: float = Field(default=1800.0, gt=0)
    keep_recent_runs: int = Field(default=5, ge=1)
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _strip_blank_numeric(cls, data: Any) -> Any:
        """Drop legacy sentinels and removed fields so old configs load.

        Blank ``""`` numeric sentinels are stripped as everywhere else.
        ``keep_min_recent`` and ``min_fresh_turns`` belonged to the removed
        subject-aware trim design; deployed configs that still pin them
        must not crash the boot (``extra="forbid"``), so they are dropped.
        """
        if isinstance(data, dict):
            data = {
                k: v
                for k, v in data.items()
                if k not in ("keep_min_recent", "min_fresh_turns")
            }
        return drop_blank_numeric_sentinels(cls, data)
