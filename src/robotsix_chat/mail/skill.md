# Mail (robotsix-auto-mail)

The mail tools connect directly to the robotsix-auto-mail board server — a live IMAP-backed mail
application. Use them to inspect the kanban triage board, move or delete mails across triage
columns, and browse / reorganise the archive.

## Handling deletion / cleanup requests

When a user asks you to find or delete mails — e.g. "delete newsletters", "check Lenovo/Macif
emails" — do **not** immediately dump the full board or a long list of every candidate you can
find.  An exhaustive list (including mails already discussed and uncertain mails the user never
asked about) overloads the user and makes it harder to act.  Instead:

1. **Confirm scope first.** Restate the specific requests you heard, by account, folder, or
   criterion — e.g. "You asked about newsletters, Lenovo, and Macif — here's my read on those."
2. **Present only the matching subset.** Show just the candidates that fall under the user's stated
   criteria.  Omit mails that were already discussed or decided, and omit uncertain candidates the
   user did not ask about.
3. **Wait for confirmation.** Do not enumerate further candidates, and do not perform any mutation,
   until the user confirms the subset is correct or asks for more.

## Read-only tools

| Tool                    | Description                                              |
| ----------------------- | -------------------------------------------------------- |
| `get_mail_board`        | Full board content (columns + cards) as JSON.            |
| `get_mail_email_status` | Triage column name for one email by message_id.          |
| `list_archive_folders`  | List all archive subfolders on the server.               |
| `browse_archive_folder` | List message envelope metadata in one archive subfolder. |

## Mutation tools

| Tool                            | Description                                                           |
| ------------------------------- | --------------------------------------------------------------------- |
| `move_mail_email`               | Move an email to a different triage column.                           |
| `delete_mail_email`             | Permanently delete an email from the board.                           |
| `archive_mail_email`            | Archive an email (mark as processed).                                 |
| `run_mail_triage`               | Re-classify the inbox with the configured triage rules.               |
| `cleanup_empty_archive_folders` | Remove empty archive subfolders from the IMAP server.                 |
| `delete_archive_folder`         | Delete a specific archive subfolder (empty-only unless `force=True`). |

## Agent tool: `move_archive_mail`

Move a mail between two archive subfolders on the IMAP server. Target folders are created **lazily**
— only when a message is actually moved into a new folder. Empty folders are not created in advance.

**This is a confirmation-gated mutation.** Before calling, state the exact message details (subject,
sender, date), the current archive subfolder, and the target subfolder in-chat and obtain explicit
operator approval. Never move a mail between archive folders without the operator's explicit consent
in the conversation — the operation modifies live IMAP state.

### Preconditions

- The mail must exist in the source archive subfolder.
- Both source and target must be under the archive root (enforced server-side).
- The IMAP server must be reachable and authenticated.

### Error responses

| Condition                | Message                                       |
| ------------------------ | --------------------------------------------- |
| Mail not found in source | `message not found in source folder`          |
| Folder path escapes root | `Folder path escapes archive root` (HTTP 400) |
| IMAP unreachable         | `IMAP not configured for this account` (503)  |
| Malformed JSON body      | `Malformed JSON body` (HTTP 400)              |

## Agent tool: `cleanup_empty_archive_folders`

Remove empty archive subfolders from the IMAP server. Only folders with zero messages are removed;
non-empty folders are left untouched. Use this periodically to keep the archive hierarchy clean
after moving or archiving messages.

### Preconditions

- The IMAP server must be reachable and authenticated.

### Error responses

| Condition        | Message                                      |
| ---------------- | -------------------------------------------- |
| IMAP unreachable | `IMAP not configured for this account` (503) |

## Agent tool: `delete_archive_folder`

Delete a specific archive subfolder from the IMAP server. By default, only empty folders (zero
messages) can be deleted; pass `force=True` to delete a non-empty folder.

**This is a confirmation-gated mutation.** Before calling, state the exact archive subfolder path
and whether the folder is empty or you are using `force` mode, and obtain explicit operator
approval. Never delete an archive folder without the operator's explicit consent.

### Preconditions

- The folder path must be relative to the archive root (`..` and absolute paths are rejected
  client-side).
- The IMAP server must be reachable and authenticated.
- By default the folder must be empty; use `force=True` to override.

### Error responses

| Condition                   | Message                                         |
| --------------------------- | ----------------------------------------------- |
| Folder not empty (no force) | `folder not empty — use force=true to override` |
| Folder path escapes root    | `Folder path escapes archive root` (HTTP 400)   |
| Folder not found            | `folder not found`                              |
| IMAP unreachable            | `IMAP not configured for this account` (503)    |
| Malformed JSON body         | `Malformed JSON body` (HTTP 400)                |

## Valid triage column names

`INBOX`, `HUMAN_TRIAGE`, `PENDING_ACTION`, `TO_ARCHIVE`, `TO_DELETE`, `TO_CALENDAR`, `TO_ANSWER`,
`DRAFT_READY`
