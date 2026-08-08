// Unit tests for the reconnect guard (reconnect-guard.js).
//
// Verifies the single-in-flight latch, generation-stamp staleness,
// isClosed suppression, and cancel behaviour.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { createReconnectGuard } from "../../src/robotsix_chat/ui/static/reconnect-guard.js";

describe("createReconnectGuard", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // ------------------------------------------------------------------
  // Basic scheduling
  // ------------------------------------------------------------------
  it("schedules a reconnect after the delay", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    guard.schedule();
    expect(onReconnect).not.toHaveBeenCalled();

    vi.advanceTimersByTime(5000);
    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  // ------------------------------------------------------------------
  // Single-in-flight latch: second schedule() is a no-op
  // ------------------------------------------------------------------
  it("ignores a second schedule() while a timer is pending", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    guard.schedule();
    guard.schedule(); // second call — should be a no-op
    vi.advanceTimersByTime(5000);

    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  it("allows re-scheduling after the first timer fires", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    guard.schedule();
    vi.advanceTimersByTime(5000);
    expect(onReconnect).toHaveBeenCalledTimes(1);

    guard.schedule();
    vi.advanceTimersByTime(5000);
    expect(onReconnect).toHaveBeenCalledTimes(2);
  });

  // ------------------------------------------------------------------
  // cancel()
  // ------------------------------------------------------------------
  it("cancel() prevents the callback from firing", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    guard.schedule();
    guard.cancel();
    vi.advanceTimersByTime(5000);

    expect(onReconnect).not.toHaveBeenCalled();
  });

  it("cancel() clears the latch so a new schedule() works", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    guard.schedule();
    guard.cancel();
    guard.schedule();
    vi.advanceTimersByTime(5000);

    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  // ------------------------------------------------------------------
  // Generation stamp: stale callbacks are discarded
  // ------------------------------------------------------------------
  it("discards a pending callback when generation is bumped", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    guard.schedule();
    guard.bumpGeneration(); // simulates a new connection being opened
    vi.advanceTimersByTime(5000);

    // The callback captured gen=0, but generation is now 1 → discard.
    expect(onReconnect).not.toHaveBeenCalled();
  });

  it("does NOT discard when generation is bumped BEFORE scheduling", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    guard.bumpGeneration(); // gen = 1
    guard.schedule();       // captures gen = 1
    vi.advanceTimersByTime(5000);

    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  // ------------------------------------------------------------------
  // isClosed guard
  // ------------------------------------------------------------------
  it("suppresses scheduling when isClosed returns true", () => {
    const onReconnect = vi.fn();
    const closed = { value: true };
    const guard = createReconnectGuard({
      delayMs: 5000,
      onReconnect,
      isClosed: () => closed.value,
    });

    guard.schedule();
    vi.advanceTimersByTime(5000);
    expect(onReconnect).not.toHaveBeenCalled();

    // After the stream is no longer intentionally closed, scheduling
    // should work.
    closed.value = false;
    guard.schedule();
    vi.advanceTimersByTime(5000);
    expect(onReconnect).toHaveBeenCalledTimes(1);
  });

  // ------------------------------------------------------------------
  // Partial fire: the timer was scheduled but gen bump happens while
  // a second schedule was ignored due to the latch.
  // ------------------------------------------------------------------
  it("single-in-flight latch + gen bump: only the latest gen callback fires", () => {
    const onReconnect = vi.fn();
    const guard = createReconnectGuard({ delayMs: 5000, onReconnect });

    // First connection fails → schedule reconnect (gen=0).
    guard.schedule();

    // A new connection is opened externally → bump gen to 1, cancel timer.
    guard.cancel();
    guard.bumpGeneration();

    // The new connection also fails → schedule reconnect (gen=1).
    guard.schedule();

    vi.advanceTimersByTime(5000);
    expect(onReconnect).toHaveBeenCalledTimes(1);
  });
});
