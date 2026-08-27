Generate and drift-check `config/config.schema.json` with the shared `robotsix-config schema` CLI
instead of the local `scripts/regenerate_schema.py`, so the committed file uses the one canonical
formatting every fleet component shares. Also bumps the `robotsix-config` pin to the commit whose
`load_config` runs legacy-key migrations before stripping unknown keys — without it the
`memory.llm.api_key` migration silently stopped firing.
