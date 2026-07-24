# Settings UI

The chat UI includes a settings panel that lets operators view and edit the server's runtime
configuration through a graphical interface, without needing direct filesystem access to the config
volume.

Use the **⚙** (gear) button in the header bar to open the panel. The panel can be resized by
dragging its left edge.

## How it works

The settings panel is backed by HTTP endpoints on the chat server:

- **`GET /config`** — returns the current on-disk config with secret field values masked as
  `"**********"`, plus the current version number and the JSON Schema for the `Settings` model.
- **`PUT /config`** — accepts a JSON object with the fields to change, **deep-merges** it over the
  existing on-disk config, validates the result through the `Settings` pydantic model, increments
  the version, and only persists if validation passes.
- **`GET /config/versions`** — returns the version history (newest first), showing what changed in
  each version.
- **`POST /config/rollback`** — reverts to a previous version and creates a new version entry
  (history is append-only, never destructive).

### Deep-merge (non-destructive save)

The most important design property: **the save handler never blanks a field the UI doesn't render.**

When the operator submits the form, the server takes the submitted JSON object and recursively
merges it into the existing config file — it does **not** replace the entire file. Any key that is
absent from the submitted payload is preserved unchanged from the on-disk file.

For example, if the on-disk config contains:

```json
{
  "server_port": 8000,
  "memory": {
    "enabled": true,
    "embedding": {
      "endpoint": "http://box:11434/v1",
      "dimensions": 1024
    }
  }
}
```

And the operator submits only `{"server_port": 9000}`, the persisted result is:

```json
{
  "server_port": 9000,
  "memory": {
    "enabled": true,
    "embedding": {
      "endpoint": "http://box:11434/v1",
      "dimensions": 1024
    }
  }
}
```

This prevents the class of bugs where saving a partial form blanks unrendered nested fields like
`memory.embedding.endpoint`, which previously caused the server to crash-loop on restart.

### Validation before persist

Before writing the merged config to disk, the server constructs a `Settings` instance from it (the
same pydantic model used at startup). If validation fails — for example, `memory.embedding.endpoint`
is empty while `memory.enabled` is `true` — the save is **rejected** with HTTP 422 and a
[RFC 9457](https://www.rfc-editor.org/rfc/rfc9457) `application/problem+json` response is returned
to the UI. The on-disk config file and the running server are **untouched**.

### Secret round-tripping

When the UI receives a secret field (e.g. `llmio_api_key`), the `GET /config` response masks it as
`"**********"`. The form renders these fields as password inputs with the placeholder "Leave
unchanged to keep current secret".

When the operator saves, the UI sends `"**********"` back for any secret the operator did not
modify. The server detects this sentinel and preserves the original on-disk value. An empty string
(`""`) submitted for a secret field is also treated as "unchanged" and preserves the original value.
If the operator types a new value into a secret field, that new value is persisted.

### Versioning and rollback

Every successful `PUT /config` or `POST /config/rollback` increments a monotonic version counter and
appends a new entry to an append-only version history file (a JSONL file alongside the config file).
Each version entry records:

- The **version number** (monotonically increasing integer).
- The **timestamp** of the change (UTC ISO-8601).
- The **changed keys** (top-level keys that differ from the previous version).
- The **full config data** at that snapshot.

The version history is never rewritten or pruned — it provides a full audit trail of every config
change. The `GET /config` response includes the current `version` number so operators can confirm
their changes took effect. The UI displays the saved version number (e.g. "Saved (v3)").

**Rollback** (`POST /config/rollback`) reverts the on-disk config to a previous version's data and
creates a **new** version entry recording the rollback — the original version history is never
modified or deleted. Rollback validates the target data against the current `Settings` schema before
persisting, so rolling back to a version saved under an older schema will fail with 422 if the old
data no longer validates.

## Operator workflow

1. Click the **⚙ Settings** button in the header bar.
2. Browse the config sections — nested objects are shown as collapsible sections.
3. Edit the fields you want to change:
   - **Text fields** — free-form string input.
   - **Number fields** — numeric input (step "1" for integers, "any" for floats).
   - **Boolean fields** — checkbox.
   - **Array fields** — text area containing JSON; edit the JSON directly.
   - **Secret fields** — password input; value shows as `"**********"`; leave unchanged to keep the
     current secret.
4. Click **Save**.
   - **Success** — the panel shows "Saved (vN)" (where N is the new version number) and reloads
     the form from the server.
   - **Validation failure** — the panel displays the pydantic validation error and highlights
     affected fields with a red border. You must fix the error before the save is accepted.
   - **Server error** — the panel shows the error message.
5. Close the panel with the **×** button, the **Escape** key, or by clicking the gear button again.

The panel remembers its open/closed state in `localStorage` across page reloads.

## Endpoint details

### `GET /config`

Returns the current on-disk config with secrets masked as `"**********"`, plus the current version
number and the JSON Schema for the `Settings` model.

**Response** `200 OK`:

```json
{
  "version": 3,
  "schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
      ...
    },
    "$defs": {
      ...
    }
  },
  "server_port": 8000,
  "llmio_api_key": "**********",
  "memory": {
    "enabled": true,
    "embedding": {
      "endpoint": "http://box:11434/v1"
    }
  }
}
```

When no config file exists yet, returns version 1 with an empty config object and the schema.

### `PUT /config`

Deep-merges the submitted JSON over the existing config, validates, increments the version, and
persists.

**Request body** (JSON object with fields to update):

```json
{
  "server_port": 9000,
  "memory": {
    "enabled": false
  }
}
```

**Response** `200 OK`:

```json
{
  "version": 4,
  "status": "ok"
}
```

**Response** `422 Unprocessable Entity` (validation failed) — RFC 9457 `application/problem+json`:

```json
{
  "type": "about:blank",
  "title": "Config Validation Failed",
  "status": 422,
  "detail": "1 validation error for Settings\nmemory.embedding.endpoint\n  must be set when memory is enabled [type=...]"
}
```

**Response** `500 Internal Server Error` (I/O failure):

```json
{
  "error": "failed to write config: ..."
}
```

### `GET /config/versions`

Returns the version history (newest first). The full config data payload is excluded from this
response to keep it compact.

**Response** `200 OK`:

```json
[
  {
    "version": 3,
    "timestamp": "2026-07-23T23:52:15.123456+00:00",
    "changed_keys": ["server_port"]
  },
  {
    "version": 2,
    "timestamp": "2026-07-23T23:50:00.000000+00:00",
    "changed_keys": ["memory"]
  },
  {
    "version": 1,
    "timestamp": "2026-07-23T23:48:00.000000+00:00",
    "changed_keys": ["initial"]
  }
]
```

If no version history exists yet, it is automatically bootstrapped from the current on-disk config
(version 1).

### `POST /config/rollback`

Reverts the on-disk config to a previous version's data and creates a new version entry recording
the rollback. The version history is never destroyed — rollback is a forward operation.

**Request body:**

```json
{
  "version": 1
}
```

**Response** `200 OK`:

```json
{
  "version": 4,
  "status": "ok"
}
```

**Response** `400 Bad Request` — invalid version parameter (not a positive integer):

```json
{
  "type": "about:blank",
  "title": "Invalid rollback target",
  "status": 400,
  "detail": "version must be a positive integer"
}
```

**Response** `404 Not Found` — version not found in history:

```json
{
  "type": "about:blank",
  "title": "Version not found",
  "status": 404,
  "detail": "version 999 not found; available: [1, 2, 3]"
}
```

**Response** `422 Unprocessable Entity` — the target version's data fails current `Settings`
validation (the schema may have changed since that version was recorded):

```json
{
  "type": "about:blank",
  "title": "Rollback validation failed",
  "status": 422,
  "detail": "version 1 fails current config validation: ..."
}
```
