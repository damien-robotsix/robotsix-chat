"""Server Settings Models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
    memory (cognee recall), knowledge store, feedback runner, and
    diagnostics store.  Results are exposed via ``GET /health`` and logged.

    Attributes:
        enabled: Master switch.  When ``False``, no health checks run.
        check_interval_seconds: Seconds between scheduled health-check
            cycles.  Default ``300`` (5 minutes).

    """

    enabled: bool = True
    check_interval_seconds: float = Field(default=300.0, gt=0)
    model_config = ConfigDict(extra="forbid")


class EvergoingSettings(BaseModel):
    """Evergoing-session settings — the single never-ending chat session.

    When enabled, exactly one *evergoing* session is created on boot and
    kept alive across restarts: it is never auto-closed or auto-evicted and
    always appears in the operator's session list flagged ``evergoing``.  A
    background scheduler runs every *trim_interval_seconds* over **every
    session** (the single context-reduction mechanism — idle compaction was
    removed) and, **only when new turns have arrived since the last pass**,
    asks a cheap summary-tier model whether the conversation's subject has
    clearly changed and how many finished leading turns can be dropped,
    then physically trims them from the session.  A no-input interval
    performs zero LLM calls.

    Attributes:
        enabled: Master switch.  When ``False`` (default) no evergoing
            session is created and the trim scheduler does not run.  Set to
            ``true`` to activate the feature.
        trim_interval_seconds: Seconds between scheduled trim passes.
            Default ``1800`` (30 minutes).
        keep_min_recent: Minimum number of most-recent turns the trim pass
            must always keep — guarantees the in-flight turn is never
            trimmed away.  Default ``2``.
        min_fresh_turns: Minimum fresh turns since the last trim before a
            session is even shown to the decision model — the gate skips
            without advancing the watermark, so short exchanges accumulate
            until it opens.  Default ``3``.

    """

    enabled: bool = False
    trim_interval_seconds: float = Field(default=1800.0, gt=0)
    keep_min_recent: int = Field(default=2, ge=1)
    min_fresh_turns: int = Field(default=3, ge=1)
    model_config = ConfigDict(extra="forbid")
