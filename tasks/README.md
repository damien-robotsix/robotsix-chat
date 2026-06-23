# Task Tracking

This directory holds the canonical task list for the robotsix-chat repository.
Every pending, in-progress, blocked, or completed piece of work is recorded here
so the assistant (and any human) can pick up where the last conversation left
off.

## File layout

| File | Purpose |
|---|---|
| `tasks/TASKS.md` | Active tasks — pending, in-progress, or blocked. |
| `tasks/ARCHIVE.md` | Completed / done tasks (history preserved). |
| `tasks/README.md` | This document — format reference and workflow. |

## Task format

Each task is a level-2 heading followed by a bullet list of fields.  The
fields are **exactly** as shown — no extra fields, no missing fields.

```markdown
## T-NNNN — Short human-readable title

- status: pending | in-progress | blocked | done
- created: YYYY-MM-DDTHH:MM:SSZ
- updated: YYYY-MM-DDTHH:MM:SSZ
- notes: free-form Markdown (optional).  What's been done, what's blocking,
  next step — whatever helps the next person (or agent) resume the work.
```

### Field rules

- **id** (`T-NNNN`): stable, monotonic.  Pick the next number when adding a
  task (e.g. the last id is `T-0012` → use `T-0013`).  Never reuse an id,
  even after archiving.
- **status**: exactly one of `pending`, `in-progress`, `blocked`, or `done`.
  Tasks in `TASKS.md` must NOT be `done`; tasks in `ARCHIVE.md` must be `done`.
- **created / updated**: ISO-8601 UTC timestamps.  Use `date -u +"%Y-%m-%dT%H:%M:%SZ"` if writing by hand.
- **notes**: optional.  Keep it brief but useful — the goal is to minimise
  context-recovery time at the start of the next conversation.

## Workflow

### READ (start of every conversation)

1. Open `tasks/TASKS.md`.
2. Scan the active tasks so you know what's in flight.
3. If nothing is pending, that's fine — the file will just have a header.

### ADD (new work appears)

1. Determine the next id by looking at the highest `T-NNNN` in **both**
   `TASKS.md` and `ARCHIVE.md`, then increment.
2. Append a new section at the **bottom** of `tasks/TASKS.md`:
   ```markdown
   ## T-NNNN — Short title

   - status: pending
   - created: YYYY-MM-DDTHH:MM:SSZ
   - updated: YYYY-MM-DDTHH:MM:SSZ
   - notes: (whatever context is useful)
   ```

### UPDATE (work progresses)

1. Edit the task's fields directly in `tasks/TASKS.md`:
   - Change `status` (e.g. `pending` → `in-progress`).
   - Bump the `updated` timestamp.
   - Add to `notes` describing what was done or what's next.

### ARCHIVE (work is done)

1. In `tasks/TASKS.md`, set `status: done` and bump `updated`.
2. **Cut** the entire task section (heading + bullets) out of `TASKS.md`.
3. **Paste** it at the bottom of `tasks/ARCHIVE.md` (below any existing
   archive entries).
4. The active list stays focused; the history is preserved.
