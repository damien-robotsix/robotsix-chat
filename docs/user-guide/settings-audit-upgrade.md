# Settings Audit — Upgrade & Migration Notes

The **settings audit** cleaned up and reorganized robotsix-chat's application configuration: dead
settings were removed, one misleadingly-named key was renamed (with backward compatibility), and the
Settings panel gained UX improvements. This page is the operator-facing summary of what changed and
what — if anything — you need to do when upgrading.

**Bottom line:** no operator action is required. Every change is backward compatible — old config
files continue to load unchanged, and removed keys are silently ignored rather than rejected. See
[Configuration](../configuration.md) for the full, up-to-date settings reference.

## Removed settings

The following keys were **non-functional** — they were declared in the config model but no longer
drove any behaviour — and have been removed:

- `llmio_task_budget_tokens` — the task-budget / self-pacing countdown on keyless tiers was
  decommissioned.
- `compaction_min_turns` — idle-timeout compaction was removed.
- `compaction_keep_recent_turns` — idle-timeout compaction was removed.

Context reduction is now handled solely by the periodic summary scheduler; the compaction knobs no
longer had any effect.

**Impact on your config: none.** These keys had no effect before removal, so dropping them changes
no behaviour. A deployed config file that still carries any of them **loads normally** — the loader
strips the stale key before validation and logs an informational message, so a startup crash is not
possible even though the config model otherwise forbids unknown keys. You may delete these keys from
your config file at your leisure, but you are not required to.

## Renamed setting

`llmio_api_key` was renamed to **`openrouter_api_key`**. The old name was misleading: the field is
the OpenRouter fallback-slot API key (also used for vision captioning), not a generic "llmio" key.

**Backward compatibility is solid.** A config file that still uses `llmio_api_key` continues to work
— the loader accepts it as an alias and maps its value onto `openrouter_api_key` at load time. If
both keys are present, an explicitly-set `openrouter_api_key` wins; otherwise the legacy value is
carried over. No secret is lost and no startup crash occurs.

**Recommended (optional) step:** rename the key in your config file to `openrouter_api_key` the next
time you edit it, so the file matches the canonical schema. This is a cosmetic cleanup — the alias
will keep working.

## New Settings-panel UX

The browser Settings panel (**⚙**) gained the following improvements. They are UI-only and require
no config changes:

- **JSON editor for `llmio_tier_overrides`.** This object field is now rendered as a validated JSON
  textarea. Enter a JSON object; invalid JSON (or valid JSON that is not an object) is rejected with
  a clear error before anything is persisted. Existing values round-trip unchanged.
- **Multi-line textareas for long free-text fields.** Long string fields such as `agent_instruction`
  are now edited in enlarged multi-line textareas instead of single-line inputs.
- **Settings grouped by category.** Fields carry group labels, so the panel organizes settings into
  labelled sections (e.g. LLM I/O, authentication) rather than one flat list.

See the [Settings UI guide](settings-ui.md) for the full panel walkthrough.

## Upgrade checklist

1. Upgrade to the new image / release as usual — no config edit is required to boot.
1. (Optional) Rename `llmio_api_key` → `openrouter_api_key` in your config file the next time you
   edit it.
1. (Optional) Delete the removed keys (`llmio_task_budget_tokens`, `compaction_min_turns`,
   `compaction_keep_recent_turns`) from your config file; they are ignored either way.
