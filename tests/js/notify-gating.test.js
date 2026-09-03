// @vitest-environment node
//
// Unit tests for the focus-aware desktop-notification gating helper
// (notify-gating.js).
//
// Verifies the de-duplication contract: a notification is suppressed when
// the browser tab is visible AND the target conversation/subsession is the
// one the user is actively viewing.  The helper is pure (no DOM side
// effects), so these run in the Node environment.

import { describe, it, expect } from "vitest";
import {
  isDocumentVisible,
  shouldNotifyMainConversation,
  shouldNotifySubsession,
} from "../../src/robotsix_chat/ui/static/notify-gating.js";

describe("isDocumentVisible", () => {
  it("treats a visible document as visible", () => {
    expect(isDocumentVisible({ hidden: false })).toBe(true);
  });

  it("treats a hidden document as hidden", () => {
    expect(isDocumentVisible({ hidden: true })).toBe(false);
  });

  it("treats a document without the hidden property as visible", () => {
    expect(isDocumentVisible({})).toBe(true);
  });

  it("returns false when given no document-like object", () => {
    expect(isDocumentVisible(null)).toBe(false);
  });
});

describe("shouldNotifyMainConversation", () => {
  it("notifies when the tab is hidden (user is elsewhere)", () => {
    expect(shouldNotifyMainConversation({ docVisible: false, subsessionFocused: false })).toBe(true);
    expect(shouldNotifyMainConversation({ docVisible: false, subsessionFocused: true })).toBe(true);
  });

  it("suppresses while the user views the main conversation", () => {
    expect(shouldNotifyMainConversation({ docVisible: true, subsessionFocused: false })).toBe(false);
  });

  it("notifies when the tab is visible but a subsession is focused", () => {
    expect(shouldNotifyMainConversation({ docVisible: true, subsessionFocused: true })).toBe(true);
  });
});

describe("shouldNotifySubsession", () => {
  it("notifies when the tab is hidden (user is elsewhere)", () => {
    expect(shouldNotifySubsession({ docVisible: false, focused: false })).toBe(true);
    expect(shouldNotifySubsession({ docVisible: false, focused: true })).toBe(true);
  });

  it("suppresses while the user views this exact subsession", () => {
    expect(shouldNotifySubsession({ docVisible: true, focused: true })).toBe(false);
  });

  it("notifies when the tab is visible but a different target is focused", () => {
    expect(shouldNotifySubsession({ docVisible: true, focused: false })).toBe(true);
  });
});