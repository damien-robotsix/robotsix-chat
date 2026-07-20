# Settings UI

The chat UI includes a settings panel that lets operators view and edit the server's runtime
configuration through a graphical interface, without needing direct filesystem access to the config
volume.

Use the **⚙** (gear) button in the header bar to open the panel. The panel can be resized by
dragging its left edge.

## How it works

The settings panel is backed by two HTTP endpoints on the chat server:

- **`GET /config`** — returns the current on-disk config with secret field values masked as `"***"`.
  This prevents secrets from being exposed to the browser.
- **`PUT /config`** — accepts a JSON object with the fields to change, **deep-merges** it over the
  existing on-disk config, validates the result through the `Settings` pydantic model, and only
  persists if validation passes.

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
is empty while `memory.enabled` is `true` — the save is **rejected** with HTTP 422 and the
validation error is returned to the UI. The on-disk config file and the running server are
**untouched**.

### Secret round-tripping

When the UI receives a secret field (e.g. `llmio_api_key`), the `GET /config` response masks it as
`"***"`. The form renders these fields as password inputs with the placeholder "Leave unchanged to
keep current secret".

When the operator saves, the UI sends `"***"` back for any secret the operator did not modify. The
server detects this sentinel and preserves the original on-disk value. If the operator types a new
value into a secret field, that new value is persisted.

## Operator workflow

1. Click the **⚙ Settings** button in the header bar.
2. Browse the config sections — nested objects are shown as collapsible sections.
3. Edit the fields you want to change:
   - **Text fields** — free-form string input.
   - **Number fields** — numeric input (step "1" for integers, "any" for floats).
   - **Boolean fields** — checkbox.
   - **Array fields** — text area containing JSON; edit the JSON directly.
   - **Secret fields** — password input; value shows as `"***"`; leave unchanged to keep the current
     secret.
4. Click **Save**.
   - **Success** — the panel shows "Saved." and reloads the form from the server.
   - **Validation failure** — the panel displays the pydantic validation error and highlights
     affected fields with a red border. You must fix the error before the save is accepted.
   - **Server error** — the panel shows the error message.
5. Close the panel with the **×** button, the **Escape** key, or by clicking the gear button again.

The panel remembers its open/closed state in `localStorage` across page reloads.

## Endpoint details

### `GET /config`

Returns the current on-disk config with secrets masked as `"***"`.

**Response** `200 OK`:

```json
{
  "server_port": 8000,
  "llmio_api_key": "***",
  "memory": {
    "enabled": true,
    "embedding": {
      "endpoint": "http://box:11434/v1"
    }
  }
}
```

When no config file exists yet, returns `{}`.

### `PUT /config`

Deep-merges the submitted JSON over the existing config, validates, and persists.

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
  "status": "ok"
}
```

**Response** `422 Unprocessable Entity` (validation failed):

```json
{
  "error": "config validation failed",
  "detail": "1 validation error for Settings\nmemory.embedding.endpoint\n  must be set when memory is enabled [type=...]"
}
```

**Response** `500 Internal Server Error` (I/O failure):

```json
{
  "error": "failed to write config: ..."
}
```
