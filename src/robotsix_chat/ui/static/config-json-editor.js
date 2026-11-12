// Multi-line, pretty-printed JSON editor for object/array config settings.
//
// The @robotsix/ui config renderer emits every free-form object or list
// setting (e.g. `llmio_tier_overrides`) as a single-line
// `<input type="text" data-json="1">` holding compact JSON, which is
// unreadable for any non-trivial value.  The renderer has no per-field widget
// override, so after it mounts we pair each such input with a monospace
// `<textarea>` showing `JSON.stringify(value, null, 2)`.
//
// The original input stays in the DOM (turned into `type="hidden"`) and keeps
// its `data-key` + `data-json="1"`: the panel's collector only JSON-parses
// an `HTMLInputElement` carrying `data-json="1"`, so it remains the single
// source the collector reads.  Every valid edit is written back to it as
// compact JSON, so the persisted value is the parsed object — formatting is
// presentation-only.  An invalid edit leaves the hidden input unparseable (the
// collector then omits the field, keeping the stored value), flags the editor
// with an inline error, and blocks the Save button until it is fixed.
//
// Extracted from chat.js so the behaviour is unit-testable.

const UPGRADED_ATTR = "data-json-editor";
const MIN_ROWS = 6;
const MAX_HEIGHT_PX = 480;

/** Pretty-print a compact JSON string; returns the input unchanged if unparseable. */
export function prettyJson(raw) {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch (_err) {
    return raw;
  }
}

/**
 * Validate *text* as JSON.  Returns `{ ok: true, value }` or
 * `{ ok: false, error }` with a human-readable message.
 */
export function validateJson(text) {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (err) {
    return { ok: false, error: (err && err.message) || "Invalid JSON" };
  }
}

function autoGrow(ta) {
  ta.style.height = "auto";
  const next = Math.min(ta.scrollHeight, MAX_HEIGHT_PX);
  if (next > 0) ta.style.height = next + "px";
}

/**
 * Upgrade one renderer JSON input to a multi-line editor.  No-op for anything
 * that is not an un-upgraded `<input type="text" data-json="1" data-key>`.
 *
 * @param {HTMLInputElement} input
 * @returns {HTMLTextAreaElement|null} the editor, or null when skipped
 */
export function upgradeConfigJsonField(input) {
  if (!input || input.tagName !== "INPUT" || input.type !== "text") return null;
  if (!input.getAttribute("data-key") || input.dataset.json !== "1") return null;
  if (input.getAttribute(UPGRADED_ATTR) === "1") return null;
  input.setAttribute(UPGRADED_ATTR, "1");

  const ta = document.createElement("textarea");
  ta.className = "rsu-config-value rsu-config-json-editor";
  ta.rows = MIN_ROWS;
  ta.spellcheck = false;
  ta.wrap = "off";
  ta.disabled = input.disabled;
  ta.setAttribute("aria-label", input.getAttribute("data-key"));
  ta.value = prettyJson(input.value);

  const errorEl = document.createElement("div");
  errorEl.className = "rsu-config-json-error";
  errorEl.setAttribute("role", "alert");
  errorEl.hidden = true;

  input.type = "hidden";
  input.after(ta, errorEl);

  const validate = function () {
    const res = validateJson(ta.value);
    if (res.ok) {
      ta.classList.remove("rsu-config-json-invalid");
      ta.removeAttribute("aria-invalid");
      errorEl.hidden = true;
      errorEl.textContent = "";
      input.value = JSON.stringify(res.value);
    } else {
      ta.classList.add("rsu-config-json-invalid");
      ta.setAttribute("aria-invalid", "true");
      errorEl.hidden = false;
      errorEl.textContent = "Invalid JSON — not saved: " + res.error;
      // Keep the hidden input unparseable so the collector omits this field
      // rather than persisting a stale or corrupted value.
      input.value = ta.value;
    }
    return res.ok;
  };

  const onEdit = function (evt) {
    validate();
    autoGrow(ta);
    // Re-dispatch on the collected input so the renderer's own onChange
    // (wired on [data-key]) marks the panel dirty and enables Save.
    input.dispatchEvent(new Event(evt.type, { bubbles: true }));
  };
  ta.addEventListener("input", onEdit);
  ta.addEventListener("change", onEdit);
  ta.addEventListener("blur", validate);

  autoGrow(ta);
  return ta;
}

/** True when any JSON editor inside *root* currently holds invalid JSON. */
export function hasInvalidJsonEditor(root) {
  return !!(root && root.querySelector(".rsu-config-json-editor.rsu-config-json-invalid"));
}

function upgradeAll(root) {
  if (root.matches && root.matches('input[data-json="1"][data-key]')) {
    upgradeConfigJsonField(root);
  }
  if (root.querySelectorAll) {
    const inputs = root.querySelectorAll('input[data-json="1"][data-key]');
    for (let i = 0; i < inputs.length; i++) upgradeConfigJsonField(inputs[i]);
  }
}

/**
 * Upgrade every JSON input under *panel* now and on every re-render, and
 * block Save while any editor holds invalid JSON.
 *
 * @param {HTMLElement} panel - the config panel mount element
 * @returns {MutationObserver}
 */
export function wireConfigJsonEditors(panel) {
  const observer = new MutationObserver(function (mutations) {
    for (let i = 0; i < mutations.length; i++) {
      const nodes = mutations[i].addedNodes;
      for (let j = 0; j < nodes.length; j++) {
        const node = nodes[j];
        if (node && node.nodeType === 1) upgradeAll(node);
      }
    }
  });
  observer.observe(panel, { childList: true, subtree: true });
  upgradeAll(panel);

  // Capture phase: runs before the renderer's own Save handler.
  panel.addEventListener(
    "click",
    function (evt) {
      const target = evt.target;
      if (!target || !target.closest || !target.closest(".rsu-config-save")) return;
      if (!hasInvalidJsonEditor(panel)) return;
      evt.preventDefault();
      evt.stopImmediatePropagation();
      const bad = panel.querySelector(".rsu-config-json-editor.rsu-config-json-invalid");
      if (bad && bad.focus) bad.focus();
    },
    true,
  );
  return observer;
}
