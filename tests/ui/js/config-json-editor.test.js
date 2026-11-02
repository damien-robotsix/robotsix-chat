import { describe, it, expect, beforeEach } from "vitest";
import {
  prettyJson,
  validateJson,
  upgradeConfigJsonField,
  hasInvalidJsonEditor,
  wireConfigJsonEditors,
} from "../../../src/robotsix_chat/ui/static/config-json-editor.js";

const OVERRIDES = {
  fallback: { level2: { model: "openrouter/x/y", max_tokens: 131072 } },
};

// Mirror of the @robotsix/ui renderer's `buildJsonRow` output.
function makePanel(value) {
  const panel = document.createElement("div");
  panel.className = "rsu-config-panel";
  panel.innerHTML =
    '<div class="rsu-config-row">' +
    '<span class="rsu-config-key">llmio_tier_overrides</span>' +
    '<input type="text" class="rsu-config-value" data-key="llmio_tier_overrides"' +
    ' data-json="1" spellcheck="false">' +
    '<span class="rsu-config-hint">JSON object</span>' +
    "</div>" +
    '<button type="button" class="rsu-btn rsu-config-save" disabled>Save</button>';
  panel.querySelector("input").value = JSON.stringify(value);
  document.body.appendChild(panel);
  return panel;
}

// Mirror of the @robotsix/ui collector's `readField` for a data-json input.
const OMIT = Symbol("omit");
function collect(panel, key) {
  const el = panel.querySelector(`[data-key="${key}"]`);
  if (el instanceof HTMLInputElement && el.dataset.json === "1") {
    try {
      return JSON.parse(el.value);
    } catch {
      return OMIT;
    }
  }
  return el ? el.value : OMIT;
}

function type(ta, text) {
  ta.value = text;
  ta.dispatchEvent(new Event("input", { bubbles: true }));
}

describe("config JSON editor", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  it("prettyJson indents with two spaces and passes garbage through", () => {
    expect(prettyJson('{"a":{"b":1}}')).toBe('{\n  "a": {\n    "b": 1\n  }\n}');
    expect(prettyJson("{oops")).toBe("{oops");
  });

  it("validateJson reports errors", () => {
    expect(validateJson('{"a":1}')).toEqual({ ok: true, value: { a: 1 } });
    expect(validateJson("{a:1}").ok).toBe(false);
  });

  it("renders an object setting as pretty-printed multi-line JSON", () => {
    const panel = makePanel(OVERRIDES);
    const ta = upgradeConfigJsonField(panel.querySelector("input"));
    expect(ta).not.toBeNull();
    expect(ta.tagName).toBe("TEXTAREA");
    expect(ta.classList.contains("rsu-config-json-editor")).toBe(true);
    expect(ta.value).toBe(JSON.stringify(OVERRIDES, null, 2));
    expect(ta.value.split("\n").length).toBeGreaterThan(1);
    expect(ta.rows).toBeGreaterThanOrEqual(6);
    // The "JSON object" hint is kept, and the collected input is hidden.
    expect(panel.querySelector(".rsu-config-hint").textContent).toBe("JSON object");
    expect(panel.querySelector('[data-key="llmio_tier_overrides"]').type).toBe("hidden");
    // Collected value is unchanged by the upgrade.
    expect(collect(panel, "llmio_tier_overrides")).toEqual(OVERRIDES);
  });

  it("leaves scalar text inputs alone and is idempotent", () => {
    const input = document.createElement("input");
    input.type = "text";
    input.setAttribute("data-key", "log_level");
    expect(upgradeConfigJsonField(input)).toBeNull();
    const panel = makePanel({});
    const json = panel.querySelector("input");
    expect(upgradeConfigJsonField(json)).not.toBeNull();
    expect(upgradeConfigJsonField(json)).toBeNull();
    expect(panel.querySelectorAll("textarea").length).toBe(1);
  });

  it("rejects invalid JSON inline and does not persist it", () => {
    const panel = makePanel(OVERRIDES);
    wireConfigJsonEditors(panel);
    const ta = panel.querySelector("textarea");
    type(ta, '{"fallback": ');
    const err = panel.querySelector(".rsu-config-json-error");
    expect(err.hidden).toBe(false);
    expect(err.textContent).toContain("Invalid JSON");
    expect(ta.getAttribute("aria-invalid")).toBe("true");
    expect(hasInvalidJsonEditor(panel)).toBe(true);
    // Collector omits the field, so the stored value survives.
    expect(collect(panel, "llmio_tier_overrides")).toBe(OMIT);
    // Save is blocked before the renderer's own handler runs.
    const save = panel.querySelector(".rsu-config-save");
    save.disabled = false;
    let saved = false;
    save.addEventListener("click", () => {
      saved = true;
    });
    save.click();
    expect(saved).toBe(false);
  });

  it("saves a valid edit as the parsed object", () => {
    const panel = makePanel(OVERRIDES);
    wireConfigJsonEditors(panel);
    const hidden = panel.querySelector('[data-key="llmio_tier_overrides"]');
    let changes = 0;
    hidden.addEventListener("input", () => {
      changes += 1;
    });
    const ta = panel.querySelector("textarea");
    type(ta, "{oops");
    const next = { fallback: { level2: { model: "m", max_tokens: 8 } }, extra: [1, 2] };
    type(ta, JSON.stringify(next, null, 4));
    expect(changes).toBe(2);
    expect(panel.querySelector(".rsu-config-json-error").hidden).toBe(true);
    expect(hasInvalidJsonEditor(panel)).toBe(false);
    expect(hidden.value).toBe(JSON.stringify(next));
    expect(collect(panel, "llmio_tier_overrides")).toEqual(next);
    const save = panel.querySelector(".rsu-config-save");
    let saved = false;
    save.disabled = false;
    save.addEventListener("click", () => {
      saved = true;
    });
    save.click();
    expect(saved).toBe(true);
  });

  it("upgrades JSON fields added by a re-render", async () => {
    const panel = document.createElement("div");
    document.body.appendChild(panel);
    wireConfigJsonEditors(panel);
    const fresh = makePanel([1, 2]);
    panel.appendChild(fresh);
    await new Promise((r) => setTimeout(r, 0));
    const ta = panel.querySelector("textarea.rsu-config-json-editor");
    expect(ta).not.toBeNull();
    expect(ta.value).toBe("[\n  1,\n  2\n]");
  });
});
