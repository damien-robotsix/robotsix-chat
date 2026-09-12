"""Shared utilities for the robotsix-chat server.

Currently provides :mod:`robotsix_chat.common.http` — a safe HTTP request
helper that consolidates error handling across the board-reader, refdocs,
and version-check HTTP clients — and :mod:`robotsix_chat.common.github_app_token`,
a shared adapter over robotsix-github-auth's ``mint_installation_token``
used by the refdocs, version-check, and repo-study clients.
"""
