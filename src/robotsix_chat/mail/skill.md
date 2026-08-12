# Mail (robotsix-auto-mail)

The mail tools connect directly to the robotsix-auto-mail board server — a live IMAP-backed mail
application. Use them to inspect the kanban triage board, move or delete mails across triage
columns, and browse / reorganise the archive.

## Read-only tools

| Tool                    | Description                                              |
| ----------------------- | -------------------------------------------------------- |
| `get_mail_board`        | Full board content (columns + cards) as JSON.            |
| `get_mail_email_status` | Triage column name for one email by message_id.          |
| `list_archive_folders`  | List all archive subfolders on the server.               |
| `browse_archive_folder` | List message envelope metadata in one archive subfolder. |

## Mutation tools

| Tool                 | Description                                             |
| -------------------- | ------------------------------------------------------- |
| `move_mail_email`    | Move an email to a different triage column.             |
| `delete_mail_email`  | Permanently delete an email from the board.             |
| `archive_mail_email` | Archive an email (mark as processed).                   |
| `run_mail_triage`    | Re-classify the inbox with the configured triage rules. |

## Agent tool: `move_archive_mail`

Move a mail between two archive subfolders on the IMAP server. The target subfolder hierarchy is
created automatically if it does not yet exist.

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

## Valid triage column names

`INBOX`, `HUMAN_TRIAGE`, `PENDING_ACTION`, `TO_ARCHIVE`, `TO_DELETE`, `TO_CALENDAR`, `TO_ANSWER`,
`DRAFT_READY`