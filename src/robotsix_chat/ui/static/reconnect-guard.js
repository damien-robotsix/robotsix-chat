// Reconnect guard — prevents duplicate reconnection timers.
//
// Extracted from chat.js:scheduleEventReconnect and the generation-stamp
// pattern used in openEventStream.  The guard ensures:
//   1. Only one reconnect timer is scheduled at a time (single-in-flight
//      latch).
//   2. A generation stamp lets callers detect and discard stale callbacks
//      from a prior connection.
//
// This is the mechanism that prevented the duplicate-bubble regression
// (documented at chat.js:~1235-1252).

/**
 * Create a reconnect guard.
 *
 * @param {Object} opts
 * @param {number}  opts.delayMs      - reconnect delay in ms (default 5000)
 * @param {Function} opts.onReconnect - called when the timer fires
 * @param {Function} [opts.isClosed]  - if returns true, scheduling is
 *                                      suppressed (optional)
 * @returns {{ schedule: Function, cancel: Function, bumpGeneration: Function,
 *             generation: number }}
 */
export function createReconnectGuard({ delayMs = 5000, onReconnect, isClosed } = {}) {
  let timer = null;
  let generation = 0;

  function schedule() {
    if (isClosed && isClosed()) return;
    if (timer !== null) return; // single-in-flight latch
    const gen = generation;
    timer = setTimeout(function () {
      timer = null;
      // Only fire if the generation hasn't changed since scheduling.
      // A caller that bumps the generation after cancel() + new open
      // invalidates this callback.
      if (gen === generation) {
        onReconnect();
      }
    }, delayMs);
  }

  function cancel() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  function bumpGeneration() {
    generation++;
  }

  return { schedule, cancel, bumpGeneration, get generation() { return generation; } };
}
