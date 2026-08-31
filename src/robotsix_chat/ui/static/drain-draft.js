// Durable drain of a session's server-side draft queue.
//
// Extracted from chat.js:drainBackgroundSession, which used to POST every
// queued message fire-and-forget and then clear the draft UNCONDITIONALLY —
// one failed send (network blip, chat mid-redeploy) and the operator's
// queued messages were wiped with nothing left to retry
// (operator-reported: "the queued messages seem to be lost when I change
// sessions").
//
// Contract: the draft is cleared only after every item was sent and
// acknowledged; items that fail are written BACK to the draft (in order) so
// a later drain — or restoreDraft() when the operator returns — can retry.

/**
 * Drain one session's draft queue durably.
 *
 * @param {Object}   opts
 * @param {Function} opts.fetchDraft - async () => draft object ({queue: []})
 *                                     or null on error.
 * @param {Function} opts.sendItem   - async (item) => boolean — true when the
 *                                     server acknowledged the message.
 * @param {Function} opts.putDraft   - async (queueItems) => void — persist
 *                                     the given items as the new draft
 *                                     (empty array clears it).
 * @returns {Promise<{sent: number, failed: number}>}
 */
export async function drainSessionDraft({ fetchDraft, sendItem, putDraft }) {
  let draft = null;
  try {
    draft = await fetchDraft();
  } catch (_) {
    return { sent: 0, failed: 0 };
  }
  const queue = draft && Array.isArray(draft.queue) ? draft.queue : [];
  const items = queue.filter(
    (it) => it && (it.text || (Array.isArray(it.images) && it.images.length > 0))
  );
  if (items.length === 0) return { sent: 0, failed: 0 };

  const failed = [];
  let sent = 0;
  // Sequential on purpose: preserves message order in the conversation and
  // avoids hammering a server that may be mid-restart.
  for (const item of items) {
    let ok = false;
    try {
      ok = await sendItem(item);
    } catch (_) {
      ok = false;
    }
    if (ok) {
      sent++;
    } else {
      failed.push(item);
    }
  }

  try {
    // Only successful sends leave the draft; failures are preserved for a
    // later retry instead of being wiped.
    await putDraft(failed);
  } catch (_) {
    // Best-effort: if even the draft write fails, the old draft remains —
    // worst case a successful message is re-sent later, which the server's
    // message-id dedupe absorbs.
  }
  return { sent, failed: failed.length };
}
