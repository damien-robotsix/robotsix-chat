// Unit tests for the bulk discard of closed periodic runs (discard-closed.js).

import { describe, it, expect } from "vitest";
import {
  closedPeriodicSessions,
  discardSessions,
} from "../../src/robotsix_chat/ui/static/discard-closed.js";

const PERIODIC = "periodic";
const row = (id, owner, closed) => ({ session_id: id, _owner: owner, closed });

describe("closedPeriodicSessions", () => {
  it("keeps only closed periodic runs", () => {
    const sessions = [
      row("a", PERIODIC, true),
      row("b", PERIODIC, false),
      row("c", "operator", true),
      row("d", PERIODIC, true),
    ];
    expect(closedPeriodicSessions(sessions, PERIODIC, null).map((s) => s.session_id))
      .toEqual(["a", "d"]);
  });

  it("never selects the run the operator is viewing", () => {
    const sessions = [row("a", PERIODIC, true), row("b", PERIODIC, true)];
    expect(closedPeriodicSessions(sessions, PERIODIC, "a").map((s) => s.session_id))
      .toEqual(["b"]);
  });

  it("tolerates an empty or missing list", () => {
    expect(closedPeriodicSessions([], PERIODIC, null)).toEqual([]);
    expect(closedPeriodicSessions(undefined, PERIODIC, null)).toEqual([]);
  });
});

describe("discardSessions", () => {
  it("deletes every session sequentially and counts them", async () => {
    const order = [];
    let inFlight = 0, maxInFlight = 0;
    const res = await discardSessions(
      [row("a", PERIODIC, true), row("b", PERIODIC, true), row("c", PERIODIC, true)],
      async (id) => {
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        await new Promise((r) => setTimeout(r, 1));
        order.push(id);
        inFlight -= 1;
      },
    );
    expect(order).toEqual(["a", "b", "c"]);
    expect(maxInFlight).toBe(1);
    expect(res).toEqual({ deleted: 3, failed: 0, failures: [] });
  });

  it("counts a refused delete and keeps going", async () => {
    const res = await discardSessions(
      [row("a", PERIODIC, true), row("b", PERIODIC, true), row("c", PERIODIC, true)],
      async (id) => {
        if (id === "b") throw new Error("Delete failed (HTTP 404)");
      },
    );
    expect(res.deleted).toBe(2);
    expect(res.failed).toBe(1);
    expect(res.failures).toEqual([{ id: "b", error: "Delete failed (HTTP 404)" }]);
  });

  it("returns zeros for nothing to do", async () => {
    expect(await discardSessions([], async () => {})).toEqual({
      deleted: 0, failed: 0, failures: [],
    });
  });
});
