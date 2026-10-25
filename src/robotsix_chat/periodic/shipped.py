"""Boot-time drift check between the SHIPPED presets and the LIVE config.

The committed ``config/config.json`` is only a *template*: the running chat
reads its periodic presets from the deployed config volume, and the template is
not even copied into the container image. A pull request that edits a preset's
``initial_prompt`` in the template therefore changes nothing live unless the
same preset name exists in the live config and the operator (or deploy) applies
the edit there — the 2026-10-22 incident, where a ``dependabot-drain`` template
edit merged green while the live ``repo-hygiene`` preset kept the old behaviour.

This module is the shipped, code-side mirror of the template presets: preset
name → SHA256 of its ``initial_prompt``. At boot :func:`check_shipped_presets`
compares it with the live ``periodic.sessions`` and surfaces (as warnings)
every shipped preset that is absent from the live config or whose live prompt
differs from the shipped one, so a template-only preset change is never
silently treated as deployed.

``tests/periodic/test_shipped_drift.py`` asserts this mapping equals the
presets in ``config/config.json`` — editing a template preset prompt fails CI
until the new hash is mirrored here, which is the moment to check that the
preset actually exists in the live config (``GET /config``).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping

from robotsix_chat.config.periodic_models import PeriodicSessionDefinition

logger = logging.getLogger(__name__)

# Mirror of ``periodic.sessions[]`` in config/config.json: name -> sha256 of
# the preset's ``initial_prompt``.
SHIPPED_PRESET_PROMPT_SHA256S: dict[str, str] = {
    "dependabot-drain": (
        "b0d2e4f3b80b0f366a1cbb9dc5dd4883eb38989c7949a135ce3736c4ffd4f567"
    ),
    "gate-drain": ("3b6d7c1c21f0eba13abb307db469b840021d92330f89963106a40684f90a07b5"),
    "release-gate": (
        "70654e1a78132a5dab0b4655b3c8db11db240be747af0305caa31ec42294667d"
    ),
}


def prompt_sha256(prompt: str) -> str:
    """Return the SHA256 hex digest used to fingerprint a preset prompt."""
    return hashlib.sha256(prompt.encode()).hexdigest()


def check_shipped_presets(
    live: Iterable[PeriodicSessionDefinition],
    shipped: Mapping[str, str] = SHIPPED_PRESET_PROMPT_SHA256S,
) -> list[str]:
    """Surface shipped presets whose template prompt does not reach *live*.

    Returns one human-readable finding per shipped preset that is either
    absent from the live config (a template edit to it is a no-op in
    production) or present with a different ``initial_prompt`` (the template
    change was not applied live, or the operator customised the prompt — the
    live value is kept either way). Each finding is also logged as a warning.
    An empty list means every shipped preset is live with the shipped prompt.
    """
    live_by_name = {d.name: d for d in live}
    findings: list[str] = []
    for name, shipped_sha in sorted(shipped.items()):
        definition = live_by_name.get(name)
        if definition is None:
            findings.append(
                f"shipped periodic preset {name!r} is absent from the live "
                "config: edits to it in the config/config.json template do "
                "NOT reach the running chat — add or apply it to the live "
                "config (PUT /config) if it should run"
            )
            continue
        live_sha = prompt_sha256(definition.initial_prompt)
        if live_sha != shipped_sha:
            findings.append(
                f"live periodic preset {name!r} initial_prompt (sha256 "
                f"{live_sha[:8]}…) differs from the shipped template "
                f"(sha256 {shipped_sha[:8]}…): the shipped change was not "
                "applied live or the prompt was operator-edited; the live "
                "prompt is kept"
            )
    for finding in findings:
        logger.warning("Periodic preset drift: %s", finding)
    return findings
