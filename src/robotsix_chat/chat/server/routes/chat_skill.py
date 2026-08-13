"""``GET /chat-skill`` — SKILL.md endpoint for the robotsix-chat agent.

Chat is the consumer of every other component's chat-skill document; this
serves its own, so it appears in the central-deploy roster alongside them
and can address itself through ``component_request``.

Without a skill body the roster builder drops a component, which left the
agent with no route to its own config API: the deploy plane's
``PUT /chat/config/{name}`` was the only path, and that one rebuilt the
document from a stored schema template rather than from what chat owns.

The text is versioned with the app so it stays in sync with the routes it
describes.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import PlainTextResponse

_CHAT_SKILL_TEXT = """\
---
name: robotsix-chat-self
description: Read and update this chat agent's own configuration, with
  version history and rollback.
---

## robotsix-chat-self — Chat Agent Skill

You are connected to **your own** component API. Use it to inspect and
change your own configuration.

Your config file is the single source of truth for your settings. You own
it: these endpoints validate every write against your live `Settings`
model, so a change that would not load is rejected before it is persisted
rather than after a restart.

**You *can* write your own config.** The allowed write path is your own
`PUT /config` endpoint, documented below. The guard that follows only
rules out routing those writes through the deploy plane's
template-derived endpoints — it is not a prohibition on self-configuration.

The `set_component_config` tool configures **other** component agents in
your `component_client.components` allowlist, not yourself — never use it
for your own config; use `PUT /config` here instead.

Do **not** route config writes through the deploy plane. Endpoints under
`/chat/config/{name}` on the deploy component rebuild the whole document
from a stored copy of your schema, which silently drops keys that copy
does not know about and reinstates keys you have removed.

### Base URL

The same host and port that served this document.

---

## GET /config — read the current config

Returns the on-disk config with secrets masked, plus two extra top-level
keys:

- `version` — the current version number (integer, starts at 1).
- `schema` — the JSON Schema your settings are validated against. Consult
  it to find the exact key path for a setting; nesting matters.

Secret values come back as `"***"` when set and `""` when unset.

---

## PUT /config — update the config

```
PUT /config
{"autonomous": {"sessions": [{"name": "nightly", "max_auto_turns": 40}]}}
```

Semantics worth knowing before you call it:

- **Deep merge.** The payload is merged over the existing config; keys you
  omit keep their current values. Send only what you are changing — you do
  not need to (and should not) echo back a full document.
- **Secrets are preserved.** A masked (`"***"`) or blank value for a secret
  keeps the stored secret. Only a real, non-empty value overwrites it.
- **Validated as a whole.** The merged result is checked against your
  `Settings` model. On failure you get `422` with a `failures` list naming
  each precondition that did not hold, and nothing is written.
- **Versioned.** A successful write appends a new version entry and returns
  the new `version`.

A `422` mentioning `extra_forbidden` means the merged config carries a key
your model no longer defines — usually a field removed by an upgrade, or
one written in the wrong place. Fix the key path (check `schema` from
`GET /config`); the offending key is named in the error.

Changes that only take effect at startup need a restart to become live.
Restart yourself with `POST /chat/services/{your-component-id}/restart` on
the deploy component.

---

## GET /config/versions — list version history

Returns the append-only history: `version`, `timestamp`, and the
`changed_keys` for each entry.

---

## POST /config/rollback — revert to an earlier version

```
POST /config/rollback
{"version": 3}
```

Reverts the on-disk config to that version's data. History is append-only:
the rollback itself becomes a **new** version, so nothing is ever lost and
a rollback can itself be rolled back.
"""


async def chat_skill_endpoint(request: Request) -> PlainTextResponse:  # noqa: ARG001
    """Serve the SKILL.md document describing this component's own API.

    *request* is unused; Starlette requires it in the endpoint signature.
    """
    return PlainTextResponse(_CHAT_SKILL_TEXT)
