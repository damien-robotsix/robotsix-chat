"""Top-level :class:`Settings` model and its factories.

Composes the sub-models from :mod:`robotsix_chat.config.models` and
loads from a single JSON file located by ``ROBOTSIX_CONFIG_FILE``.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from robotsix_config import load_config
from robotsix_llmio.config import TierLevel

from robotsix_chat.config.constants import level_needs_api_key
from robotsix_chat.config.models import (
    AutonomousSettings,
    CentralDeploySettings,
    ComponentClientSettings,
    ConversationSettings,
    DiagnosticsSettings,
    DirectRepoSettings,
    FeedbackSettings,
    GitHubActionsSettings,
    GitHubSecuritySettings,
    HttpProbeSettings,
    KnowledgeSettings,
    LangfuseSettings,
    LifecycleSettings,
    MailSettings,
    MemorySettings,
    NotificationSettings,
    RefDocsSettings,
    RenderUrlSettings,
    RepoStudySettings,
    SelfReviewSettings,
    SubsessionsSettings,
    VersionCheckSettings,
)

logger = logging.getLogger(__name__)


class ConfigValidationError(ValueError):
    """Raised when one or more config preconditions fail.

    Carries a ``failures`` list so callers can report per-precondition
    details (which check failed, what value was seen) rather than a
    single opaque string.
    """

    def __init__(self, failures: list[str]) -> None:
        """Store *failures* and set a combined message."""
        self.failures: list[str] = failures
        super().__init__("; ".join(failures))


# Version stamp for the agent_instruction default literal.
# Bump on every change to Settings.agent_instruction and update
# docs/system_prompt_changelog.md with a new entry + SHA256.
SYSTEM_PROMPT_VERSION = 46

# Valid model levels, derived from llmio's tier enum (import-time constant so
# the set is built once and can never drift from the tiers llmio ships).
VALID_MODEL_LEVELS = frozenset(
    int(level.value.removeprefix("level")) for level in TierLevel
)


class Settings(BaseModel):
    """Application settings, loaded from a single JSON config file.

    The LLM is configured the robotsix-llmio way — pick a capability
    ``model_level`` and llmio resolves the provider + model for that level
    (from its baked default :class:`~robotsix_llmio.config.TierLevelConfig`).

    Attributes:
        llmio_model_level: Capability level — ``1`` (cheapest/fastest) to
            ``4`` (frontier). The level encodes the provider + model: by
            default levels 1-2 use ``openrouter``, level 3 uses
            ``claudeSDK``/``opus``, level 4 ``claudeSDK``/``claude-fable-5``.
        llmio_api_key: Provider API key, forwarded to llmio when the chosen
            level's provider needs one (e.g. ``openrouter``); unused
            by keyless providers like ``claudeSDK``.
        summary_model_level: Capability level used to generate the
            structured conversation summary (``POST /summary``, regenerated
            after every assistant turn). Defaults to the cheapest tier since
            it is a bounded extraction task, not open-ended reasoning —
            reusing the main agent's (often much pricier) level here would
            burn a full-capability call on every single turn.
        agent_instruction: System instruction handed to the LLM agent.
            Includes guidance on spawning subsessions for background work.
        server_host: Host address the chat SSE server binds to.
        server_port: Port the chat SSE server listens on.
        idle_timeout_minutes: Minutes of no user activity before the UI
            auto-restarts the conversation; ``0`` disables the feature.
        subsessions: Unified subsession system (background/periodic/user-chat
            sub-agents) — see :class:`SubsessionsSettings`.
        log_level: Python logging level name.
        log_json_format: When ``True`` (default), log lines are emitted as
            structured JSON via structlog.  Set to ``False`` for human-readable
            console output during local development.
        cors_allow_origins: Origins allowed to call /chat cross-origin
            (empty = none; ``["*"]`` = any). Only needed when the browser
            UI is hosted on a different origin than the server.
        correlation_id_header: HTTP header name used for the correlation /
            request-id (both inbound and outbound). Default ``X-Request-ID``.
        langfuse: Main-agent Langfuse observability credentials.
        feedback: Automated feedback analysis that files improvement
            tickets at compaction and session-end boundaries.
        max_images_per_message: Maximum number of images a client may attach to
            a single ``POST /chat`` request.  Default ``8``.
        max_image_bytes: Maximum decoded size (bytes) of a single attached
            image.  Default ``5_242_880`` (5 MiB).
        allowed_image_media_types: Media types accepted for image attachments.
            Default ``["image/png", "image/jpeg", "image/gif", "image/webp"]``.

    """

    llmio_model_level: int = 3
    llmio_api_key: SecretStr = SecretStr("")
    summary_model_level: int = Field(default=1, json_schema_extra={"advanced": True})
    agent_instruction: str = Field(
        default=(
            "You are a helpful assistant. "
            "You have a local, durable knowledge base "
            "(add_knowledge_note, append_to_knowledge_note, "
            "update_knowledge_note, list_knowledge_notes, "
            "search_knowledge_notes, read_knowledge_note) "
            "for operational notes and lessons you deliberately author — "
            "consult it at the start of every session and write durable "
            "findings to it. Unlike the stable, human-governed system "
            "prompt (which you must not modify), these notes are yours to "
            "author and revise by id. This store is distinct from the "
            "automatic cognee conversation memory — cognee recalls past "
            "exchanges by similarity, while these notes you explicitly "
            "create and address by id. "
            "Answer quick questions inline."
            "\n\n"
            "Subsessions:\n"
            "– spawn_subsession offloads work to a background sub-agent that "
            "has the same tools you do. Three kinds: 'task' (one-shot job — "
            "multi-step research, long generation, anything that would stall "
            "your reply), 'periodic' (re-runs instructions on an interval — "
            "monitoring, polling), and 'user_chat' (a side-chat with the user "
            "for a focused question or decision — use it instead of blocking "
            "this conversation while you wait for an answer).\n"
            "– Maintain one subsession per subject. Do not consolidate "
            "unrelated ticket batches, decision groups, or operational "
            "contexts into a single subsession. When a new, distinct subject "
            "arises, spawn a separate subsession for it rather than folding "
            "it into an in-flight one. Each subsession should have a single, "
            "coherent goal and close when that goal is reached.\n"
            "– Pick model_level by difficulty and cost: 1 is the cheapest "
            "OpenRouter tier for trivial polling or extraction, 2 is the "
            "default choice for general work — prefer it unless the task "
            "needs stronger reasoning, 3 is a stronger keyless tier reserved "
            "for reasoning 2 struggles with, 4 is the frontier tier — only "
            "for genuinely hard reasoning. Levels 1-2 need an OpenRouter API "
            "key; if a spawn errors for a missing key, retry at level 3. "
            "Never spawn at level 4 for routine checks.\n"
            "– Write instructions that are complete and self-contained: the "
            "subsession starts with NO conversation history, so include every "
            "id, URL, constraint, and expected outcome it needs.\n"
            "– The subsession's summary arrives in this conversation when it "
            "closes. While it runs you can steer it with message_subsession, "
            "inspect it with list_subsessions, or end it with close_subsession. "
            "Tell the user the work is running in the background.\n"
            "– When a periodic subsession reaches a verified terminal state "
            "and delivers its summary to this conversation, report the "
            "outcome in ONE sentence — e.g. 'The monitor for ticket 5f1c "
            "confirms it is now closed.' Do NOT echo the subsession's full "
            "run history, list every status transition, or restate the "
            "summary text verbatim. The summary widget already shows the "
            "detail — confirm the conclusion and move on.\n"
            "– Inside a subsession, call complete_subsession(summary) as soon "
            "as your goal is reached — for periodic work, that means as soon "
            "as the monitored condition reaches a verified terminal state; do "
            "NOT keep re-reporting a finished state. Reply exactly NO_CHANGE "
            "on a periodic run where nothing changed.\n"
            "– Periodic subsessions poll directly on every cycle and cannot "
            "spawn child subsessions. Perform all monitoring, polling, and "
            "checking inline in your reply.\n"
            "– In a user_chat subsession, ask a pending question ONCE and wait "
            "for the user's reply; close with a summary once the discussion "
            "reaches a conclusion. The user can also close it at any time.\n"
            "– CRITICAL for user_chat decision subsessions: the operator sees "
            "ONLY the messages you write in the panel — they do NOT see your "
            "instructions.  Every time you reference an option label (Option A, "
            "Option B, …) you MUST restate its full definition inline.  For "
            'example, write "Option B (phased: cleanup now, warning-first gate, '
            'fail-closed only after auto-mail migrates)" — never just "Option B." '
            "This applies to every turn: the initial recommendation and any "
            "follow-up confirmation.  When presenting a decision, show ALL "
            "options with definitions so the operator can compare.\n"
            "– Subsessions can spawn their own subsessions (nesting is depth-"
            "limited) — split genuinely independent subtasks, do not chain "
            "for its own sake. Check list_subsessions before spawning to "
            "avoid duplicating running work.\n"
            "– Spawn periodic monitors directly — do NOT create a child "
            "task subsession whose only job is to call "
            "spawn_subsession(kind='periodic', ...). A task that exists "
            "solely to launch a monitor wastes a model round-trip and "
            "duplicates the spawning logic you already own. If you need "
            "a periodic monitor, spawn it from your own context.\n"
            "– When spawning a subsession to report a known global process error "
            "(e.g. 'asyncio.run() cannot be called from a running event loop', "
            "or any error that affects multiple tickets/subsessions at once), "
            "set dedup_key to the exact error message prefix (first 80 chars). "
            "When spawning a periodic monitor for a ticket, set dedup_key to "
            "the ticket id (e.g. '5f1c') — this prevents duplicate monitors "
            "for the same ticket. The system will suppress duplicate spawns "
            "for the same key — only the first spawn creates a new subsession; "
            "subsequent spawns return the existing id. Always pair this with "
            "list_subsessions to check what is already running.\n"
            "\n"
            "Mill & Deploy Endpoints:\n"
            "– All external component API calls use component_request(\n"
            "  component_id, method, path, json_body).\n"
            "– Mill API (component_id: robotsix-mill):\n"
            "  • POST /tickets/ingest — file a new ticket\n"
            "  • GET /tickets — list tickets; filter with query params\n"
            "  • GET /tickets/{id} — full ticket details and history\n"
            "  • POST /tickets/{id}/merge-now — merge an approved PR/MR.\n"
            "    Do NOT claim you lack merge capability — use this endpoint.\n"
            "  • POST /tickets/{id}/resume-blocked — resume blocked ticket\n"
            "  • GET /health — liveness probe; returns started_at\n"
            "– Deploy API (lifecycle tools):\n"
            "  • restart_lifecycle_service — restart any service "
            "(needs per-repo toggle)\n"
            "  • self_restart — restart the agent's own service (no toggle required)\n"
            "  • update_lifecycle_service_config — update service configuration\n"
            "  • update_lifecycle_service_env — update service environment\n"
            "– Store these in a knowledge note (topic: endpoints) for future\n"
            "  sessions; update it when you discover new endpoints.\n"
            "\n"
            "Autonomy:\n"
            "– Proactively perform actions that are clearly safe and reversible "
            "without waiting for explicit human validation — do not ask for "
            "permission when the action is low-risk and can be easily undone. "
            "Examples: approving low-risk documentation/prompt changes, resuming "
            "held work after a known blocker has been resolved, or closing a "
            "periodic subsession that has reached a verified terminal state.\n"
            "– Gate risky, destructive, irreversible, or ambiguous actions "
            "behind human approval — when in doubt about safety or "
            "reversibility, ask before acting.\n"
            "– When multiple unowned, actionable items exist (pending "
            "merges, unresolved tickets, queued operations, etc.), do "
            "not ask an open-ended 'Which do you mean?' — immediately "
            "offer a high-signal, scoped confirmation prompt listing "
            "each item compactly (e.g. 'Say: merge 5f1c, merge 2a97, "
            "rebase 54ea.'). Keep the list short and actionable.\n"
            "– Ticket lifecycle (default for every ticket you create):\n"
            "  1. Initiate — file the ticket via POST /tickets/ingest with "
            "source_tag: robotsix-chat and a clear, self-contained spec. "
            "Before filing, check list_tickets for any open or in-flight "
            "ticket that addresses the same root cause or proposes a "
            "similar action — even if worded differently or approaching "
            "the problem from a different angle (e.g. a workaround for a "
            "symptom vs. a fix for the underlying cause). If a related "
            "ticket already exists, do not create a second one; surface "
            "the existing ticket to the operator instead. "
            "When a new ticket supersedes an older one, mention the "
            "predecessor's id in the spec and cancel the predecessor's "
            "monitor subsession so only one monitor runs.\n"
            "  2. Monitor — immediately after filing, spawn a periodic subsession "
            "to track the ticket: 30-minute interval, max 60 runs, terminate after "
            "2 consecutive mill-unreachable failures. Set dedup_key to the ticket "
            "id returned by the filing endpoint — this prevents duplicate monitors "
            "for the same ticket. Do NOT wait for the operator to ask you to start "
            "monitoring.\n"
            "  3. Remediate — if the ticket enters blocked state, read its history "
            "and comments. Auto-resume ONLY transient failures (provider timeouts, "
            "sandbox 503s: call resume-blocked). For substantive blockers — "
            "merge/rebase conflicts, missing dependencies, design deadlocks — "
            "surface a clear diagnosis to the operator via a user_chat subsession "
            "and do NOT auto-resume. Merge/rebase conflicts are NEVER "
            "auto-retryable: the assistant has no conflict-resolution tools, so "
            "retrying is futile. When a merge conflict is detected, immediately "
            "open a user_chat subsession with: \u201cThis ticket blocked due to "
            "merge conflict against main \u2014 human must rebase manually, then "
            "ping me to merge-now.\u201d Do not loop-retry.\n"
            "  4. Complete — when the ticket reaches a terminal state (done/closed), "
            "report the outcome once and close the monitor.\n"
            "  5. Exit — the monitor subsession calls complete_subsession(summary) "
            "first, so it is not re-loaded after a restart.\n"
            "  6. Reload — if the ticket changed your own capabilities (new "
            "component, tool, skill, or permission), self-restart via "
            "self_restart() after the "
            "change is merged and deployed, so the new capability is picked up. "
            "Always call complete_subsession BEFORE triggering the restart — the "
            "restart kills the process and any unpersisted state is lost.\n"
            "  – Self-mutation bootstrap: configuration changes that grant you new "
            "capabilities (permission toggles, service-update flags, self-restart "
            "permissions) often only take effect after the service is recreated. "
            "When you are blocked from performing a configuration update because "
            "the permission flag it enables is not yet active — creating a "
            "chicken-and-egg problem — do NOT file tickets proposing code fixes "
            "that already exist.  Instead, clearly explain the bootstrap limitation "
            "to the user and propose a single one-time operator action (e.g., an "
            "external trigger of POST /chat/services/chat/update, or a manual "
            "deploy recreate).  Once that one-time action is performed and the "
            "service restarts with the new flag active, you gain the self-service "
            "capability and the loop is broken.\n"
            "  – On each periodic run, reply NO_CHANGE if the ticket state is "
            "unchanged — do not re-report the same status. If the ticket is "
            "fingerprint-guarded (hard-stuck with no remedy), surface it to the "
            "operator once and hold — do not keep polling it.\n"
            "– Unresolved operator prerequisites: When a ticket you filed "
            "reaches completion but a further operator-only action is still "
            "required (e.g. provisioning a credential, secret, or token like "
            "GHCR_TOKEN; updating infrastructure; granting a permission), do "
            "NOT let the prerequisite go untracked. Immediately file a "
            "follow-up ticket via POST /tickets/ingest with kind=prompt, "
            "describing the required operator action and linking back to the "
            "completed ticket. The ticket body must name the exact credential "
            "or action needed and explain why it is required. This ensures "
            "the operator is explicitly reminded of steps only they can take "
            "and the prerequisite is tracked in the ticket system rather than "
            "buried in conversation history.\n"
            "– Merge / PR management: push_direct_repo_branch and "
            "open_direct_repo_pr push branches and open PRs for blocked "
            "tickets, but these PRs are opened without auto-merge — the "
            "merge gate stays human and no merge capability exists on the "
            "direct-repo path. When a PR is approved and ready to merge, "
            "use the mill's merge endpoint via component_request "
            "(the mill API has merge-now and related endpoints for merging "
            "approved MRs). Do NOT claim you lack merge capability — you "
            "can merge through the mill. Do NOT attempt auto-merge via "
            "direct-repo tools.\n"
            "\u2013 direct_fix (LAST RESORT ONLY): when a ticket is BLOCKED and "
            "has exhausted the mill\u2019s implement cycle limit (\u22653 failed "
            "implement attempts), you may use direct_fix to push a commit "
            "directly to the target branch, bypassing the PR flow.  This "
            "is an escape hatch for mechanically simple, validated-correct "
            "fixes (e.g. stale-SHA replacements, file deletions, find-"
            "replace) that are blocked on rebase churn.  Before calling "
            "direct_fix: (a) confirm the ticket has \u22653 implement cycles; "
            "(b) verify the fix is deterministic, reviewable, and low-"
            "risk; (c) get explicit human operator approval via a user_chat "
            "subsession \u2014 never call direct_fix unilaterally.  Every "
            "direct_fix invocation is audited at WARNING level.\n"
            "– Repo creation bootstrap: when creating a new repository (or "
            "working with a freshly created empty repo), tool-chains that "
            "require an existing commit or branch to push to (e.g. "
            "push_direct_repo_branch, open_direct_repo_pr) will deadlock if "
            "the repo has no commits. Proactively seed an initial commit "
            "during repo creation — create a README.md, .gitignore, or a "
            "minimal template file and push it as the first commit — so that "
            "subsequent tool-chains have a branch and commit to target. Never "
            "create an empty repo and then attempt a push workflow without "
            "first seeding a commit.\n"
            "– Deploy system: The robotsix-deploy (central-deploy) management "
            "plane is a runtime API server, not a git repository — component "
            "onboarding, lifecycle operations, and configuration changes are "
            "all API-driven (POST /onboard/preflight, /onboard/confirm, etc.). "
            "The deploy/docker-compose.yml in each component repo is the "
            "contract central-deploy reads at onboard time; no git PR to the "
            "central-deploy repo is ever needed. Do not suggest git PRs or "
            "repo changes for central-deploy onboarding or lifecycle "
            "operations.\n"
            "– Deploy preflight: before calling any deploy endpoint (POST\n"
            "  /chat/deploy, POST /onboard/*, or any lifecycle mutation), you\n"
            "  MUST:\n"
            "  1. Retrieve the target component repo's deploy/docker-compose.yml\n"
            "     and count its services, volumes, healthchecks, and commands.\n"
            "  2. Check the chat_agent_deployable_components allowlist (via\n"
            "     component_request to central-deploy or the roster) — if the\n"
            "     component is not listed, refuse to proceed and report the\n"
            "     missing allowlist entry; never attempt to deploy a component\n"
            "     that is not explicitly authorised for chat-agent deployment.\n"
            "  3. Compare the contract against the endpoint's known capabilities:\n"
            "     single-container endpoints cannot deploy multi-service compose\n"
            "     files, named volumes, multiple networks, or healthcheck\n"
            "     stanzas. If the endpoint cannot reproduce the full contract,\n"
            "     refuse to proceed and explain which contract elements are\n"
            "     unsupported.\n"
            "  Do NOT offer to deploy through an endpoint whose capabilities you\n"
            "  have not verified — guessing causes failed deploys and wastes\n"
            "  operator time. If you cannot determine the endpoint's capabilities\n"
            "  (e.g. the server is running an older version whose deploy support\n"
            "  is unknown), state that limitation and ask the operator to verify\n"
            "  before proceeding.\n"
            "– Contract-version troubleshooting: When a user encounters a "
            '"missing or incorrect central-deploy-contract-version header" '
            "error during onboarding, diagnose concretely before suggesting "
            "a ticket: (a) check whether the component's deploy/docker-"
            'compose.yml has "# central-deploy-contract-version: N" as its '
            "very first line — if the header is missing, the fix is to add "
            "it (the version number is in the repo's own deploy/docker-"
            "compose.yml); walk the user through adding it. (b) If the header "
            "is present but central-deploy rejects it, check the component's "
            "recent PRs for a version bump — a recent merge may have changed "
            "the expected version. (c) If the correct version remains unclear "
            "after checking the repo, file a ticket on the component repo "
            "to clarify the expected contract version.\n"
            "– When multiple MRs are pending human approval, do not ask "
            "an open-ended 'which should I approve?' and do not dump every "
            "MR id without context. First assess which MRs are strictly "
            "needed for your active tickets versus incidental or optional. "
            "Present a categorized prompt that lets the operator filter in "
            "one reply — e.g. '14 MRs pending: 3 needed for active tickets "
            "(5f1c, 2a97, 54ea), 11 incidental. Approve the needed ones, "
            "all, or exclude specific MRs?' — then approve the selected "
            "group in bulk through the mill's merge endpoint.\n"
            "\n\n"
            "Efficiency:\n"
            "– If a required tool is missing, state it in one sentence and stop — "
            "do not explore alternatives, explain why, or narrate checking for it.\n"
            "– Answer in three sentences or fewer unless the user explicitly "
            "asks you to elaborate. Do NOT volunteer multi-row markdown tables, "
            "timeline/audit dumps, or recap lists — emit those formats ONLY when "
            "the user explicitly requests them (e.g. 'show me a table', 'give me "
            "the full audit'). Never repeat content already shown earlier in the "
            "same conversation.\n"
            "– All tools are already loaded and available for the entire "
            "session; there is no separate tool-loading step. Never narrate "
            "loading, preparing, or fetching tools (e.g. 'I'll load the "
            "tools…', 'Let me load the task management tool first') and never "
            "announce or run a 'capability check'. When you need a tool, call "
            "it directly; if it is unavailable you will learn that from the "
            "call result. Do not restate tool descriptions across turns.\n"
            "– System notices about service restarts are for your awareness "
            "only. If you must reference them (e.g. the user asks about "
            "background tasks), condense repeated identical notices into a "
            "single summary: 'The monitor for ticket 42e0 has been resumed "
            "X times after restarts.' Do not repeat or re-list verbatim "
            "every restart notice that appears in the conversation."
            "\n\n"
            "Verification:\n"
            "– When reporting the state of an external system (repository contents, "
            "deployment status, ticket resolution, configuration changes), always "
            "verify the current state through available tools rather than relying "
            "on memory alone. Memory is a fallible cache — the live system is the "
            "source of truth.\n"
            "– Cognee memory recall (the 'Relevant memory from earlier "
            "conversations' block prepended to each turn) is similarity-based "
            "and can be stale, incomplete, or fabricated. When a recalled-memory "
            "claim asserts a concrete fact about external state (queue sizes, "
            "ticket counts, deployment status, configuration values, etc.), "
            "cross-check it against the live API before acting on it. Never "
            "treat a recalled-memory assertion as authoritative — verify first, "
            "then act. If verification contradicts the recall, trust the live "
            "data and disregard the recalled claim.\n"
            "– When the user directly challenges a claim about external state, "
            "re-verify against the live system immediately. Never double down on "
            "a memory-based assertion when the user reports contradictory "
            "observable evidence (e.g. an empty repo where you claimed files "
            "exist, a stale container where you claimed a fix was deployed). "
            "Acknowledge the discrepancy, re-check, and report the verified "
            "current state — distrusting memory when it conflicts with live "
            "observation preserves trust.\n"
            "– When the user states a concrete fact (e.g. 'the secrets have been "
            "provided', 'the config is correct', 'that deployment already ran'), "
            "treat the user's statement as ground truth. Do not contradict it "
            "based on tool output, logs, or recollection — your evidence may be "
            "stale, from a different scope, or misinterpreted. Instead, raise a "
            "targeted clarification question to reconcile the discrepancy (e.g. "
            "'I see errors suggesting otherwise — could you confirm which "
            "environment or when the change was applied?'), then proceed with the "
            "user's account.\n"
            "– Prefer timestamped evidence over recollection: when available, "
            "cite the specific tool call result, commit SHA, or deployment "
            "timestamp that supports your claim, rather than asserting it from "
            "memory.\n"
            "– When filing a ticket that involves authorization or configuration "
            "changes (gate functions, permission checks, compose labels, deploy "
            "contracts), first read the relevant source files through available "
            "tools to verify current behavior. Include accurate context in the "
            "ticket spec — do not file based on assumptions about what the code "
            "does. A superficial change (docstring-only edit, label addition "
            "without logic change) does not fix a behavioral issue and wastes "
            "implement cycles.\n"
            "– Server-side capability probes: when checking whether a new "
            "server-side capability (e.g. a new HTTP endpoint like "
            "POST /chat/deploy) is available, probe the target server's "
            "endpoint directly with a GET request rather than relying on "
            "static skill descriptions, roster entries, or the audit log. "
            "A catch-all 303 redirect from an old build does NOT confirm "
            "the capability is present — only a meaningful status code "
            "(405 Method Not Allowed, 422 Unprocessable Entity, etc.) from "
            "the endpoint itself indicates the route exists. Before "
            "concluding a capability is live, check the server's running "
            "image digest (via the health endpoint or deploy status) "
            "against the expected digest from the merged PR that introduced "
            "the capability. Report the digest comparison to the user so "
            "they can independently confirm.\n"
            "\n"
            "Secret handling:\n"
            "– When a user proposes a task that will require a secret (credentials, "
            "password, token, API key, SSH/SFTP key, or any other privileged "
            "material), you must halt and direct them to the secure credential-"
            "registration channel BEFORE they paste the secret value. Ask them to "
            "register the credential via the vault / one-time-secret link or file "
            "a credential-registration ticket with a secure scope — never solicit "
            "the plaintext value in chat. "
            "(Rationale: plaintext secrets pasted into chat persist in conversation "
            "history and compaction artifacts and cannot be erased.)\n"
            "– If a secret value has already appeared in the conversation, do NOT "
            "echo, quote, or restate the plaintext secret in any of your responses "
            "— redact or reference it generically instead (e.g. 'the password you "
            "provided'). "
            "(Rationale: repeating the secret extends its lifetime in the "
            "transcript.)\n"
            "– When a secret has already been pasted as plaintext, warn the user "
            "that it is now exposed in conversation history, recommend rotating "
            "the exposed credential, and route registration through the secure "
            "channel — do not use the plaintext value to file the registration "
            "ticket. "
            "(Rationale: the exposed value is already compromised; re-using it "
            "propagates the exposure into the ticket's own history.)\n"
            "\n"
            "You are a conversational assistant. You have no ability to run shell "
            "commands, read or edit files on the host filesystem, or browse the web "
            "directly. You **can** access external systems and the network through "
            "the tools explicitly provided to you in this session — use them. "
            "If a request needs access "
            "you don't have, "
            "briefly say so and suggest an alternative; never narrate or pretend to "
            "perform actions you cannot take."
        ),
        json_schema_extra={"advanced": True},
    )
    server_host: str = Field(default="0.0.0.0", json_schema_extra={"advanced": True})  # noqa: S104  # nosec B104
    server_port: int = Field(default=8000, json_schema_extra={"advanced": True})
    idle_timeout_minutes: int = 30
    compaction_min_turns: int = Field(default=3, json_schema_extra={"advanced": True})
    log_level: str = "INFO"
    log_json_format: bool = True
    cors_allow_origins: list[str] = Field(
        default_factory=list, json_schema_extra={"advanced": True}
    )
    correlation_id_header: str = Field(
        default="X-Request-ID", json_schema_extra={"advanced": True}
    )
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    memory: MemorySettings = Field(
        default_factory=MemorySettings, json_schema_extra={"advanced": True}
    )
    central_deploy: CentralDeploySettings = Field(
        default_factory=CentralDeploySettings, json_schema_extra={"advanced": True}
    )
    mail: MailSettings = Field(
        default_factory=MailSettings, json_schema_extra={"advanced": True}
    )
    conversation: ConversationSettings = Field(
        default_factory=ConversationSettings, json_schema_extra={"advanced": True}
    )
    diagnostics: DiagnosticsSettings = Field(
        default_factory=DiagnosticsSettings, json_schema_extra={"advanced": True}
    )
    refdocs: RefDocsSettings = Field(
        default_factory=RefDocsSettings, json_schema_extra={"advanced": True}
    )
    render_url: RenderUrlSettings = Field(
        default_factory=RenderUrlSettings, json_schema_extra={"advanced": True}
    )
    knowledge: KnowledgeSettings = Field(default_factory=KnowledgeSettings)
    self_review: SelfReviewSettings = Field(
        default_factory=SelfReviewSettings, json_schema_extra={"advanced": True}
    )
    version_check: VersionCheckSettings = Field(
        default_factory=VersionCheckSettings, json_schema_extra={"advanced": True}
    )
    component_client: ComponentClientSettings = Field(
        default_factory=ComponentClientSettings,
        json_schema_extra={"advanced": True},
    )
    subsessions: SubsessionsSettings = Field(
        default_factory=SubsessionsSettings, json_schema_extra={"advanced": True}
    )
    direct_repo: DirectRepoSettings = Field(
        default_factory=DirectRepoSettings, json_schema_extra={"advanced": True}
    )
    github_security: GitHubSecuritySettings = Field(
        default_factory=GitHubSecuritySettings,
        json_schema_extra={"advanced": True},
    )
    github_actions: GitHubActionsSettings = Field(
        default_factory=GitHubActionsSettings, json_schema_extra={"advanced": True}
    )
    repo_study: RepoStudySettings = Field(
        default_factory=RepoStudySettings, json_schema_extra={"advanced": True}
    )
    lifecycle: LifecycleSettings = Field(
        default_factory=LifecycleSettings, json_schema_extra={"advanced": True}
    )
    notification: NotificationSettings = Field(
        default_factory=NotificationSettings, json_schema_extra={"advanced": True}
    )
    http_probe: HttpProbeSettings = Field(
        default_factory=HttpProbeSettings, json_schema_extra={"advanced": True}
    )
    feedback: FeedbackSettings = Field(
        default_factory=FeedbackSettings, json_schema_extra={"advanced": True}
    )
    autonomous: AutonomousSettings = Field(
        default_factory=AutonomousSettings, json_schema_extra={"advanced": True}
    )
    max_images_per_message: int = Field(default=8, json_schema_extra={"advanced": True})
    max_image_bytes: int = Field(
        default=5_242_880, json_schema_extra={"advanced": True}
    )
    allowed_image_media_types: list[str] = Field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/gif", "image/webp"],
        json_schema_extra={"advanced": True},
    )

    model_config = ConfigDict(extra="forbid")

    @staticmethod
    def _require_min(value: float | int, min_val: float | int, name: str) -> str | None:
        """Return an error string if *value* < *min_val*, or ``None``."""
        if value < min_val:
            return f"{name} must be >= {min_val}, got {value!r}"
        return None

    def model_post_init(self, __context: Any) -> None:
        """Validate fields that cannot be expressed via simple type annotations.

        All preconditions are checked so that every failure is reported
        at once — callers get a full list of what failed rather than
        stopping at the first error.
        """
        failures: list[str] = []

        if self.llmio_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"llmio.model_level must be one of {sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.llmio_model_level!r}"
            )
        # The keyless Claude SDK provider (level 3) needs no API key;
        # key-bearing providers (e.g. openrouter, levels 1-2) require one.
        if (
            level_needs_api_key(self.llmio_model_level)
            and not self.llmio_api_key.get_secret_value()
        ):
            failures.append(
                f"llmio.api_key must be set for model_level "
                f"{self.llmio_model_level} (its provider needs a key) — provide "
                "it via the `llmio.api_key` field of your config file "
                "(or use model_level 3, which is keyless)"
            )
        if self.summary_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"summary_model_level must be one of {sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.summary_model_level!r}"
            )
        # Unlike llmio_model_level, a missing key here is not fatal at config
        # load — create_agent_from_settings falls back to a keyless level
        # (see cli.py) so the default (level 1) never breaks a deployment
        # that has not configured an OpenRouter key.
        if self.memory.enabled:
            if not self.memory.llm.api_key.get_secret_value():
                failures.append(
                    "memory.llm.api_key must be set when memory is enabled — "
                    "provide it via the `memory.llm.api_key` "
                    "field of your config file"
                )
            if not self.memory.embedding.endpoint:
                failures.append(
                    "memory.embedding.endpoint must be set when memory is enabled "
                    "(e.g. http://host:11434/v1) — provide it via "
                    "the config file"
                )
        err = self._require_min(self.idle_timeout_minutes, 0, "idle_timeout_minutes")
        if err:
            failures.append(err)
        err = self._require_min(self.compaction_min_turns, 0, "compaction_min_turns")
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.max_concurrent, 1, "subsessions.max_concurrent"
        )
        if err:
            failures.append(err)
        err = self._require_min(self.subsessions.max_depth, 1, "subsessions.max_depth")
        if err:
            failures.append(err)
        if self.subsessions.default_model_level not in VALID_MODEL_LEVELS:
            failures.append(
                f"subsessions.default_model_level must be one of "
                f"{sorted(VALID_MODEL_LEVELS)}, "
                f"got {self.subsessions.default_model_level!r}"
            )
        err = self._require_min(
            self.subsessions.min_interval_seconds,
            1.0,
            "subsessions.min_interval_seconds",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.auto_stop_no_change_runs,
            1,
            "subsessions.auto_stop_no_change_runs",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.mill_recovery_initial_backoff_seconds,
            1.0,
            "subsessions.mill_recovery_initial_backoff_seconds",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.mill_recovery_max_backoff_seconds,
            1.0,
            "subsessions.mill_recovery_max_backoff_seconds",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.mill_recovery_max_retries,
            0,
            "subsessions.mill_recovery_max_retries",
        )
        if err:
            failures.append(err)
        err = self._require_min(
            self.subsessions.max_idle_runs,
            0,
            "subsessions.max_idle_runs",
        )
        if err:
            failures.append(err)
        # component_client has no required fields beyond `enabled` —
        # an empty components list just means no agents are reachable,
        # and the list_component_agents tool returns a helpful message.
        if self.refdocs.enabled and not self.refdocs.repos:
            failures.append(
                "refdocs.repos must be non-empty when refdocs is enabled — "
                "provide it via the `refdocs.repos` config field"
            )
        if self.version_check.enabled and not self.version_check.repo:
            failures.append(
                "version_check.repo is required when version_check.enabled is true — "
                "provide it via the `version_check.repo` config field"
            )
        if self.feedback.enabled and not self.feedback.board_url:
            failures.append(
                "feedback.board_url must be non-empty when feedback.enabled is "
                "true — provide it via the `feedback.board_url` config field"
            )

        if failures:
            raise ConfigValidationError(failures)

    # ------------------------------------------------------------------
    # Legacy config normalisation
    # ------------------------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_empty_strings(cls, data: Any) -> Any:
        """Coerce legacy ``""`` and JS-toString sentinels to proper containers.

        Older deployed configs used ``""`` for optional array/object
        fields that were never configured, and a browser-side serialisation
        bug in the Configure UI sometimes passes ``String(value)`` instead
        of ``JSON.stringify(value)``, yielding sentinels like
        ``"[object Object]"`` for objects.

        Normalize all of these here so validation passes on untouched or
        corrupted keys rather than failing with a type-mismatch error.
        """
        if not isinstance(data, dict):
            return data

        # Strings that indicate a JS/browser serialisation bug — an object
        # or array was passed through ``String()`` (or implicit
        # ``toString()``) instead of ``JSON.stringify``.
        _bad: frozenset[str] = frozenset({"[object Object]", "undefined", "null"})

        # Top-level list fields — tolerate "" and JS sentinels → []
        for key in ("cors_allow_origins", "allowed_image_media_types"):
            val = data.get(key)
            if val == "" or (isinstance(val, str) and val in _bad):
                data[key] = []

        # Top-level object fields — tolerate "" and JS sentinels → {}
        _object_keys = (
            "langfuse",
            "memory",
            "central_deploy",
            "mail",
            "conversation",
            "diagnostics",
            "refdocs",
            "render_url",
            "knowledge",
            "self_review",
            "version_check",
            "component_client",
            "subsessions",
            "direct_repo",
            "github_security",
            "github_actions",
            "repo_study",
            "lifecycle",
            "notification",
            "http_probe",
            "feedback",
            "autonomous",
        )
        for key in _object_keys:
            val = data.get(key)
            if val == "" or (isinstance(val, str) and val in _bad):
                data[key] = {}

        # Nested list fields inside object sub-models
        if isinstance(data.get("refdocs"), dict):
            rv = data["refdocs"].get("repos")
            if rv == "" or (isinstance(rv, str) and rv in _bad):
                data["refdocs"]["repos"] = []
        if isinstance(data.get("component_client"), dict):
            cv = data["component_client"].get("components")
            if cv == "" or (isinstance(cv, str) and cv in _bad):
                data["component_client"]["components"] = []

        # Nested object fields inside MemorySettings
        if isinstance(data.get("memory"), dict):
            for key in ("llm", "langfuse", "embedding"):
                mv = data["memory"].get(key)
                if mv == "" or (isinstance(mv, str) and mv in _bad):
                    data["memory"][key] = {}

        return data

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def load(cls) -> Settings:
        """Load from the JSON file located by ``ROBOTSIX_CONFIG_FILE``."""
        return load_config(cls)
