// Desktop-notification click routing.
//
// When a desktop notification is clicked the user expects the app to come to
// the foreground and jump straight to the conversation the notification was
// about.  The navigation primitives themselves (focusing the window/tab,
// switching the main conversation, focusing a subsession) live inside the
// chat IIFE and touch the DOM, so they are injected here as an `app` object.
// Keeping this module dependency-free makes the routing logic unit-testable
// in isolation.
//
// The injected `app` must provide:
//   - focusWindow()              — bring the browser window/tab to the front
//   - openMainConversation(sid)  — open/focus the given main conversation
//   - openSubsession(subId)      — open/focus the given user_chat subsession
//
// notify() closes the clicked notification itself (see notify.js), so these
// handlers only need to perform the navigation.

/**
 * Build the onClick handler for a MAIN-conversation notification.
 *
 * Focuses the window/tab, then opens or focuses the main conversation the
 * notification was raised for.
 *
 * @param {Object} app        - injected navigation primitives
 * @param {string} sessionId  - the session whose main conversation to open
 * @returns {Function} an onClick callback for notify()
 */
export function buildMainConversationClick(app, sessionId) {
  return function () {
    app.focusWindow();
    app.openMainConversation(sessionId);
  };
}

/**
 * Build the onClick handler for a SUBSESSION notification.
 *
 * Focuses the window/tab, then opens or focuses the specific `user_chat`
 * subsession identified by the notification.
 *
 * @param {Object} app            - injected navigation primitives
 * @param {string} subsessionId   - the user_chat subsession to open
 * @returns {Function} an onClick callback for notify()
 */
export function buildSubsessionClick(app, subsessionId) {
  return function () {
    app.focusWindow();
    app.openSubsession(subsessionId);
  };
}
