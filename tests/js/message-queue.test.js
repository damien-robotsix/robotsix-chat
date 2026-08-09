// Unit tests for the message queue (message-queue.js).
//
// Verifies FIFO ordering, busy-state gating, drain-after-completion,
// and cancel operations.

import { describe, it, expect, vi } from "vitest";
import { createMessageQueue } from "../../src/robotsix_chat/ui/static/message-queue.js";

describe("createMessageQueue", () => {
  // ------------------------------------------------------------------
  // Basic enqueue + drain (not busy)
  // ------------------------------------------------------------------
  it("dispatches the first item immediately when not busy", () => {
    const startFn = vi.fn();
    const isBusy = () => false;
    const q = createMessageQueue({ startFn, isBusy });

    q.enqueue({ text: "hello" });
    expect(startFn).toHaveBeenCalledTimes(1);
    expect(startFn).toHaveBeenCalledWith({ text: "hello" });
  });

  it("dispatches items in FIFO order", () => {
    const dispatched = [];
    const startFn = (item) => dispatched.push(item.text);
    const isBusy = () => false;
    const q = createMessageQueue({ startFn, isBusy });

    q.enqueue({ text: "a" });
    q.enqueue({ text: "b" });
    q.enqueue({ text: "c" });

    // Not busy, so each enqueue immediately drains the front.
    expect(dispatched).toEqual(["a", "b", "c"]);
  });

  // ------------------------------------------------------------------
  // Busy gating: queue accumulates while busy
  // ------------------------------------------------------------------
  it("does not dispatch while busy", () => {
    const startFn = vi.fn();
    let busy = true;
    const isBusy = () => busy;
    const q = createMessageQueue({ startFn, isBusy });

    q.enqueue({ text: "a" });
    q.enqueue({ text: "b" });
    expect(startFn).not.toHaveBeenCalled();
  });

  it("drains the queue when the consumer becomes idle", () => {
    const dispatched = [];
    const startFn = (item) => dispatched.push(item.text);
    let busy = true;
    const isBusy = () => busy;
    const q = createMessageQueue({ startFn, isBusy });

    q.enqueue({ text: "a" });
    q.enqueue({ text: "b" });

    // Consumer finishes → call drain() manually (as doPost does).
    busy = false;
    q.drain();
    expect(dispatched).toEqual(["a"]);

    // After the first item dispatches, drain again (simulating the done
    // frame handler calling drainQueue).
    q.drain();
    expect(dispatched).toEqual(["a", "b"]);
  });

  it("drain is a no-op when both busy and empty", () => {
    const startFn = vi.fn();
    let busy = true;
    const isBusy = () => busy;
    const q = createMessageQueue({ startFn, isBusy });

    q.drain();
    expect(startFn).not.toHaveBeenCalled();
  });

  // ------------------------------------------------------------------
  // length and items
  // ------------------------------------------------------------------
  it("reports correct length", () => {
    const startFn = vi.fn();
    const isBusy = () => true; // always busy → queue accumulates
    const q = createMessageQueue({ startFn, isBusy });

    expect(q.length).toBe(0);
    q.enqueue({ text: "a" });
    expect(q.length).toBe(1);
    q.enqueue({ text: "b" });
    expect(q.length).toBe(2);
  });

  // ------------------------------------------------------------------
  // cancelAll
  // ------------------------------------------------------------------
  it("cancelAll clears the queue", () => {
    const startFn = vi.fn();
    const isBusy = () => true;
    const q = createMessageQueue({ startFn, isBusy });

    q.enqueue({ text: "a" });
    q.enqueue({ text: "b" });
    expect(q.length).toBe(2);

    q.cancelAll();
    expect(q.length).toBe(0);
  });

  // ------------------------------------------------------------------
  // cancelOne
  // ------------------------------------------------------------------
  it("cancelOne removes matching items", () => {
    const startFn = vi.fn();
    const isBusy = () => true;
    const q = createMessageQueue({ startFn, isBusy });

    q.enqueue({ text: "keep", id: 1 });
    q.enqueue({ text: "drop", id: 2 });
    q.enqueue({ text: "keep", id: 3 });

    q.cancelOne((item) => item.id === 2);
    expect(q.length).toBe(2);
    expect(q.items[0].id).toBe(1);
    expect(q.items[1].id).toBe(3);
  });

  // ------------------------------------------------------------------
  // onChange callback
  // ------------------------------------------------------------------
  it("calls onChange on enqueue", () => {
    const onChange = vi.fn();
    const q = createMessageQueue({
      startFn: vi.fn(),
      isBusy: () => true,
      onChange,
    });

    q.enqueue({ text: "a" });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("calls onChange on drain when empty", () => {
    const onChange = vi.fn();
    const q = createMessageQueue({
      startFn: vi.fn(),
      isBusy: () => false,
      onChange,
    });

    // Drain with empty queue still calls onChange (for UI updates).
    q.drain();
    expect(onChange).toHaveBeenCalled();
  });

  it("calls onChange on cancelAll", () => {
    const onChange = vi.fn();
    const q = createMessageQueue({
      startFn: vi.fn(),
      isBusy: () => true,
      onChange,
    });

    q.enqueue({ text: "a" });
    onChange.mockClear();
    q.cancelAll();
    expect(onChange).toHaveBeenCalledTimes(1);
  });
});
