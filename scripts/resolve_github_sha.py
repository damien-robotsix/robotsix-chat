#!/usr/bin/env python3
"""Resolve a GitHub repo + ref to a 40-char commit SHA via ``git ls-remote``.

Usage::

    uv run scripts/resolve_github_sha.py <owner/repo> <ref>

Examples::

    uv run scripts/resolve_github_sha.py damien-robotsix/robotsix-github-workflows main
    uv run scripts/resolve_github_sha.py actions/checkout v4.2.2
"""

from __future__ import annotations

import shutil
import subprocess
import sys

_GIT_PATH: str | None = shutil.which("git")
if not _GIT_PATH:
    print("Error: git executable not found on PATH", file=sys.stderr)
    sys.exit(1)
_GIT: str = _GIT_PATH  # narrowed after the early-exit guard above


def _run_git(url: str, refspecs: list[str]) -> str | None:
    """Run ``git ls-remote`` and return the first SHA, or *None*."""
    result = subprocess.run(  # noqa: S603
        [_GIT, "ls-remote", url, *refspecs],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout
    if not stdout or not stdout.strip():
        return None
    return stdout.strip().split()[0]


def _parse_tag_output(stdout: str) -> str:
    """Extract the peeled commit SHA from ``git ls-remote`` tag output."""
    peeled_sha = ""
    plain_sha = ""
    for line in stdout.strip().split("\n"):
        sha, _, name = line.partition("\t")
        if name.endswith("^{}"):
            peeled_sha = sha
        else:
            plain_sha = sha
    return peeled_sha or plain_sha


def resolve_sha(owner: str, repo: str, ref: str) -> str:
    """Return the 40-char commit SHA for *owner*/*repo* at *ref*.

    Tries *ref* as a tag first (peeled to the underlying commit for
    annotated tags), then as a branch, then as a bare ref.
    """
    url = f"https://github.com/{owner}/{repo}.git"

    # 1. Try as a tag (peeled-tag protocol — prefer the dereferenced commit).
    result = subprocess.run(  # noqa: S603
        [
            _GIT,
            "ls-remote",
            url,
            f"refs/tags/{ref}^{{}}",
            f"refs/tags/{ref}",
        ],
        capture_output=True,
        text=True,
    )
    stdout = result.stdout
    if result.returncode == 0 and stdout and stdout.strip():
        tag_sha = _parse_tag_output(stdout)
        if len(tag_sha) == 40:
            return tag_sha

    # 2. Fall back to branch.
    branch_sha = _run_git(url, [f"refs/heads/{ref}"])
    if branch_sha is not None and len(branch_sha) == 40:
        return branch_sha

    # 3. Last resort: bare ref (some repos use non-standard ref names).
    bare_sha = _run_git(url, [ref])
    if bare_sha is not None and len(bare_sha) == 40:
        return bare_sha

    raise RuntimeError(
        f"Could not resolve {owner}/{repo} @ {ref!r} — "
        "no matching tag, branch, or ref found"
    )


def main() -> None:
    """CLI entry point."""
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <owner/repo> <ref>", file=sys.stderr)
        sys.exit(2)

    repo_spec = sys.argv[1]
    ref = sys.argv[2]

    if "/" not in repo_spec:
        print(
            f"Error: repo must be in owner/repo format, got {repo_spec!r}",
            file=sys.stderr,
        )
        sys.exit(2)

    owner, _, repo = repo_spec.partition("/")

    try:
        sha = resolve_sha(owner, repo, ref)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(sha)


if __name__ == "__main__":
    main()
