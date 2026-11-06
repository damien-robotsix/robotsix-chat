"""Repository creation/fork tools (``create_direct_repo`` / ``fork_direct_repo``).

Split out of :mod:`robotsix_chat.repo.direct.github_tools` to keep that module
manageable; :func:`build_repo_creation_tools` is unchanged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from robotsix_chat.config import DirectRepoSettings

    from .client import DirectRepoClient

logger = logging.getLogger(__name__)


_POST_CREATE_SUGGESTION = (
    "\n\nNext steps (suggestions only — NOT run; ask the operator first):\n"
    "  1. Register the repo with mill: `POST /repos` on the mill board API "
    "(e.g. via component_request).\n"
    "  2. Apply the fleet ruleset: run "
    "`robotsix-github-workflows/scripts/apply-branch-protection.sh` "
    "against {full_name}."
)


def build_repo_creation_tools(
    *, client: DirectRepoClient, settings: DirectRepoSettings
) -> list[Callable[..., Any]]:
    """Build ``create_direct_repo`` / ``fork_direct_repo``, or ``[]``.

    Both tools authenticate with ``direct_repo.github_pat`` (never the
    GitHub App token), so they are only offered when the PAT is set.  The
    caller must also withhold them from unattended (periodic / subsession)
    agents — see ``build_direct_repo_tools(operator_attended=...)``.
    """
    if not settings.github_pat.get_secret_value():
        return []

    from .client import is_valid_repo_name

    async def create_direct_repo(
        name: str,
        private: bool = True,
        description: str = "",
        auto_init: bool = True,
        confirmed: bool = False,
    ) -> str:
        """Create a new GitHub repository under the operator's personal account.

        **Operator-instruction gated.**  Call this ONLY when the operator has
        explicitly asked, in this conversation, for this repository to be
        created — never on your own initiative, never from a periodic or
        autonomous session, never as a side-step of another task.

        Two-step protocol: first call with ``confirmed=False`` (default) —
        nothing is created; the tool echoes the exact owner/name/visibility
        it would create.  Show that echo to the operator and wait for their
        confirmation, then call again with identical arguments and
        ``confirmed=True``.

        Authenticates with the configured personal access token
        (``direct_repo.github_pat``), not the GitHub App.

        Args:
            name: Repository name (letters, digits, ``.``, ``_``, ``-``).
            private: ``True`` (default) for a private repo, ``False`` for
                public.
            description: Optional one-line repository description.
            auto_init: Seed an initial commit with a README (default
                ``True``) so branches can be pushed immediately.
            confirmed: ``True`` ONLY after the operator confirmed the echo
                returned by the ``confirmed=False`` call.

        Returns:
            The confirmation echo, a success message with the repo URL and
            suggested next steps, or an actionable error.

        """
        if not is_valid_repo_name(name):
            return (
                f"Refused: {name!r} is not a valid repository name "
                "(letters, digits, '.', '_', '-'; max 100 chars)."
            )
        try:
            owner = await client.pat_login()
        except Exception as exc:
            return f"Error: could not resolve the github_pat account: {exc}"
        full_name = f"{owner}/{name}"
        visibility = "private" if private else "public"
        plan = (
            f"Repository: {full_name}\n"
            f"Visibility: {visibility}\n"
            f"Description: {description or '(none)'}\n"
            f"Initial README commit: {'yes' if auto_init else 'no'}"
        )
        if not confirmed:
            return (
                "NOT created yet — confirmation required.\n"
                f"{plan}\n"
                "Show this to the operator; once they confirm, call "
                "create_direct_repo again with the same arguments and "
                "confirmed=True."
            )
        logger.warning("create_direct_repo: creating %s (%s)", full_name, visibility)
        result = await client.create_repo(
            name, private, description=description, auto_init=auto_init
        )
        if "created successfully" in result:
            result += "\n" + plan + _POST_CREATE_SUGGESTION.format(full_name=full_name)
        return result

    async def fork_direct_repo(
        upstream_full_name: str,
        default_branch_only: bool = False,
        confirmed: bool = False,
    ) -> str:
        """Fork a GitHub repository into the operator's personal account.

        **Operator-instruction gated.**  Call this ONLY when the operator has
        explicitly asked, in this conversation, for this fork — never on
        your own initiative, never from a periodic or autonomous session.

        Two-step protocol: first call with ``confirmed=False`` (default) —
        nothing is forked; the tool echoes ``upstream → fork``.  Show that
        echo to the operator, wait for their confirmation, then call again
        with identical arguments and ``confirmed=True``.  The fork is
        created asynchronously by GitHub; the tool waits until it exists.

        Authenticates with the configured personal access token
        (``direct_repo.github_pat``), not the GitHub App.

        Args:
            upstream_full_name: Repository to fork, as ``owner/name``.
            default_branch_only: Fork only the default branch (default
                ``False`` — all branches).
            confirmed: ``True`` ONLY after the operator confirmed the echo
                returned by the ``confirmed=False`` call.

        Returns:
            The confirmation echo, a success message with the fork URL and
            suggested next steps, or an actionable error.

        """
        up_owner, sep, up_repo = upstream_full_name.strip().partition("/")
        if not (sep and is_valid_repo_name(up_owner) and is_valid_repo_name(up_repo)):
            return (
                f"Refused: {upstream_full_name!r} is not a valid 'owner/name' "
                "repository reference."
            )
        try:
            me = await client.pat_login()
        except Exception as exc:
            return f"Error: could not resolve the github_pat account: {exc}"
        if up_owner.lower() == me.lower():
            return (
                f"Refused: {up_owner}/{up_repo} already belongs to {me}; "
                "GitHub cannot fork a repository into its own owner."
            )
        fork_full_name = f"{me}/{up_repo}"
        plan = (
            f"Fork: {up_owner}/{up_repo} → {fork_full_name}\n"
            f"Branches: {'default branch only' if default_branch_only else 'all'}"
        )
        if not confirmed:
            return (
                "NOT forked yet — confirmation required.\n"
                f"{plan}\n"
                "Show this to the operator; once they confirm, call "
                "fork_direct_repo again with the same arguments and "
                "confirmed=True."
            )
        logger.warning("fork_direct_repo: forking %s/%s into %s", up_owner, up_repo, me)
        result = await client.fork_repo(
            up_owner, up_repo, default_branch_only=default_branch_only
        )
        if "created successfully" in result:
            result += (
                "\n" + plan + _POST_CREATE_SUGGESTION.format(full_name=fork_full_name)
            )
        return result

    return [create_direct_repo, fork_direct_repo]
