// Unit tests for the durable draft drain (drain-draft.js).
//
// Regression: queued messages were lost when a drain's fire-and-forget
// sends failed while the draft was cleared unconditionally.

import { describe, it, expect, vi } from "vitest";
import { drainSessionDraft } from "../../src/robotsix_chat/ui/static/drain-draft.js";

const item = (text) => ({ text, images: [] });

describe("drainSessionDraft", () => {
  it("sends every item then clears the draft", async () => {
    const sent = [];
    const puts = [];
    const res = await drainSessionDraft({
      fetchDraft: async () => ({ queue: [item("a"), item("b")] }),
      sendItem: async (it) => { sent.push(it.text); return true; },
      putDraft: async (q) => { puts.push(q); },
    });
    expect(sent).toEqual(["a", "b"]);
    expect(puts).toEqual([[]]);
    expect(res).toEqual({ sent: 2, failed: 0 });
  });

  it("writes failed items back instead of clearing", async () => {
    const puts = [];
    const res = await drainSessionDraft({
      fetchDraft: async () => ({ queue: [item("a"), item("b"), item("c")] }),
      sendItem: async (it) => it.text !== "b",
      putDraft: async (q) => { puts.push(q.map((i) => i.text)); },
    });
    expect(puts).toEqual([["b"]]);
    expect(res).toEqual({ sent: 2, failed: 1 });
  });

  it("treats a thrown send as a failure and preserves the item", async () => {
    const puts = [];
    await drainSessionDraft({
      fetchDraft: async () => ({ queue: [item("boom")] }),
      sendItem: async () => { throw new Error("network"); },
      putDraft: async (q) => { puts.push(q.map((i) => i.text)); },
    });
    expect(puts).toEqual([["boom"]]);
  });

  it("does nothing on an empty or unreadable draft", async () => {
    const putDraft = vi.fn();
    expect(
      await drainSessionDraft({ fetchDraft: async () => null, sendItem: vi.fn(), putDraft })
    ).toEqual({ sent: 0, failed: 0 });
    expect(
      await drainSessionDraft({ fetchDraft: async () => { throw new Error("x"); }, sendItem: vi.fn(), putDraft })
    ).toEqual({ sent: 0, failed: 0 });
    expect(putDraft).not.toHaveBeenCalled();
  });

  it("skips empty placeholder items without failing them", async () => {
    const sent = [];
    const puts = [];
    await drainSessionDraft({
      fetchDraft: async () => ({ queue: [item(""), item("real")] }),
      sendItem: async (it) => { sent.push(it.text); return true; },
      putDraft: async (q) => { puts.push(q); },
    });
    expect(sent).toEqual(["real"]);
    expect(puts).toEqual([[]]);
  });
});
