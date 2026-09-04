// Focus-aware desktop-notification gating for the SSE-driven chat UI.
//
// The live SSE update channel (the /events stream and the /chat POST
// response) already drives conversation and subsession updates.  Whether a
// newly-arrived message warrants a desktop notification depends on whether
// the user is actively viewing the target: a notification is suppressed when
// the browser tab is visible AND the target is the one the user is currently
// looking at.  These helpers are pure (no DOM side effects) so they can be
// unit-tested in Node.

/**
 * Whether the current browser tab is visible.
 *
 * A notification is pointless (and intrusive) when the tab is in the
 * foreground because the new message is already on screen.  Treat an
 * environment without the `hidden` property as visible.
 *
 * @param {Object} [doc] - a Document-like object (defaults to the global one)
 * @returns {boolean} true when the tab is visible
 */
export function isDocumentVisible(doc) {
  const d = doc || (typeof document !== "undefined" ? document : null);
  if (!d) return false;
  return !d.hidden;
}

/**
 * Whether a new main-conversation message should raise a desktop
 * notification.
 *
 * The main conversation is "actively focused" (already on screen) only when
 * the tab is visible and no subsession is in focus mode.  When it is on
 * screen a new message is already visible, so no notification is needed.
 *
 * @param {Object} opts
 * @param {boolean} opts.docVisible        - is the browser tab visible?
 * @param {boolean} opts.subsessionFocused - is a subsession in focus mode?
 * @returns {boolean} true when the notification should be raised
 */
export function shouldNotifyMainConversation({ docVisible, subsessionFocused }) {
  return !(docVisible && !subsessionFocused);
}

/**
 * Whether a new subsession update should raise a desktop notification.
 *
 * A subsession is "actively viewed" (already on screen) when the tab is
 * visible AND either (a) this specific subsession is the one in focus mode,
 * or (b) its row is expanded and the side-chat panel is visible — the same
 * on-screen signal the unread-badge logic uses to treat a message as read.
 * When it is on screen a new message is already visible, so no notification
 * is needed.
 *
 * @param {Object} opts
 * @param {boolean} opts.docVisible - is the browser tab visible?
 * @param {boolean} opts.focused    - is THIS subsession the focused one?
 * @param {boolean} opts.visible    - is THIS subsession's row expanded and
 *                                    its panel visible (on screen)?
 * @returns {boolean} true when the notification should be raised
 */
export function shouldNotifySubsession({ docVisible, focused, visible }) {
  return !(docVisible && (focused || visible));
}
