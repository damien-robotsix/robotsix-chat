// Desktop notification helper wrapping the standard Notification API.
//
// Extracted so the permission-gating and availability-fallback logic is
// unit-testable and shared.  Callers can rely on notify() to never throw,
// regardless of whether the browser supports the Notification API or the
// user has already denied permission — an unavailable or unauthorized
// notification simply resolves to a no-op (returns null).

function resolveScope(win) {
  if (win) return win;
  if (typeof window !== "undefined") return window;
  if (typeof globalThis !== "undefined") return globalThis;
  return null;
}

/**
 * Whether the current context exposes the standard Notification API.
 *
 * @param {Object} [win] - a Window-like object (defaults to the global one)
 * @returns {boolean} true when `win.Notification` is a usable constructor
 */
export function isNotificationSupported(win) {
  const scope = resolveScope(win);
  return !!(scope && "Notification" in scope && typeof scope.Notification === "function");
}

/**
 * Resolve the current notification permission, never throwing.
 *
 * @param {Object} [win] - a Window-like object (defaults to the global one)
 * @returns {string} "unsupported" when the API is missing, otherwise the
 *   Notification.permission value ("default" | "granted" | "denied")
 */
export function getNotificationPermission(win) {
  if (!isNotificationSupported(win)) return "unsupported";
  const scope = resolveScope(win);
  return scope.Notification.permission || "default";
}

/**
 * Request notification permission, without ever re-prompting once the user
 * has already decided.
 *
 * The browser only allows `requestPermission` from a user-gesture context
 * (e.g. a click handler), so callers should invoke this from a click handler
 * rather than on page load.  Returns a Promise resolving to the resulting
 * permission string ("granted" | "denied" | "default"), or "unsupported"
 * when the API is unavailable.
 *
 * @param {Object} [win] - a Window-like object (defaults to the global one)
 * @returns {Promise<string>}
 */
export function requestNotificationPermission(win) {
  const scope = resolveScope(win);
  if (!scope || !("Notification" in scope)) return Promise.resolve("unsupported");

  const current = scope.Notification.permission;
  // Already decided — never re-prompt a granted or denied user.
  if (current === "granted" || current === "denied") {
    return Promise.resolve(current);
  }
  if (typeof scope.Notification.requestPermission !== "function") {
    return Promise.resolve("unsupported");
  }

  // requestPermission resolves to a Promise in modern browsers but used a
  // callback in older ones; handle both without throwing.
  return new Promise((resolve) => {
    let settled = false;
    const done = (result) => {
      if (!settled) {
        settled = true;
        resolve(result);
      }
    };
    let out;
    try {
      out = scope.Notification.requestPermission(done);
    } catch (err) {
      done("unsupported");
      return;
    }
    if (out && typeof out.then === "function") {
      out.then(done, () => done("unsupported"));
    }
  });
}

/**
 * Show a desktop notification, no-op when unavailable or unauthorized.
 *
 * `tag` is passed through to the Notification constructor so the OS can
 * replace or de-duplicate an existing notification with the same tag.  When
 * `onClick` is provided it fires with the Notification instance, and the
 * notification is closed afterwards.
 *
 * @param {Object} opts
 * @param {string} [opts.title]    - notification title
 * @param {string} [opts.body]     - notification body text
 * @param {string} [opts.tag]      - de-duplication tag (OS-level replacement)
 * @param {Function} [opts.onClick] - called with the Notification on click
 * @param {Object} [win]           - a Window-like object (defaults to global)
 * @returns {Notification|null} the shown notification, or null when it was
 *   not shown (unsupported API or permission not granted)
 */
export function notify({ title, body, tag, onClick } = {}, win) {
  const scope = resolveScope(win);
  if (!scope || !("Notification" in scope)) return null;
  if (scope.Notification.permission !== "granted") return null;

  let notification;
  try {
    notification = new scope.Notification(title || "Notification", {
      body: body || "",
      tag: tag || "",
    });
  } catch (err) {
    return null;
  }

  if (onClick) {
    notification.onclick = function () {
      try {
        onClick(notification);
      } finally {
        notification.close();
      }
    };
  }
  return notification;
}
