# Mail (robotsix-auto-mail)

The mail tools connect directly to the robotsix-auto-mail board server — a live IMAP-backed mail
application. Use them to inspect the kanban triage board, move or delete mails across triage
columns, and browse / reorganise the archive.

## Mailbox / account counts

Never assert how many mailboxes or mail accounts exist from memory, a previous summary, or the
conversation so far — the live board can change between sessions and your recollection is
frequently stale. When the number of mailboxes/accounts matters to your reply, or the user corrects
you about it (e.g. "you are missing the other mailbox, there are 3 of them"):

1. **Query the live board first.** Call `get_mail_board` (which reads the live `/board-content`
   endpoint) and derive the account/mailbox count from the returned JSON before responding.
2. **Acknowledge the correction against live data.** If the live count disagrees with what you said,
   accept the correction and restate the live count — do not re-argue from memory.

## Handling deletion / cleanup requests

When a user asks you to find or delete mails — e.g. "delete newsletters", "check Lenovo/Macif
emails" — do **not** immediately dump the full board or a long list of every candidate you can find.
An exhaustive list (including mails already discussed and uncertain mails the user never asked
about) overloads the user and makes it harder to act. Instead:

1. **Confirm scope first.** Restate the specific requests you heard, by account, folder, or
   criterion — e.g. "You asked about newsletters, Lenovo, and Macif — here's my read on those."
2. **Present only the matching subset.** Show just the candidates that fall under the user's stated
   criteria. Omit mails that were already discussed or decided, and omit uncertain candidates the
   user did not ask about.
3. **Wait for confirmation.** Do not enumerate further candidates, and do not perform any mutation,
   until the user confirms the subset is correct or asks for more.

## Selecting an account

The auto-mail board can host multiple registered mail accounts. Call `list_mail_accounts` first to
discover the available accounts and their `account_id` values. To view a specific account's board,
pass its `account_id` to `get_mail_board` (e.g. `get_mail_board(account_id="acct_2")`). When no
`account_id` is given, `get_mail_board` returns the server's default account — do not assume that
covers every account the user asks about.

## Read-only tools

| Tool                    | Description                                                               |
| ----------------------- | ------------------------------------------------------------------------- |
| `get_mail_board`        | Full board content (columns + cards) as JSON; pass `account_id` to scope. |
| `list_mail_accounts`    | List registered mail accounts and their `account_id` values.              |
| `get_mail_email_status` | Triage column name for one email by message_id.                           |
| `list_archive_folders`  | List all archive subfolders on the server.                                |
| `browse_archive_folder` | List message envelope metadata in one archive subfolder.                  |

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

## Archive root (OVH-hosted accounts)

For OVH-hosted accounts the archive root IMAP folder is `INBOX/robotsix-mail-archive` — OVH IMAP
places user folders under the `INBOX/` namespace, so a bare `robotsix-mail-archive` root does not
exist on the server and the archive appears empty.

`archive_root` is server-side configuration on the auto-mail board server; the chat agent never
sends or receives it. If `list_archive_folders` returns an empty folder list (or the user reports an
empty archive), the archive root is likely misconfigured: suggest the operator set `archive_root` to
`INBOX/robotsix-mail-archive` for OVH-hosted accounts. The server's `GET /mail/archive-root-check`
endpoint reports the archive-root folder count and emits the same OVH guidance when the root looks
empty.

## Valid triage column names

`INBOX`, `HUMAN_TRIAGE`, `PENDING_ACTION`, `TO_ARCHIVE`, `TO_DELETE`, `TO_CALENDAR`, `TO_ANSWER`,
`DRAFT_READY`
