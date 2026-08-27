// Memory-backend degradation banner.
//
// The cognee memory backend fails in ways that are invisible from the chat:
// recall() returns "" on any fault, so replies keep streaming — just without
// any recalled context, and with nothing being written back. On 2026-08-26
// the graph store was unopenable for 16 hours while the UI showed no sign of
// it. This renders the backend's own `degraded` flag (from GET /health) as a
// banner so that state is visible rather than inferred from container logs.
//
// Extracted from chat.js so the render decision is unit-testable.

/**
 * Render the memory-degradation banner into `el`.
 *
 * Hides the banner when the backend is healthy or the payload is missing —
 * an absent `memory` block means the server did not report one, which is not
 * evidence of a fault.
 *
 * @param {HTMLElement|null} el      - banner root (expects a `.msg` child)
 * @param {Object|null} memory       - the `memory` block of GET /health
 * @returns {boolean} whether the banner is now visible
 */
export function renderMemoryBanner(el, memory) {
  if (!el) return false;
  const msgEl = el.querySelector(".msg");
  if (!msgEl) return false;

  if (!memory || !memory.degraded) {
    el.classList.remove("visible");
    return false;
  }

  const backend = memory.backend || "memory";
  msgEl.textContent = "";

  const lead = document.createElement("strong");
  lead.textContent = "Memory (" + backend + ") is not working.";
  msgEl.appendChild(lead);
  msgEl.appendChild(
    document.createTextNode(
      " Replies continue without recall — nothing is being remembered from " +
      "this conversation."
    )
  );

  if (memory.reason) {
    const detail = document.createElement("span");
    detail.className = "detail";
    // textContent, not innerHTML: the reason embeds a raw exception string.
    detail.textContent = String(memory.reason);
    msgEl.appendChild(document.createElement("br"));
    msgEl.appendChild(detail);
  }

  el.classList.add("visible");
  return true;
}
