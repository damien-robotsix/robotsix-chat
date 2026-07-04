Standards-alignment sweep against robotsix-standards: fix the deploy contract's `config-target`
label (absolute path + dedicated `chat-config` volume); commit `config/config.json` as the defaults
template (local credentials move to the gitignored `config/config.local.json`); refactor the
Dockerfile runtime stage to the standard copy-from-builder pattern (no uv/git in the runtime image)
and drop the dead `SERVER_HOST`/`SERVER_PORT` env vars; conform to the fleet-wide 80% coverage floor
(no per-repo threshold); add shared `baseline-check`, `auto-release`, and `changelog-check` workflow
callers and bump all shared workflow pins; adopt the robotsix-modules taxonomy schema for
`docs/modules.yaml` (kebab-case ids, housekeeping module) and retire the custom drift script; add
the standard pytest `live`-marker addopts; remove the retired YAML-era config artifacts
(`deploy/config.example.yaml`, `.env.example`, `config/skills/`, stale docs), the unused `pyyaml`
dependency, and 137 accidentally committed `.local_pkgs/` vendored files.
