# Vision fallback (image captioning)

robotsix-chat lets users attach images to a chat turn. How an attachment is handled depends on the
**active chat model**:

- **Vision-capable models** (the keyless Claude SDK slot, levels 3–4) read the image bytes natively
  — the picture is passed straight to the model as a native image block.
- **Text-only models** (the keyed OpenRouter slot, levels 1–2 — e.g. DeepSeek) cannot accept image
  input. Sending image bytes to them fails upstream (`No endpoints found that support image input`).
  For these models robotsix-chat routes the attachment to a **configured vision model** that
  captions it, and the caption is handed back to the chat model as text.

The `vision_model` setting names the OpenRouter model used for that captioning fallback.

## Configuration

`vision_model` is a **top-level** setting. Like every other component-owned setting it is edited
through the component's own surface — there is **no dedicated environment variable** for it (the
only env var this app consumes is `ROBOTSIX_CONFIG_FILE`, which merely *locates* the config file).

Set it either way:

1. **Config file** — add/edit the `vision_model` key in `config/config.json` (or
   `config/config.local.json` for local runs) and restart the service.
1. **Settings panel** — open `⚙ Settings` in the browser chat UI, which loads the config via
   `GET /config` and persists via `PUT /config`.

| JSON key       | Type     | Default                         | Description                                                                                                                                          |
| -------------- | -------- | ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `vision_model` | `string` | `openrouter/openai/gpt-4o-mini` | OpenRouter model id used to caption attached images when the active chat model lacks vision support. Empty string means "vision model unconfigured". |

### Model id format

`vision_model` is an **OpenRouter model id** written as `openrouter/<vendor>/<model-slug>` — the
same `openrouter/` prefix used elsewhere in the config, followed by the vendor and model slug
exactly as OpenRouter lists them.

### Credentials

Captioning runs through the **keyed OpenRouter provider slot**, so it needs
[`llmio_api_key`](configuration.md) (your OpenRouter API key) to be set. No separate credential is
introduced: the vision call bills under the same OpenRouter key as the level 1–2 chat slots. A
vision model configured without an `llmio_api_key` cannot run — captioning then falls back to the
curated no-image-support error (see [Behavior](#behavior)).

## Supported vision models

Any vision-capable model available on OpenRouter works. Pick one by cost/quality and write it in the
`openrouter/<vendor>/<model-slug>` form. Common choices:

| `vision_model`                                        | Notes                                            |
| ----------------------------------------------------- | ------------------------------------------------ |
| `openrouter/openai/gpt-4o-mini`                       | Default — cheap, fast, good enough for captions. |
| `openrouter/openai/gpt-4o`                            | Higher-quality captions, higher cost.            |
| `openrouter/anthropic/claude-3.5-sonnet`              | Strong image understanding.                      |
| `openrouter/google/gemini-flash-1.5`                  | Low-cost Google vision model.                    |
| `openrouter/meta-llama/llama-3.2-90b-vision-instruct` | Open-weight vision model (Llava-family).         |

> Consult the [OpenRouter model list](https://openrouter.ai/models?modality=text%2Bimage) for the
> current set of vision-capable models and their exact slugs. The slug must name a model that
> accepts image input, or the captioning call will fail the same way a text-only chat model would.

## Behavior

The routing is decided per turn, from the active chat model and whether `vision_model` is
configured:

| Active chat model                       | `vision_model` set?   | What happens                                                                                                                                                       |
| --------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Vision-capable (Claude SDK, levels 3–4) | any                   | Image is read natively; `vision_model` is never consulted.                                                                                                         |
| Text-only (OpenRouter, levels 1–2)      | **yes** (default)     | The image is captioned by `vision_model`; the caption is substituted for the image so the chat model gets usable text.                                             |
| Text-only (OpenRouter, levels 1–2)      | **no** (empty string) | The curated no-image-support failure path applies: the user sees an actionable message explaining the active model cannot read images, not a raw "internal error". |

The `Settings.vision_model_configured` property (`bool(vision_model)`) is the switch: a non-empty
string means "captioning enabled", an empty string means "unconfigured".

### Error handling and user-facing messages

- **Unconfigured** (`vision_model: ""`) on a text-only model → the Phase 1 curated error message
  telling the user the active model cannot process images and how to proceed (switch to a
  vision-capable level, or configure `vision_model`). No raw provider error is surfaced.
- **Configured but the caption call fails** (bad slug, missing `llmio_api_key`, upstream outage) →
  the failure degrades to the same curated no-image-support message rather than crashing the turn.

## Examples

### Basic setup (default — captioning on)

The committed `config/config.json` template already enables the fallback with a cheap model:

```jsonc
{
  "chat_default_model_level": 2,          // a text-only OpenRouter level
  "llmio_api_key": "sk-or-...",           // pragma: allowlist secret — OpenRouter key
  "vision_model": "openrouter/openai/gpt-4o-mini"
}
```

With this config, a user attaching a PNG to a level-2 turn gets an automatic caption instead of an
error.

### Advanced: a stronger caption model

```jsonc
{
  "chat_default_model_level": 2,
  "llmio_api_key": "sk-or-...",           // pragma: allowlist secret
  "vision_model": "openrouter/openai/gpt-4o"
}
```

### Disabling the fallback (curated error instead of captioning)

Set `vision_model` to an empty string. Text-only models then return the curated no-image-support
message rather than captioning:

```jsonc
{
  "chat_default_model_level": 2,
  "vision_model": ""
}
```

## Migration guide

### Enabling vision fallback for an existing deployment

1. Ensure `llmio_api_key` (your OpenRouter key) is set — the caption call bills under it.
1. Set `vision_model` to a supported OpenRouter vision model, e.g. `openrouter/openai/gpt-4o-mini`.
1. Restart the service (or save via the Settings panel). Attachments on text-only levels are now
   captioned automatically.

### Backward compatibility

- **Default-on.** The field ships with a non-empty default (`openrouter/openai/gpt-4o-mini`), so a
  fresh deployment or one that adopts the new `config/config.json` template gets vision fallback
  **enabled by default**.
- **Older config files** that predate the field simply fall back to the model default when the key
  is absent — no config edit is required to pick up the feature.
- **Preserving the old behavior.** Deployments that want the pre-feature behavior (curated error, no
  captioning) opt out explicitly by setting `vision_model: ""`.
- **Vision-capable levels are unaffected** — levels 3–4 read images natively regardless of
  `vision_model`, so switching the fallback on or off never changes their behavior.

See [Configuration](configuration.md) for the full settings reference.
