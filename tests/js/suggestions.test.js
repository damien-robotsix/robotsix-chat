// Unit tests for the pure ```suggestions helpers (suggestions.js).
//
// These functions are DOM-free string transforms, so they run in a plain
// Node environment without jsdom.

import { describe, it, expect } from "vitest";
import {
  parseSuggestions,
  stripStreamingSuggestions,
} from "../../src/robotsix_chat/ui/static/suggestions.js";

describe("parseSuggestions", () => {
  it("returns null suggestions when no block is present", () => {
    const raw = "Just some prose with no fenced block.";
    const parsed = parseSuggestions(raw);
    expect(parsed.suggestions).toBeNull();
    expect(parsed.cleanText).toBe(raw);
  });

  it("extracts one option per line and strips the block from cleanText", () => {
    const raw =
      "Which do you want?\n\n" +
      "```suggestions\n" +
      "Approve ticket 73f3 and merge\n" +
      "Reject and close\n" +
      "```";
    const parsed = parseSuggestions(raw);
    expect(parsed.suggestions).toEqual([
      "Approve ticket 73f3 and merge",
      "Reject and close",
    ]);
    expect(parsed.cleanText).toBe("Which do you want?");
    expect(parsed.cleanText).not.toContain("```suggestions");
  });

  it("ignores blank lines inside the block", () => {
    const raw = "Pick:\n```suggestions\nA\n\n  \nB\n```";
    const parsed = parseSuggestions(raw);
    expect(parsed.suggestions).toEqual(["A", "B"]);
  });

  it("returns null suggestions for an empty block", () => {
    const raw = "Pick:\n```suggestions\n\n```";
    const parsed = parseSuggestions(raw);
    expect(parsed.suggestions).toBeNull();
  });
});

describe("stripStreamingSuggestions", () => {
  it("leaves text without a fence untouched", () => {
    const raw = "Plain streaming prose so far.";
    expect(stripStreamingSuggestions(raw)).toBe(raw);
  });

  it("strips a complete block that arrived mid-stream", () => {
    const raw = "Decide:\n\n```suggestions\nYes\nNo\n```";
    expect(stripStreamingSuggestions(raw)).toBe("Decide:");
  });

  it("strips an unclosed block still being streamed", () => {
    const raw = "Decide:\n\n```suggestions\nYes\nN";
    expect(stripStreamingSuggestions(raw)).toBe("Decide:");
  });

  it("hides the opener fence the instant it is complete", () => {
    const raw = "Decide:\n```suggestions";
    expect(stripStreamingSuggestions(raw)).toBe("Decide:");
  });

  it("hides a partially-typed opener fence (```sugg)", () => {
    const raw = "Decide:\n```sugg";
    expect(stripStreamingSuggestions(raw)).toBe("Decide:");
  });

  it("hides a bare triple-backtick at the very end", () => {
    const raw = "Decide:\n```";
    expect(stripStreamingSuggestions(raw)).toBe("Decide:");
  });

  it("leaves in-progress inline code (one or two backticks) alone", () => {
    expect(stripStreamingSuggestions("use `x")).toBe("use `x");
    expect(stripStreamingSuggestions("use ``x")).toBe("use ``x");
  });

  it("leaves a non-suggestions fence untouched while its body streams", () => {
    // Mid-stream, before the closing fence, an unrelated code block is intact.
    const raw = "```python\nprint(1)";
    expect(stripStreamingSuggestions(raw)).toBe(raw);
  });
});
