import { describe, it, expect, beforeEach } from "vitest";
import { renderMemoryBanner } from "../../src/robotsix_chat/ui/static/memory-banner.js";

function makeBanner() {
  const el = document.createElement("div");
  el.id = "memory-banner";
  const msg = document.createElement("span");
  msg.className = "msg";
  el.appendChild(msg);
  document.body.appendChild(el);
  return el;
}

describe("renderMemoryBanner", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("stays hidden when the backend is healthy", () => {
    const el = makeBanner();
    expect(renderMemoryBanner(el, { backend: "cognee", degraded: false })).toBe(false);
    expect(el.classList.contains("visible")).toBe(false);
  });

  it("stays hidden when /health reports no memory block", () => {
    const el = makeBanner();
    expect(renderMemoryBanner(el, undefined)).toBe(false);
    expect(el.classList.contains("visible")).toBe(false);
  });

  it("shows the banner and names the backend when degraded", () => {
    const el = makeBanner();
    expect(renderMemoryBanner(el, { backend: "cognee", degraded: true })).toBe(true);
    expect(el.classList.contains("visible")).toBe(true);
    expect(el.textContent).toContain("Memory (cognee) is not working.");
    expect(el.textContent).toContain("without recall");
  });

  it("includes the reason when the backend supplies one", () => {
    const el = makeBanner();
    renderMemoryBanner(el, {
      backend: "cognee",
      degraded: true,
      reason: "recall failed: UNREACHABLE_CODE",
    });
    expect(el.textContent).toContain("UNREACHABLE_CODE");
  });

  it("escapes the reason rather than treating it as markup", () => {
    const el = makeBanner();
    renderMemoryBanner(el, {
      backend: "cognee",
      degraded: true,
      reason: "<img src=x onerror=alert(1)>",
    });
    expect(el.querySelector("img")).toBeNull();
    expect(el.textContent).toContain("<img src=x onerror=alert(1)>");
  });

  it("hides again once the backend recovers", () => {
    const el = makeBanner();
    renderMemoryBanner(el, { backend: "cognee", degraded: true });
    expect(el.classList.contains("visible")).toBe(true);
    renderMemoryBanner(el, { backend: "cognee", degraded: false });
    expect(el.classList.contains("visible")).toBe(false);
  });
});
