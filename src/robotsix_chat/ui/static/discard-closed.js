// Bulk discard of closed periodic runs.
//
// The scheduler keeps the last few closed runs per preset and the sidebar
// hides them by default, but an operator who wants a clean list still had
// to delete each one by hand (and the old pile-ups ran to ~70). This takes
// the session list and a per-session delete function and removes every
// closed periodic run, one at a time, reporting what it could not delete.
//
// Pure logic: no DOM, no fetch — the caller injects `deleteSession`, so
// the behaviour is unit-testable (tests/ui/js/discard-closed.test.js).

/** Select the closed periodic runs in `sessions`, skipping `activeSessionId`. */
export function closedPeriodicSessions(sessions, periodicOwner, activeSessionId) {
  const out = [];
  for (const s of sessions || []) {
    if (!s || s._owner !== periodicOwner || !s.closed) continue;
    if (activeSessionId && s.session_id === activeSessionId) continue;
    out.push(s);
  }
  return out;
}

/**
 * Delete every session in `sessions` sequentially via `deleteSession(id)`.
 *
 * Sequential on purpose: each delete tears down an event stream and the
 * server rewrites the conversation store per call, so a burst of parallel
 * deletes races the store. A failed delete (thrown or rejected) is counted
 * and skipped; the rest still go.
 *
 * @returns {Promise<{deleted: number, failed: number, failures: Array<{id: string, error: string}>}>}
 */
export async function discardSessions(sessions, deleteSession) {
  const result = { deleted: 0, failed: 0, failures: [] };
  for (const s of sessions || []) {
    try {
      await deleteSession(s.session_id);
      result.deleted += 1;
    } catch (err) {
      result.failed += 1;
      result.failures.push({
        id: s.session_id,
        error: err && err.message ? err.message : String(err),
      });
    }
  }
  return result;
}
