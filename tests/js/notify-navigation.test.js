// @vitest-environment node
//
// Unit tests for the desktop-notification click routing (notify-navigation.js).
//
// The navigation primitives are injected, so these run in the Node
// environment with a fake `app`. Each test asserts the click handler focuses
// the window and routes to the correct destination.

import { describe, it, expect, vi } from "vitest";
import {
  buildMainConversationClick,
  buildSubsessionClick,
} from "../../src/robotsix_chat/ui/static/notify-navigation.js";

function makeApp() {
  return {
    focusWindow: vi.fn(),
    openMainConversation: vi.fn(),
    openSubsession: vi.fn(),
  };
}

describe("buildMainConversationClick", () => {
  it("focuses the window and opens the main conversation", () => {
    const app = makeApp();
    const onClick = buildMainConversationClick(app, "sess-1");
    onClick();
    expect(app.focusWindow).toHaveBeenCalledTimes(1);
    expect(app.openMainConversation).toHaveBeenCalledWith("sess-1");
    expect(app.openSubsession).not.toHaveBeenCalled();
  });

  it("focuses before navigating", () => {
    const app = makeApp();
    const order = [];
    app.focusWindow.mockImplementation(() => order.push("focus"));
    app.openMainConversation.mockImplementation(() => order.push("nav"));
    buildMainConversationClick(app, "sess-1")();
    expect(order).toEqual(["focus", "nav"]);
  });
});

describe("buildSubsessionClick", () => {
  it("focuses the window and opens the target subsession", () => {
    const app = makeApp();
    const onClick = buildSubsessionClick(app, "sub-9");
    onClick();
    expect(app.focusWindow).toHaveBeenCalledTimes(1);
    expect(app.openSubsession).toHaveBeenCalledWith("sub-9");
    expect(app.openMainConversation).not.toHaveBeenCalled();
  });

  it("focuses before navigating", () => {
    const app = makeApp();
    const order = [];
    app.focusWindow.mockImplementation(() => order.push("focus"));
    app.openSubsession.mockImplementation(() => order.push("nav"));
    buildSubsessionClick(app, "sub-9")();
    expect(order).toEqual(["focus", "nav"]);
  });
});
