New `repo_study` capability: the chat agent can fetch a temporary local snapshot of a GitHub
repository (tarball download — no `git` binary in the image) and study it with read-only tools
(`fetch_repo_for_study`, `list_repo_files`, `read_repo_file`, `search_repo_files`,
`drop_repo_workspace`). Workspaces live under `/data/repo_study`, are capped in size, and expire
automatically. Private repos authenticate through the existing `direct_repo` GitHub App credentials;
public repos need no auth. Config-gated by `repo_study.enabled` (off by default).
