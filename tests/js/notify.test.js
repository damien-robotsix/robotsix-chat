// @vitest-environment node
//
// Unit tests for the desktop notification helper (notify.js).
//
// Verifies availability detection, permission-gating, no-repeat-prompt
// behaviour, and graceful no-op fallbacks.  The helper is pure (it touches
// only its resolved scope object), so these run in Node environment; each
// test stubs `globalThis.Notification`.

import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  isNotificationSupported,
  getNotificationPermission,
  requestNotificationPermission,
  notify,
} from "../../src/robotsix_chat/ui/static/notify.js";

function makeFakeNotification({ permission = "default" } = {}) {
  class FakeNotification {
    constructor(title, opts) {
      this.title = title;
      this.opts = opts || {};
      this.closed = false;
    }
    close() {
      this.closed = true;
    }
  }
  FakeNotification.permission = permission;
  FakeNotification.requestPermission = vi.fn(() => Promise.resolve("granted"));
  return FakeNotification;
}

describe("isNotificationSupported", () => {
  beforeEach(() => {
    delete globalThis.Notification;
  });

  it("returns false when the Notification API is absent", () => {
    expect(isNotificationSupported()).toBe(false);
  });

  it("returns true when the Notification API is present", () => {
    globalThis.Notification = makeFakeNotification();
    expect(isNotificationSupported()).toBe(true);
  });
});

describe("getNotificationPermission", () => {
  beforeEach(() => {
    delete globalThis.Notification;
  });

  it("reports unsupported when the API is absent", () => {
    expect(getNotificationPermission()).toBe("unsupported");
  });

  it("reports the underlying permission value", () => {
    globalThis.Notification = makeFakeNotification({ permission: "granted" });
    expect(getNotificationPermission()).toBe("granted");
  });
});

describe("requestNotificationPermission", () => {
  beforeEach(() => {
    delete globalThis.Notification;
  });

  it("returns unsupported when the API is absent (no throw)", async () => {
    await expect(requestNotificationPermission()).resolves.toBe("unsupported");
  });

  it("does NOT re-prompt when permission is already granted", async () => {
    const Fake = makeFakeNotification({ permission: "granted" });
    globalThis.Notification = Fake;
    await expect(requestNotificationPermission()).resolves.toBe("granted");
    expect(Fake.requestPermission).not.toHaveBeenCalled();
  });

  it("does NOT re-prompt when permission is already denied", async () => {
    const Fake = makeFakeNotification({ permission: "denied" });
    globalThis.Notification = Fake;
    await expect(requestNotificationPermission()).resolves.toBe("denied");
    expect(Fake.requestPermission).not.toHaveBeenCalled();
  });

  it("calls requestPermission when the state is undecided", async () => {
    const Fake = makeFakeNotification({ permission: "default" });
    globalThis.Notification = Fake;
    await expect(requestNotificationPermission()).resolves.toBe("granted");
    expect(Fake.requestPermission).toHaveBeenCalledTimes(1);
  });
});

describe("notify", () => {
  beforeEach(() => {
    delete globalThis.Notification;
  });

  it("shows no notification when the API is unavailable", () => {
    expect(notify({ title: "x" })).toBeNull();
  });

  it("shows no notification when permission is not granted", () => {
    globalThis.Notification = makeFakeNotification({ permission: "default" });
    expect(notify({ title: "x" })).toBeNull();

    globalThis.Notification = makeFakeNotification({ permission: "denied" });
    expect(notify({ title: "x" })).toBeNull();
  });

  it("creates a Notification with title/body/tag when granted", () => {
    globalThis.Notification = makeFakeNotification({ permission: "granted" });
    const n = notify({ title: "hello", body: "world", tag: "conv-1" });
    expect(n).not.toBeNull();
    expect(n.title).toBe("hello");
    expect(n.opts.body).toBe("world");
    expect(n.opts.tag).toBe("conv-1");
  });

  it("defaults title and passes an empty tag", () => {
    globalThis.Notification = makeFakeNotification({ permission: "granted" });
    const n = notify({});
    expect(n).not.toBeNull();
    expect(n.title).toBe("Notification");
    expect(n.opts.tag).toBe("");
  });

  it("wires onClick and closes the notification on click", () => {
    globalThis.Notification = makeFakeNotification({ permission: "granted" });
    const onClick = vi.fn();
    const n = notify({ title: "t", onClick });
    expect(onClick).not.toHaveBeenCalled();
    n.onclick();
    expect(onClick).toHaveBeenCalledWith(n);
    expect(n.closed).toBe(true);
  });
});
