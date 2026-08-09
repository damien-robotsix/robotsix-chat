// Message queue — FIFO dispatch with busy-state gating.
//
// Extracted from chat.js:drainQueue / messageQueue.  A queue that holds
// items and dispatches them one at a time via a user-provided startFn.
// While the consumer reports "busy", the queue accumulates; when the
// consumer goes idle, the next item is dispatched automatically.

/**
 * Create a message queue.
 *
 * @param {Object} opts
 * @param {Function} opts.startFn    - called with (item) to begin processing
 * @param {Function} opts.isBusy     - returns true while a request is in
 *                                     flight (sending/streaming); the queue
 *                                     will not dispatch the next item while
 *                                     busy.
 * @param {Function} [opts.onChange] - called whenever the queue changes
 *                                     (enqueue, dequeue, clear); useful for
 *                                     updating UI (e.g. cancel-queued button
 *                                     visibility).
 * @returns {{ enqueue: Function, drain: Function, cancelAll: Function,
 *             cancelOne: Function, length: number, items: Array }}
 */
export function createMessageQueue({ startFn, isBusy, onChange } = {}) {
  const items = []; // FIFO queue

  function drain() {
    if (isBusy && isBusy()) return;
    if (items.length === 0) {
      if (onChange) onChange();
      return;
    }
    const item = items.shift();
    startFn(item);
    if (onChange) onChange();
  }

  function enqueue(item) {
    items.push(item);
    if (onChange) onChange();
    drain();
  }

  function cancelAll() {
    items.length = 0;
    if (onChange) onChange();
  }

  function cancelOne(predicate) {
    for (let i = items.length - 1; i >= 0; i--) {
      if (predicate(items[i])) {
        items.splice(i, 1);
      }
    }
    if (onChange) onChange();
  }

  return {
    enqueue,
    drain,
    cancelAll,
    cancelOne,
    get length() {
      return items.length;
    },
    get items() {
      return items;
    },
  };
}
