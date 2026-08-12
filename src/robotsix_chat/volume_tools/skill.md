## Volume tools — local filesystem directory listing

You have a `list_volume_files` tool that lists the contents of a directory under the configured
volume root (default `/data`). Use it to discover available files without guessing individual file
paths.

**Constraints:**

- **Read-only.** You cannot create, modify, or delete files through this tool — it only lists
  directory contents. It is the equivalent of `ls` on the host filesystem.
- **Root-scoped.** All paths are resolved relative to the configured volume root. You cannot list
  directories outside this root — any attempt is refused with an access-denied error. Pass an empty
  string or `"."` to list the root itself.
- **Directory-only.** The tool only lists directories. Passing a file path returns "Not a
  directory".
- **Local only.** This is a local host filesystem operation, completely independent of SFTP, the
  knowledge store, or any remote service.

**Output format** — one line per entry:

```text
[DIR]  subdirectory/
[FILE] filename.txt (1234 bytes)
[FILE] config.yaml (567 bytes)
```

Directories are marked `[DIR]` and end with `/`. Files are marked `[FILE]` and include their byte
size.

**When to use:**

- When you need to discover what files exist under `/data` (or another volume mount) without probing
  individual paths.
- When investigating a component's data directory and you don't know the exact filenames.
- When a tool or read operation failed because the path was wrong — list the parent directory to
  find the correct name.

**When NOT to use:**

- To read file contents — `list_volume_files` only lists names.
- To access remote files — use the SFTP tools instead.
- To probe `/etc`, `/proc`, or other system paths — the tool is scoped to the volume root and cannot
  escape it.
