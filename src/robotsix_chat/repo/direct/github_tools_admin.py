"""Repository settings and admin tools for :func:`build_github_tools`, split out.

The tool closures below were extracted verbatim from
:func:`robotsix_chat.repo.direct.github_tools.build_github_tools` to keep that
factory manageable.  ``build_github_tools`` calls
:func:`build_github_tools_admin` and splices the returned tools back into its
result list at their original positions, so the public tool set (names,
count, order) is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .board_client import BoardClient
    from .client import DirectRepoClient

logger = logging.getLogger(__name__)


def build_github_tools_admin(
    *,
    client: DirectRepoClient,
    board: BoardClient,
    component_request: Callable[..., Any] | None,
    assert_in_scope: Callable[..., Awaitable[str | None]],
) -> list[Callable[..., Any]]:
    """Build and return the 5 repository settings and admin tools.

    Returned in definition order; ``build_github_tools`` unpacks them
    positionally.  The closures capture only this function's parameters.
    """

    async def enable_repo_pages(
        repo_full_name: str,
        build_type: str = "workflow",
    ) -> str:
        """Enable GitHub Pages built from a workflow on a repository.

        Enables Pages via ``POST /repos/{owner}/{repo}/pages`` with
        ``build_type: workflow`` and reads the resulting site status back.
        Idempotent — a repo that already has Pages returns an
        already-enabled result instead of an error.

        **This is a confirmation-gated mutation.**  This changes live
        repository settings.  Before calling this tool you MUST obtain
        explicit operator approval in the conversation (the same class of
        mutation as ``set_repo_security_and_analysis``).  State the exact
        repository and wait for the operator to confirm before proceeding.

        **No BLOCKED-state requirement.** This is a confirmation-gated
        tool — it does not require a ticket to be in BLOCKED state.

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).
            build_type: Pages build type — ``"workflow"`` (default, built
                via GitHub Actions) or ``"legacy"``.

        Returns:
            A message reporting the enable result and the resulting Pages
            site status, or an actionable error (e.g. permission denied).

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error
        return await client.enable_pages(
            repo_full_name=repo_full_name,
            build_type=build_type,
        )

    async def get_repo_settings(repo_full_name: str) -> str:
        """Read a repository's branch/merge-hygiene settings.

        Calls ``GET /repos/{owner}/{repo}`` and reports
        ``delete_branch_on_merge``, ``default_branch``, ``allow_auto_merge``
        and the other merge-strategy flags.  Read-only.

        **No BLOCKED-state requirement.**

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g.
                ``"robotsix/robotsix-chat"``).

        Returns:
            One line per setting (``- key: value``), or an error string.

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error
        return await client.get_repo_settings(repo_full_name)

    async def set_repo_delete_branch_on_merge(
        repo_full_name: str,
        enabled: bool = True,
    ) -> str:
        """Toggle ``delete_branch_on_merge`` on a repository.

        Sends ``PATCH /repos/{owner}/{repo}`` with a body containing ONLY
        ``delete_branch_on_merge`` — no other repository setting can be
        changed through this tool.  Idempotent.  Requires the GitHub App
        permission ``Administration: write``.

        **Repository-settings mutation.**  Call it when the operator asked
        for it in the conversation or when a periodic preset written by the
        operator (e.g. ``repo-hygiene`` PART 2) instructs it — that preset
        is the operator's standing consent.  Read the current value first
        with ``get_repo_settings`` and only call this when it differs.

        **No BLOCKED-state requirement.**

        **Scope:** *repo_full_name* must be within the robotsix-mill GitHub
        App's current installation scope (checked dynamically at call time).

        Args:
            repo_full_name: GitHub ``owner/name``.
            enabled: Target value (default ``True``).

        Returns:
            A confirmation message, or an actionable error (e.g. missing
            ``administration: write`` permission).

        """
        if error := await assert_in_scope(client, repo_full_name):
            return error
        return await client.set_delete_branch_on_merge(
            repo_full_name=repo_full_name,
            enabled=enabled,
        )

    async def reset_implement_spawn_counter(ticket_id: str) -> str:
        """Reset the implement-agent spawn counter for a blocked ticket.

        Sends ``POST /tickets/{ticket_id}/resume-blocked`` to the board API
        with a spawn-counter justification, clearing the spawn-limit block
        so the implement agent can be re-spawned against this ticket.

        Tries the component roster path first when *component_request*
        is available (resolving ``"mill"`` via the central-deploy roster
        or component fallbacks); on failure falls back to the direct
        ``board_api_base_url`` path.  When *component_request* is
        unavailable the direct path is used directly.

        On board builds that do not expose the ``implement_spawn_count``
        artifact DELETE route the call returns **HTTP 405 / an error**.
        Do not retry the reset — use the ``resume-blocked`` mechanism
        (``POST /tickets/{id}/resume-blocked``) as the standard fallback.

        Args:
            ticket_id: The blocked ticket whose counter to reset
                (e.g. ``"20250624T020652Z-my-ticket-a1b2"``).

        Returns:
            A status message — success confirmation, or a
            ``MANUAL INTERVENTION REQUIRED`` error describing the board
            API failure, the ticket's current visibility on the board, and
            the next step for a human operator.

        """
        # Resolve paraphrased / abbreviated IDs before making the request.
        resolved_map = await board.resolve_ticket_ids([ticket_id])
        effective_id = resolved_map.get(ticket_id) or ticket_id

        justification = (
            "Spawn counter reset — allowing re-implement after spawn limit reached."
        )

        # Try roster path first when available.
        if component_request is not None:
            resp = await component_request(
                "mill",
                "POST",
                f"/tickets/{effective_id}/resume-blocked",
                json_body={"justification": justification},
            )
            if resp.startswith("HTTP 2"):
                return (
                    f"Implement spawn counter reset for ticket {effective_id} "
                    "(via roster path). The ticket can now be re-spawned."
                )
            logger.info(
                "reset_implement_spawn_counter roster path failed for %s; "
                "falling back to direct board API",
                effective_id,
            )

        # Fall back to the direct board API path.
        ok, reason = await board.resume_blocked_ticket(effective_id, justification)
        if ok:
            return (
                f"Implement spawn counter reset for ticket {effective_id}. "
                "The ticket can now be re-spawned."
            )

        board_url = board._board_url
        # Even when resume fails, confirm whether the ticket is still visible
        # on the board so the operator knows if this is a connectivity/API
        # failure or a ticket that has vanished from the board.
        state = await board.get_ticket_state(effective_id)
        if state:
            visibility = (
                f"Ticket {effective_id} is still visible on the board "
                f"(current state: {state})."
            )
        else:
            visibility = (
                f"Ticket {effective_id} could not be located on the board — "
                "it may have been deleted or the board API is unreachable."
            )
        return (
            f"Error: could not reset implement spawn counter for ticket "
            f"{effective_id}.\n"
            "MANUAL INTERVENTION REQUIRED: resume-blocked failed with "
            f"{reason or 'unknown board API error'}.\n"
            f"{visibility}\n"
            f"Board URL: {board_url}. A human should check the ticket on the "
            "board and resume it manually."
        )

    async def inspect_github_installation_token(
        repo_full_name: str,
    ) -> str:
        """Inspect the GitHub App installation token's expiry and permission scope.

        Mints a **fresh** installation token for *repo_full_name* (bypassing any
        cached token) so the returned permission map reflects the App's current
        grant — not a possibly-stale cached token.  Use this to distinguish
        "the token was cached/stale" from "the App genuinely lacks a
        permission" when a GitHub API call fails with a 403 permission error
        such as ``lacks pages: write``.

        **Read-only.** Does not modify any repository state and does not
        require a ticket to be in BLOCKED state.

        Args:
            repo_full_name: GitHub ``owner/name`` (e.g. ``"robotsix/robotsix-chat"``).

        Returns:
            A report with the App id, configured and resolved installation ids,
            token expiry timestamp, seconds remaining, and the token's
            effective permission map.  A mismatch between the configured and
            resolved installation ids is called out explicitly.

        """
        try:
            details = await client.get_installation_token_diagnostics(repo_full_name)
        except Exception as exc:
            return f"Error inspecting installation token for {repo_full_name}: {exc}"

        permissions: dict[str, str] = details["permissions"]
        configured_id = details["configured_installation_id"]
        resolved_id = details["resolved_installation_id"]
        mode = details.get("installation_mode", "per_repository")
        configured_exists = details.get("configured_installation_exists")
        mode_label = (
            "per-repository resolution (no fixed installation id — the "
            "installation is resolved from each repo, so a re-install needs "
            "no config change)"
            if mode == "per_repository"
            else "override (a fixed `github_app_installation_id` is pinned)"
        )
        configured_display = f"`{configured_id}`" if configured_id else "`` (empty)"
        lines = [
            f"GitHub App installation token diagnostic for `{repo_full_name}`:",
            f"- App id: `{details['app_id']}`",
            f"- Installation mode: {mode_label}",
            f"- Configured installation id: {configured_display}",
            f"- Resolved installation id (for `{repo_full_name}`): `{resolved_id}`",
            f"- Token expires at: `{details['expires_at']}` (UTC)",
            f"- Seconds remaining: {details['seconds_remaining']}",
        ]
        if configured_id and mode == "override":
            if configured_exists is False:
                lines.append(
                    "  ⚠️ The pinned installation id no longer exists on GitHub "
                    "(minting returned HTTP 404). Clear "
                    "`github_app_installation_id` to resolve per repository, or "
                    "update it to the current installation id."
                )
            elif configured_exists is True:
                lines.append("  ✓ The pinned installation id still exists.")
            else:
                lines.append(
                    "  (could not confirm whether the pinned installation "
                    "still exists.)"
                )
        elif configured_id and mode == "per_repository":
            lines.append(
                "  ⚠️ The pinned installation id returned HTTP 404 earlier this "
                "process and has been abandoned — tokens are now resolved per "
                "repository. Clear `github_app_installation_id` from config."
            )
        if resolved_id != configured_id:
            lines.append(
                "  ⚠️ Mismatch: the resolved installation id differs from the "
                "configured id — this repo is installed under a different "
                "installation of the App than the one in config. The permission "
                "map below reflects the resolved installation."
            )
        lines.append("- Permissions (effective scope):")
        if not permissions:
            lines.append(
                "  - (none returned — the installation may have no permissions granted)"
            )
        else:
            for name in sorted(permissions):
                lines.append(f"  - `{name}`: `{permissions[name]}`")
        return "\n".join(lines)

    return [
        enable_repo_pages,
        get_repo_settings,
        set_repo_delete_branch_on_merge,
        reset_implement_spawn_counter,
        inspect_github_installation_token,
    ]
