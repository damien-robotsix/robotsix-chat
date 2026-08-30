// Unit tests for the ```suggestions helpers (suggestions.js).
//
// String-transform tests are pure; chip-rendering tests use the jsdom
// environment configured in vitest.config.js.

import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  parseSuggestions,
  stripStreamingSuggestions,
  renderSuggestionChips,
  disableStaleSuggestionChips,
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

// ---- DOM-coupled chip tests (jsdom) -----------------------------------

describe("renderSuggestionChips", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("renders one button per suggestion below the anchor element", () => {
    const parent = document.createElement("div");
    const anchor = document.createElement("p");
    anchor.textContent = "Which do you want?";
    parent.appendChild(anchor);
    document.body.appendChild(parent);

    const container = renderSuggestionChips(
      ["Approve and merge", "Reject and close"],
      () => {},
      anchor,
    );

    expect(container.className).toBe("suggestion-chips");
    const chips = container.querySelectorAll(".suggestion-chip");
    expect(chips.length).toBe(2);
    expect(chips[0].textContent).toBe("Approve and merge");
    expect(chips[1].textContent).toBe("Reject and close");
    // Chips are inserted after the anchor in the parent.
    expect(anchor.nextSibling).toBe(container);
  });

  it("keeps the free-text input available (does not remove or hide it)", () => {
    const parent = document.createElement("div");
    const anchor = document.createElement("p");
    parent.appendChild(anchor);
    const input = document.createElement("textarea");
    input.id = "msg-input";
    parent.appendChild(input);
    document.body.appendChild(parent);

    renderSuggestionChips(["Yes", "No"], () => {}, anchor);

    // The input is still in the DOM and not hidden.
    expect(document.getElementById("msg-input")).not.toBeNull();
  });

  it("calls onSubmit with the chip text when a chip is clicked", () => {
    const parent = document.createElement("div");
    const anchor = document.createElement("p");
    parent.appendChild(anchor);
    document.body.appendChild(parent);

    const onSubmit = vi.fn();
    const container = renderSuggestionChips(
      ["Option A", "Option B"],
      onSubmit,
      anchor,
    );

    const chips = container.querySelectorAll(".suggestion-chip");
    chips[1].click();
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith("Option B");
  });

  it("renders disabled chips when disabled=true", () => {
    const parent = document.createElement("div");
    const anchor = document.createElement("p");
    parent.appendChild(anchor);
    document.body.appendChild(parent);

    const onSubmit = vi.fn();
    const container = renderSuggestionChips(
      ["Yes", "No"],
      onSubmit,
      anchor,
      true,
    );

    const chips = container.querySelectorAll(".suggestion-chip");
    expect(chips[0].disabled).toBe(true);
    expect(chips[0].classList.contains("suggestion-chip--stale")).toBe(true);
    expect(container.classList.contains("suggestion-chips--stale")).toBe(true);
    chips[0].click();
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("disableStaleSuggestionChips", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("disables all active chips and marks them stale", () => {
    // Build a container with two anchor elements; chips are inserted as
    // siblings of the anchor inside the same parent.
    const root = document.createElement("div");
    document.body.appendChild(root);
    const anchor1 = document.createElement("p");
    const anchor2 = document.createElement("p");
    root.appendChild(anchor1);
    root.appendChild(anchor2);

    // Two sets of chips — one already stale, one active.
    const old = renderSuggestionChips(["Old A"], () => {}, anchor1, true);
    const fresh = renderSuggestionChips(["New A", "New B"], () => {}, anchor2);

    disableStaleSuggestionChips(root);

    const allChips = root.querySelectorAll(".suggestion-chip");
    for (const chip of allChips) {
      expect(chip.disabled).toBe(true);
      expect(chip.classList.contains("suggestion-chip--stale")).toBe(true);
    }
    expect(fresh.classList.contains("suggestion-chips--stale")).toBe(true);
  });

  it("prevents double-send: clicking a disabled chip does not fire onSubmit", () => {
    const root = document.createElement("div");
    document.body.appendChild(root);
    const anchor = document.createElement("p");
    root.appendChild(anchor);

    const onSubmit = vi.fn();
    renderSuggestionChips(["Yes", "No"], onSubmit, anchor);
    disableStaleSuggestionChips(root);

    const chips = root.querySelectorAll(".suggestion-chip");
    chips[0].click();
    chips[1].click();
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("streaming → finalised chip pipeline", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("hides raw fence during streaming and shows chips after finalisation", () => {
    // Simulate the streaming flow: tokens arrive one at a time.
    var raw = "";
    const tokens = [
      "Here are your options:\n\n",
      "```suggestions\n",
      "Deploy now\n",
      "Wait until Monday\n",
      "```",
    ];

    for (const token of tokens) {
      raw += token;
      // During streaming, the visible text must never show the fence.
      const visible = stripStreamingSuggestions(raw);
      expect(visible).not.toContain("```suggestions");
      expect(visible).not.toContain("```");
    }

    // After the full message has arrived, finalise: parse + render chips.
    const parsed = parseSuggestions(raw);
    expect(parsed.suggestions).toEqual(["Deploy now", "Wait until Monday"]);
    expect(parsed.cleanText).not.toContain("```");

    const parent = document.createElement("div");
    const bubble = document.createElement("div");
    parent.appendChild(bubble);
    document.body.appendChild(parent);

    bubble.innerHTML = parsed.cleanText;
    renderSuggestionChips(parsed.suggestions, () => {}, bubble);

    const chips = parent.querySelectorAll(".suggestion-chip");
    expect(chips.length).toBe(2);
    expect(chips[0].textContent).toBe("Deploy now");
    expect(chips[1].textContent).toBe("Wait until Monday");
  });
});
