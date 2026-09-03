import { processSSEStream } from "./sse-parser.js";
import { drainSessionDraft } from "./drain-draft.js";
import { renderMemoryBanner } from "./memory-banner.js";
import {
  parseSuggestions,
  stripStreamingSuggestions,
  renderSuggestionChips,
  disableStaleSuggestionChips,
} from "./suggestions.js";

// ---- AppShell initialization ------------------------------------------
// Mounts the shared fleet chrome (mountAppShell from @robotsix/ui).
// Runs asynchronously — the IIFE below captures DOM refs while the
// controls are still in the hidden #header-controls template; once the
// AppShell is mounted the controls are moved into its right slot.
(async function initAppShell() {
  const mount = document.getElementById("appshell-mount");
  if (!mount) return;
  try {
    const { mountAppShell } = await import("/static/vendor/vanilla.js");
    const projectTitle =
      document
        .querySelector('meta[name="project-title"]')
        ?.getAttribute("content") || "robotsix-chat";

    const handle = mountAppShell(mount, {
      brand: projectTitle,
      navItems: [],
      settingsHref: "#",
    });

    // Move per-component controls from the hidden template into the
    // AppShell's right slot so their existing event handlers stay attached.
    const controls = document.getElementById("header-controls");
    if (controls) {
      while (controls.firstChild) {
        handle.rightSlot.appendChild(controls.firstChild);
      }
      controls.remove();
    }

    // The AppShell provides its own Settings entry — hide the
    // standalone settings toggle button to avoid duplicate controls.
    var settingsToggle = document.getElementById("settings-toggle");
    if (settingsToggle) settingsToggle.style.display = "none";

    // Intercept the AppShell Settings link to open the side panel instead
    // of navigating to '#'.
    const settingsLink = handle.element.querySelector(
      ".rsu-appshell-settings"
    );
    if (settingsLink) {
      settingsLink.addEventListener("click", function (e) {
        e.preventDefault();
        // openSettingsPanel is defined inside the IIFE — reach it via the
        // global internal registry so this top-level module code can call
        // into the IIFE's closure.
        var fn = window.__chatOpenSettingsPanel;
        if (typeof fn === "function") fn();
      });
    }

    // Publish the AppShell root for CSS selectors that need to target it
    // (e.g. side-panel margin push).
    handle.element.id = "appshell-header";
  } catch (_err) {
    // AppShell unavailable (vendor assets missing — vendor-ui.sh not
    // run, or Docker image built without the UI stage).  Show the
    // fallback controls in-place so the chat remains usable.
    var ctl = document.getElementById("header-controls");
    if (ctl) ctl.style.display = "";
  }
})();

(function () {
  "use strict";

  // ---- DOM refs --------------------------------------------------------
  const chatEl       = document.getElementById("chat");
  const msgInput     = document.getElementById("msg-input");
  const sendBtn      = document.getElementById("send-btn");
  const errorBanner  = document.getElementById("error-banner");
  const errorMsgEl   = errorBanner.querySelector(".msg");
  const errorDismiss = errorBanner.querySelector(".dismiss");
  const connDot      = document.getElementById("connection-dot");
  const sessionsToggle  = document.getElementById("sessions-toggle");
  const sessionsPanel   = document.getElementById("sessions-panel");
  const sessionsDismiss = sessionsPanel.querySelector(".dismiss");
  const sessionsResizeHandle = document.getElementById("sessions-resize-handle");
  const newChatBtn = document.getElementById("new-chat-btn");
  const notificationsToggle  = document.getElementById("notifications-toggle");
  const notificationsPanel   = document.getElementById("notifications-panel");
  const notificationsBadge   = document.getElementById("notifications-badge");
  const notificationsList    = document.getElementById("notifications-list");
  const notificationsDismiss = notificationsPanel
    ? notificationsPanel.querySelector(".dismiss")
    : null;
  const subsToggle     = document.getElementById("subsessions-toggle");
  const subsPanel      = document.getElementById("subsessions-panel");
  const subsResizeHandle = document.getElementById("subsessions-resize-handle");
  const subsList       = document.getElementById("subsessions-list");
  const attachBtn      = document.getElementById("attach-btn");
  const fileInput      = document.getElementById("file-input");
  const previewTray    = document.getElementById("preview-tray");
  const attachErrorEl  = document.getElementById("attach-error");
  const cancelQueuedBtn = document.getElementById("cancel-queued-btn");

  // ---- State -----------------------------------------------------------
  var state = "idle";          // idle | sending | streaming | error
  var currentAssistantBubble = null;  // the <div> receiving tokens
  var rawAssistantText       = "";    // accumulated raw text for markdown rendering
  var typingIndicatorEl      = null;  // the animated dots element
  var lastModelTimestampEl   = null;  // timestamp element for last model message
  var messageQueue = [];       // FIFO queue of { text, el } for busy-state
  var unreadNotifications = []; // unread notification records for the badge/panel
  // (currentRequestSessionId removed — unused; cross-session guard uses
  //  the requestSessionId captured inside doPost instead.)

  // ---- Live re-attach state -------------------------------------------
  // The foreground turn's live tokens arrive on the POST /chat response body
  // (rendered by doPost) AND are mirrored onto /events. To avoid double
  // rendering, the tab that owns the in-flight POST renders from the POST and
  // ignores the /events echo; any other view (a second tab, or this tab after
  // switching away and back) renders the turn from /events instead.
  var activePostAbort = null;      // AbortController for the in-flight POST
  var activePostSessionId = null;  // session that POST belongs to (null = none)
  var reattachActive = false;      // rendering an in-flight turn via /events
  var reattachTurnId = null;       // turn_id currently being re-attached

  // ---- Image attachments -----------------------------------------------
  var MAX_IMAGES = 8;
  var MAX_FILE_BYTES = 5 * 1024 * 1024;  // 5 MiB
  var ALLOWED_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  var pendingImages = [];  // { file, objectURL, mediaType }

  function clearAttachError() {
    attachErrorEl.classList.remove("visible");
    attachErrorEl.textContent = "";
  }

  function showAttachError(msg) {
    attachErrorEl.textContent = msg;
    attachErrorEl.classList.add("visible");
  }

  function removeAttachment(index) {
    var item = pendingImages[index];
    if (item && item.objectURL) URL.revokeObjectURL(item.objectURL);
    pendingImages.splice(index, 1);
    renderPreviewTray();
    clearAttachError();
  }

  function renderPreviewTray() {
    previewTray.innerHTML = "";
    if (pendingImages.length === 0) {
      previewTray.classList.remove("has-images");
      return;
    }
    previewTray.classList.add("has-images");
    for (var i = 0; i < pendingImages.length; i++) {
      var item = pendingImages[i];
      var wrap = document.createElement("div");
      wrap.className = "preview-item";

      var img = document.createElement("img");
      img.src = item.objectURL;
      img.alt = item.file.name;
      wrap.appendChild(img);

      var rm = document.createElement("button");
      rm.className = "remove-btn";
      rm.textContent = "\u00d7";
      rm.title = "Remove " + item.file.name;
      rm.setAttribute("aria-label", "Remove " + item.file.name);
      // capture index in closure
      (function (idx) {
        rm.addEventListener("click", function (e) {
          e.stopPropagation();
          removeAttachment(idx);
        });
      })(i);
      wrap.appendChild(rm);

      previewTray.appendChild(wrap);
    }
  }

  function validateAndAddFiles(files) {
    clearAttachError();
    var accepted = [];
    for (var i = 0; i < files.length; i++) {
      var file = files[i];
      if (ALLOWED_TYPES.indexOf(file.type) === -1) {
        showAttachError("Unsupported file type: " + (file.type || "unknown") +
                        ". Use PNG, JPEG, GIF, or WebP.");
        continue;
      }
      if (file.size > MAX_FILE_BYTES) {
        showAttachError("\"" + file.name + "\" is too large (" +
                        (file.size / 1024 / 1024).toFixed(1) +
                        " MiB). Maximum is 5 MiB.");
        continue;
      }
      if (pendingImages.length + accepted.length >= MAX_IMAGES) {
        showAttachError("Maximum " + MAX_IMAGES + " images allowed.");
        break;
      }
      accepted.push(file);
    }

    for (var j = 0; j < accepted.length; j++) {
      var f = accepted[j];
      var objectURL = URL.createObjectURL(f);
      var entry = { file: f, objectURL: objectURL, mediaType: f.type, _b64: null };
      pendingImages.push(entry);
      // Eagerly encode so saveDraft() can work synchronously.
      encodeImage(f).then(function (encoded) {
        entry._b64 = { media_type: encoded.media_type, data: encoded.data, filename: f.name };
      });
    }

    renderPreviewTray();
  }

  function encodeImage(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        // readAsArrayBuffer returns the raw bytes; convert to binary string
        // then btoa for base64 WITHOUT any data: prefix.
        var bytes = new Uint8Array(reader.result);
        var binary = "";
        for (var i = 0; i < bytes.length; i++) {
          binary += String.fromCharCode(bytes[i]);
        }
        var b64 = btoa(binary);
        resolve({ media_type: file.type, data: b64 });
      };
      reader.onerror = function () { reject(new Error("Failed to read file")); };
      reader.readAsArrayBuffer(file);
    });
  }

  function clearPendingImages() {
    pendingImages = [];
    renderPreviewTray();
    clearAttachError();
  }

  // ---- Draft persistence (queued messages + pending images) ------------

  /** Convert a base64 string back to a pending-image entry. */
  function _base64ToPendingImage(b64, mediaType, filename) {
    try {
      var binary = atob(b64);
      var bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      var blob = new Blob([bytes], { type: mediaType });
      var file = new File([blob], filename, { type: mediaType });
      var objectURL = URL.createObjectURL(blob);
      // Eager encode so subsequent saveDraft calls work without FileReader.
      var entry = { file: file, objectURL: objectURL, mediaType: mediaType, _b64: { media_type: mediaType, data: b64, filename: filename } };
      return entry;
    } catch (_) {
      return null;
    }
  }

  /** Save the current messageQueue and pendingImages to the backend. */
  function saveDraft() {
    if (!activeSessionId) return;

    var pending = [];
    for (var i = 0; i < pendingImages.length; i++) {
      if (pendingImages[i]._b64) {
        pending.push(pendingImages[i]._b64);
      }
    }

    var queue = [];
    for (var j = 0; j < messageQueue.length; j++) {
      var item = messageQueue[j];
      var qImgs = [];
      var imgs = item.images || [];
      for (var k = 0; k < imgs.length; k++) {
        if (imgs[k]._b64) {
          qImgs.push(imgs[k]._b64);
        }
      }
      queue.push({ text: item.text, images: qImgs, messageId: item.messageId });
    }

    // Always PUT — even an empty payload clears the server-side draft
    // so already-sent messages never re-appear on the next restore.
    var body = JSON.stringify({ pending_images: pending, queue: queue });
    fetch(apiBase() + "/sessions/" + encodeURIComponent(activeSessionId) + "/draft", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: body,
      keepalive: true
    }).catch(function () { /* best-effort */ });
  }

  /** Restore a previously saved draft from the backend. */
  function restoreDraft() {
    if (!activeSessionId) return;

    fetch(apiBase() + "/sessions/" + encodeURIComponent(activeSessionId) + "/draft")
      .then(function (r) {
        if (!r.ok) return {};
        return r.json();
      })
      .then(function (draft) {
        if (!draft) return;

        // Restore pending images.
        if (Array.isArray(draft.pending_images) && draft.pending_images.length > 0) {
          for (var i = 0; i < draft.pending_images.length; i++) {
            var img = draft.pending_images[i];
            var pi = _base64ToPendingImage(img.data, img.media_type, img.filename || "image");
            if (pi) pendingImages.push(pi);
          }
          renderPreviewTray();
        }

        // Restore queued messages.
        if (Array.isArray(draft.queue) && draft.queue.length > 0) {
          for (var j = 0; j < draft.queue.length; j++) {
            var qItem = draft.queue[j];
            if (!qItem.text && (!qItem.images || qItem.images.length === 0)) continue;

            var el = addUserBubble(qItem.text || "");
            var restoredImages = [];
            if (Array.isArray(qItem.images) && qItem.images.length > 0) {
              var imgsDiv = document.createElement("div");
              imgsDiv.className = "bubble-images";
              for (var k = 0; k < qItem.images.length; k++) {
                var qImg = qItem.images[k];
                var pi = _base64ToPendingImage(qImg.data, qImg.media_type, qImg.filename || "image");
                if (!pi) continue;
                restoredImages.push(pi);
                var thumb = document.createElement("img");
                thumb.src = pi.objectURL;
                thumb.alt = pi.file.name;
                imgsDiv.appendChild(thumb);
              }
              if (imgsDiv.children.length > 0) {
                el.insertBefore(imgsDiv, el.firstChild);
              }
            }

            el.classList.add("queued");
            addCancelButton(el, qItem.messageId);
            messageQueue.push({ text: qItem.text, el: el, images: restoredImages, messageId: qItem.messageId });
          }
          updateCancelQueuedButton();
          // Automatically dispatch queued messages restored from draft when
          // the session regains focus — but NOT while a turn is in flight
          // (isBusy is true when we've re-attached to an ongoing turn via
          // /events). In that case handleReattachDone drains once it finishes.
          if (!isBusy()) drainQueue();
        }
      })
      .catch(function () { /* best-effort */ });
  }

  function isBusy() {
    return state === "sending" || state === "streaming";
  }

  // ---- Idle timeout (minutes; 0 = disabled) ----------------------------
  var IDLE_TIMEOUT_MINUTES = parseInt(document.querySelector('meta[name="idle-timeout-minutes"]').content, 10) || 0;
  var idleTimerId = null;

  function resetIdleTimer() {
    if (IDLE_TIMEOUT_MINUTES <= 0) return;
    if (idleTimerId) clearTimeout(idleTimerId);
    idleTimerId = setTimeout(restartConversation, IDLE_TIMEOUT_MINUTES * 60 * 1000);
  }

  function restartConversation() {
    idleTimerId = null;
    // Do NOT clear existing chat history — idle timeout adds an inline
    // notice so the user can still scroll back through the conversation.
    currentAssistantBubble = null;
    messageQueue = [];
    reattachActive = false;
    reattachTurnId = null;
    hideTypingIndicator();
    state = "idle";
    updateSendBusy();
    // Append a brief italic notice so the user knows what happened.
    var notice = document.createElement("div");
    notice.className = "bubble assistant";
    notice.style.fontStyle = "italic";
    notice.textContent = "You were idle for "
                       + IDLE_TIMEOUT_MINUTES + " minute(s) — "
                       + "the conversation has been compacted. "
                       + "Your previous messages are still visible above.";
    chatEl.appendChild(notice);
    scrollToBottom();
  }

  function serverUrl() {
    var origin = window.location.origin;
    // file:// origins report the string "null" — fall back to localhost.
    if (!origin || origin === "null") {
      return "http://localhost:8000/chat";
    }
    return origin + "/chat";
  }

  // ---- Conversation owner ----------------------------------------------
  // This deployment is single-user: there is no login and no per-browser
  // identity. Every access point is the same person, so the owner sent with
  // every request is a fixed constant — which is what makes the session list
  // identical on every computer, browser, and private window.
  //
  // Previously this was a random UUID minted into localStorage, so a second
  // computer (or cleared site data) silently became a *different* owner and
  // was served an empty session list. The server canonicalises any owner id
  // it receives (see canonical_owner_id in chat/conversation.py), so a stale
  // cached copy of this file still lands in the same pool.
  var PROJECT_TITLE = document.querySelector('meta[name="project-title"]').content;
  var OPERATOR_OWNER = "operator";  // MUST match OPERATOR_OWNER in chat/conversation.py
  var clientId = OPERATOR_OWNER;

  function randomId() {
    try {
      if (window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID();
      }
    } catch (_) {}
    return "c-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 10);
  }

  // ---- Session management (localStorage-backed) -----------------------
  var ACTIVE_SESSION_KEY = PROJECT_TITLE + "-active-session-id";
  var SUBS_PANEL_KEY = PROJECT_TITLE + "-subsessions-panel-visible";
  var SESSIONS_PANEL_KEY = PROJECT_TITLE + "-sessions-panel-visible";
  var UNREAD_SESSION_KEY = PROJECT_TITLE + "-unread-sessions";
  var activeSessionId = null;
  var sessionsList = [];        // cached session list from server

  // Periodic sessions are owned by a fixed pseudo-owner ("periodic"),
  // not by this browser's clientId. They are surfaced in the list (see
  // fetchSessions) and every per-session request must be scoped to their
  // real owner so history, replies, and the event stream reach them.
  var PERIODIC_OWNER = "periodic";
  // Keep in sync with DEFAULT_SCHEDULE_INTERVAL_SECONDS in
  // config/periodic_models.py.
  var DEFAULT_SCHEDULE_INTERVAL_SECONDS = 86400;
  function ownerFor(sid) {
    for (var oi = 0; oi < sessionsList.length; oi++) {
      var s = sessionsList[oi];
      if (s && s.session_id === sid) {
        // The owner the session was actually fetched under (tagged in
        // fetchSessions) routes per-session requests correctly.
        if (s._owner) return s._owner;
        return clientId;
      }
    }
    return clientId;
  }

  function getActiveSessionId() {
    try { return localStorage.getItem(ACTIVE_SESSION_KEY) || null; }
    catch (_) { return null; }
  }

  function setActiveSessionId(sid) {
    activeSessionId = sid;
    try { localStorage.setItem(ACTIVE_SESSION_KEY, sid); } catch (_) {}
  }

  function getSubsPanelVisible() {
    try { return localStorage.getItem(SUBS_PANEL_KEY) === "true"; }
    catch (_) { return false; }
  }

  function setSubsPanelVisible(visible) {
    try { localStorage.setItem(SUBS_PANEL_KEY, visible ? "true" : "false"); } catch (_) {}
  }

  function restoreSubsPanelState() {
    if (getSubsPanelVisible()) { openSubsessionsPanel(); }
  }

  // ---- Sessions panel visibility (localStorage-backed) ----------------
  function getSessionsPanelVisible() {
    try { return localStorage.getItem(SESSIONS_PANEL_KEY) !== "false"; }
    catch (_) { return true; }
  }

  function setSessionsPanelVisible(visible) {
    try { localStorage.setItem(SESSIONS_PANEL_KEY, visible ? "true" : "false"); } catch (_) {}
  }

  function restoreSessionsPanelState() {
    if (getSessionsPanelVisible()) {
      openSessionsPanel();
    } else {
      sessionsPanel.classList.remove("visible");
      hideSessionsResizeHandle();
    }
  }

  function openSessionsPanel() {
    sessionsPanel.classList.add("visible");
    positionSessionsResizeHandle();
    document.documentElement.style.setProperty('--sessions-width', sessionsPanel.getBoundingClientRect().width + 'px');
  }

  // ---- Unread session tracking (localStorage-backed) ------------------
  function getUnreadState() {
    try {
      var raw = localStorage.getItem(UNREAD_SESSION_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (_) { return {}; }
  }

  function setUnreadState(state) {
    try { localStorage.setItem(UNREAD_SESSION_KEY, JSON.stringify(state)); } catch (_) {}
  }

  function markSessionRead(sessionId) {
    // Reset the unread baseline for this session to its current turn_count,
    // clearing any highlight. Future increases will re-trigger highlighting.
    if (!sessionId) return;
    var state = getUnreadState();
    for (var i = 0; i < sessionsList.length; i++) {
      if (sessionsList[i].session_id === sessionId) {
        state[sessionId] = sessionsList[i].turn_count || 0;
        setUnreadState(state);
        return;
      }
    }
  }

  function updateUnreadFromList(sessions) {
    // Ensure every server-side session has a baseline entry so future
    // turn_count increases are detected. Sessions missing from the stored
    // state (new sessions, or sessions from a previous browsing session)
    // get their current turn_count as baseline; existing sessions keep
    // their stored (possibly lower) baseline so the unread highlight fires.
    var state = getUnreadState();
    var changed = false;
    for (var i = 0; i < sessions.length; i++) {
      var s = sessions[i];
      var sid = s.session_id;
      if (!(sid in state)) {
        state[sid] = s.turn_count || 0;
        changed = true;
      }
    }
    if (changed) { setUnreadState(state); }
  }

  function isSessionUnread(sessionId, turnCount) {
    if (sessionId === activeSessionId) return false;
    var state = getUnreadState();
    var lastSeen = state[sessionId];
    // Not tracked yet — treat as read (no prior baseline).
    if (lastSeen === undefined) return false;
    return turnCount > lastSeen;
  }

  // ---- Session API helpers --------------------------------------------
  function apiBase() {
    return serverUrl().replace(/\/chat$/, "");
  }

  function fetchSessions() {
    var base = apiBase();
    var mine = fetch(
      base + "/sessions?owner_id=" + encodeURIComponent(clientId),
      { method: "GET" }
    ).then(function (r) {
      if (!r.ok) throw new Error("Failed to fetch sessions");
      return r.json();
    });
    // Periodic sessions live under the "periodic" owner, not this
    // client — fetch them too so the operator can see and reply to them.
    // Best-effort: never let their absence break the normal session list.
    var auto = fetch(
      base + "/sessions?owner_id=" + encodeURIComponent(PERIODIC_OWNER),
      { method: "GET" }
    ).then(function (r) {
      return r.ok ? r.json() : { sessions: [] };
    }).catch(function () { return { sessions: [] }; });
    return Promise.all([mine, auto]).then(function (res) {
      var a = res[0] || {}, b = res[1] || {};
      var seen = {}, merged = [];
      // Tag each list with the owner it was fetched under so ownerFor(sid)
      // can route per-session requests (history, events, delete, ...) to the
      // correct owner.
      var lists = [
        { owner: clientId, sessions: a.sessions || [] },
        { owner: PERIODIC_OWNER, sessions: b.sessions || [] }
      ];
      for (var li = 0; li < lists.length; li++) {
        var ownerId = lists[li].owner;
        var arr = lists[li].sessions;
        for (var i = 0; i < arr.length; i++) {
          var s = arr[i];
          if (s && s.session_id && !seen[s.session_id]) {
            seen[s.session_id] = true;
            s._owner = ownerId;
            merged.push(s);
          }
        }
      }
      merged.sort(function (x, y) {
        return (y.last_active || 0) - (x.last_active || 0);
      });
      // active_session_id stays the client's own — a periodic session
      // must never silently become the default active view.
      return { sessions: merged, active_session_id: a.active_session_id };
    });
  }

  function createNewSession() {
    var url = apiBase() + "/sessions";
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner_id: clientId })
    }).then(function (r) {
      if (!r.ok) throw new Error("Failed to create session");
      return r.json();
    });
  }

  // ---- Active-session model badge --------------------------------------
  // Rendered in the header from whichever session-list entry is active, and
  // updated live by the `session_model` SSE frame so an escalation shows up
  // without waiting for the next session-list refetch.  While llmio's
  // provider failover is active (GET /models .failover), the badge switches
  // to a distinct state: every level is served by the fallback (OpenRouter)
  // provider until the window expires.
  var failoverState = null;   // GET /models .failover snapshot
  var lastModelBadge = null;  // {name, escalated} to re-render on state flips

  function renderActiveModel(name, escalated) {
    lastModelBadge = { name: name, escalated: escalated };
    var el = document.getElementById("active-model");
    if (!el) return;
    if (!name) {
      el.textContent = "";
      el.className = "";
      return;
    }
    var failover = !!(failoverState && failoverState.failover_active);
    var text = escalated ? name + " \u23eb" : name;
    if (failover) text += " \u26a0";
    el.textContent = text;
    var cls = escalated ? "model-badge model-badge-escalated" : "model-badge";
    if (failover) cls += " model-badge-failover";
    el.className = cls;
    if (failover) {
      var until = failoverState.failover_until
        ? new Date(failoverState.failover_until).toLocaleTimeString()
        : "soon";
      el.title =
        "Provider failover active — turns run on the backup provider " +
        "(OpenRouter); back to the default provider at " + until;
    } else {
      el.title = escalated
        ? "Escalated to a stronger model for this session"
        : "Model serving the active session";
    }
  }

  // ---- Per-session model selector --------------------------------------
  // Options are sourced from GET /models (robotsix-llmio's configured tiers,
  // never a hard-coded list). Selecting one pins the active session to that
  // level from its next turn via POST /sessions/{id}/model; the choice is
  // per-session and never leaks to other sessions.
  var modelOptions = [];        // [{level, name, provider, needs_api_key, available}]
  var defaultModelLevel = null; // server's configured chat level
  var pendingModelLevel = null; // active session's level, applied once options load

  function loadModelOptions() {
    fetch(apiBase() + "/models", { method: "GET" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || !Array.isArray(data.models)) return;
        modelOptions = data.models;
        defaultModelLevel = data.default_level;
        var wasActive = !!(failoverState && failoverState.failover_active);
        failoverState = data.failover || null;
        var isActive = !!(failoverState && failoverState.failover_active);
        populateModelSelector();
        // Re-render the badge when the failover state flips (the model
        // names themselves also change: the fallback slot serves them).
        if (wasActive !== isActive && lastModelBadge && lastModelBadge.name) {
          renderActiveModel(lastModelBadge.name, lastModelBadge.escalated);
        }
      })
      .catch(function () { /* selector stays empty; badge still works */ });
  }

  function populateModelSelector() {
    var sel = document.getElementById("model-selector");
    if (!sel) return;
    sel.innerHTML = "";
    for (var i = 0; i < modelOptions.length; i++) {
      var m = modelOptions[i];
      var opt = document.createElement("option");
      opt.value = String(m.level);
      var label = m.name || ("level " + m.level);
      if (m.level === defaultModelLevel) label += " (default)";
      if (!m.available) label += " — needs API key";
      opt.textContent = label;
      opt.disabled = !m.available;
      sel.appendChild(opt);
    }
    if (pendingModelLevel != null) setActiveModelLevel(pendingModelLevel);
  }

  function setActiveModelLevel(level) {
    pendingModelLevel = level;
    var sel = document.getElementById("model-selector");
    if (!sel || level == null) return;
    var val = String(level);
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === val) { sel.value = val; return; }
    }
  }

  function onModelSelectorChange() {
    var sel = document.getElementById("model-selector");
    if (!sel || !activeSessionId) return;
    var level = Number(sel.value);
    if (!level) return;
    fetch(
      apiBase() + "/sessions/" + encodeURIComponent(activeSessionId) + "/model",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: level })
      }
    ).then(function (r) {
      if (!r.ok) throw new Error("Failed to set model");
      return r.json();
    }).then(function (data) {
      pendingModelLevel = data.model_level;
      renderActiveModel(data.model_name, !!data.escalated);
      for (var i = 0; i < sessionsList.length; i++) {
        if (sessionsList[i].session_id === activeSessionId) {
          sessionsList[i].model_name = data.model_name;
          sessionsList[i].model_level = data.model_level;
          sessionsList[i].model_escalated = !!data.escalated;
          break;
        }
      }
      renderSessionList({ sessions: sessionsList });
    }).catch(function () {
      showError("Could not change the model for this session.");
      // Revert the selector to the session's known level.
      refreshSessions();
    });
  }

  // ---- Session list rendering -----------------------------------------
  function renderSessionList(data) {
    if (!data || !Array.isArray(data.sessions)) return;
    sessionsList = data.sessions;
    var listEl = document.getElementById("sessions-list");
    var scrollTop = listEl.scrollTop;
    listEl.innerHTML = "";

    for (var i = 0; i < sessionsList.length; i++) {
      var s = sessionsList[i];
      var row = document.createElement("div");
      row.className = "session-row";
      if (s.session_id === activeSessionId) {
        row.classList.add("active");
      }
      if (isSessionUnread(s.session_id, s.turn_count || 0)) {
        row.classList.add("session-row-unread");
      }

      var titleDiv = document.createElement("div");
      titleDiv.className = "session-title";
      if (s._owner === PERIODIC_OWNER) {
        row.classList.add("session-periodic");
        titleDiv.textContent = "[PERIODIC] " + (s.title || "Untitled");
      } else if (s.evergoing) {
        // The single never-ending session: leading turns from finished,
        // off-subject topics are physically trimmed (they disappear from the
        // transcript — distinct from the summary/compaction card).
        row.classList.add("session-evergoing");
        titleDiv.textContent = "[EVERGOING] " + (s.title || "Untitled");
      } else {
        titleDiv.textContent = s.title || "Untitled";
      }
      row.appendChild(titleDiv);

      var metaDiv = document.createElement("div");
      metaDiv.className = "session-meta";
      var parts = [];

      if (s.turn_count !== undefined) {
        parts.push(s.turn_count + " turn" + (s.turn_count !== 1 ? "s" : ""));
      }
      if (s.last_active) {
        parts.push(relativeTime(s.last_active));
      }
      metaDiv.textContent = parts.join(" · ");
      row.appendChild(metaDiv);

      // Model badge — which tier this session runs on. Escalated sessions
      // (the agent asked for a stronger model) are marked so the operator can
      // see at a glance which conversations cost more.
      if (s.model_name && s.session_id === activeSessionId) {
        renderActiveModel(s.model_name, !!s.model_escalated);
      }
      if (s.session_id === activeSessionId && s.model_level != null) {
        setActiveModelLevel(s.model_level);
      }
      if (s.model_name) {
        var modelDiv = document.createElement("div");
        modelDiv.className = "session-model";
        if (s.model_escalated) {
          modelDiv.classList.add("session-model-escalated");
          modelDiv.title = "Escalated to a stronger model for this session";
          modelDiv.textContent = s.model_name + " \u23eb";
        } else {
          modelDiv.textContent = s.model_name;
        }
        row.appendChild(modelDiv);
      }

      // Delete (close) button — appears on hover; stops the session's
      // subsessions and deletes its history (after a confirm()).
      var delBtn = document.createElement("button");
      delBtn.className = "session-delete-btn";
      delBtn.type = "button";
      delBtn.title = "Delete chat";
      delBtn.setAttribute("aria-label", "Delete chat");
      delBtn.textContent = "🗑";
      row.appendChild(delBtn);

      // Closure to capture session_id / title
      (function (sid, title) {
        row.addEventListener("click", function () {
          if (sid !== activeSessionId) {
            switchSession(sid);
          }
        });
        delBtn.addEventListener("click", function (ev) {
          ev.stopPropagation();
          var label = title || "Untitled";
          if (window.confirm(
            "Delete chat “" + label + "”?\n\n" +
            "This stops its subsessions and deletes its history. " +
            "This cannot be undone."
          )) {
            deleteSession(sid);
          }
        });
      })(s.session_id, s.title);

      listEl.appendChild(row);
    }

    // Restore scroll position (preserved across auto-refresh re-renders).
    listEl.scrollTop = scrollTop;
  }


  function deleteSession(sid) {
    // Tear down any background event stream for this session.
    closeBackgroundEventStream(sid);

    var url = apiBase() + "/sessions/" + encodeURIComponent(sid) +
              "?owner_id=" + encodeURIComponent(ownerFor(sid));
    return fetch(url, { method: "DELETE" }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) {
          // Surface a refused delete (including 404) instead of letting the
          // card silently reappear after the next refresh.
          throw new Error(
            (data && data.error) || "Delete failed (HTTP " + r.status + ")"
          );
        }
        return data;
      });
    }).then(function (data) {
      // If we closed the active session, switch to the server-chosen
      // replacement (it always returns one) so the chat view stays valid.
      if (sid === activeSessionId && data && data.active_session_id) {
        switchSession(data.active_session_id);
      }
      refreshSessions();
    }).catch(function (err) {
      showError("Delete failed: " + (err && err.message ? err.message : err));
      // Refresh anyway so the list reflects authoritative server state.
      refreshSessions();
    });
  }

  function relativeTime(raw) {
    // Return a human-readable relative time string (e.g. "2m ago", "1h ago").
    // Accepts Unix timestamps in seconds (number) or ISO 8601 strings.
    var ms;
    if (typeof raw === "number") {
      ms = raw * 1000;  // seconds → milliseconds
    } else {
      ms = new Date(raw).getTime();
    }
    if (!ms || ms <= 0) return "";
    var diffSec = Math.floor((Date.now() - ms) / 1000);
    if (diffSec < 0) return "";
    if (diffSec < 60) return "just now";
    var mins = Math.floor(diffSec / 60);
    if (mins < 60) return mins + "m ago";
    var hours = Math.floor(mins / 60);
    if (hours < 24) return hours + "h ago";
    var days = Math.floor(hours / 24);
    return days + "d ago";
  }

  function updateLastModelTimestamp(ts) {
    // Show a relative timestamp for the last model message at the bottom
    // of the chat area.  `ts` is a Unix timestamp in seconds.
    if (lastModelTimestampEl) {
      lastModelTimestampEl.remove();
      lastModelTimestampEl = null;
    }
    if (!ts) return;
    var el = document.createElement("div");
    el.id = "last-model-timestamp";
    el.textContent = relativeTime(ts);
    el.title = new Date(ts * 1000).toLocaleString();
    chatEl.appendChild(el);
    lastModelTimestampEl = el;
    scrollToBottom();
  }

  function updateActiveHighlight() {
    // Re-render to refresh active highlight.
    if (sessionsList.length > 0) {
      renderSessionList({ sessions: sessionsList });
    }
  }

  function refreshSessions() {
    fetchSessions().then(function (data) {
      updateUnreadFromList(data.sessions || []);
      markSessionRead(activeSessionId);
      renderSessionList(data);
      // NOTE: we purposely do NOT update activeSessionId from the server's
      // active_session_id here — that would silently clobber the user's choice
      // when the panel is opened. The server's opinion is only used during
      // initial bootstrap (see the fetchSessions call at the bottom).
    }).catch(function () {
      // Silently ignore — list may be stale but still usable.
    });
  }

  function switchSession(sessionId) {
    if (sessionId === activeSessionId) return;

    // Persist the outgoing session's queued messages before switching away.
    // They stay queued (not auto-dispatched into a session we're leaving) and
    // are restored — and re-attached to any ongoing turn — when we return.
    saveDraft();

    // If the outgoing session has queued messages, open a background event
    // stream so we can drain them when its turn completes — even while
    // another session is focused.
    var oldSessionId = activeSessionId;
    var hasQueued = messageQueue.length > 0;

    // Clear the now-persisted queue so it does not leak into the new session.
    messageQueue = [];
    updateCancelQueuedButton();

    // Abandon the outgoing session's foreground POST stream. Its turn keeps
    // running server-side; we re-attach to it via /events (chat_turn_resume)
    // if we come back. Aborting stops its onData from rendering into the
    // bubbles we're about to clear.
    if (activePostAbort) {
      try { activePostAbort.abort(); } catch (_) {}
      activePostAbort = null;
      activePostSessionId = null;
    }

    // Reset per-turn state — when we return to this session later,
    // restoreDraft() must be able to call drainQueue() without isBusy()
    // blocking it (the abandoned POST left state as "sending"/"streaming").
    state = "idle";
    updateSendBusy();
    reattachActive = false;
    reattachTurnId = null;
    hideTypingIndicator();

    // 1. Persist the new active session_id.
    setActiveSessionId(sessionId);

    // 1b. Clear unread highlight for this session.
    markSessionRead(sessionId);

    // If switching back to a session that has a background event stream,
    // close it — the foreground /events channel (opened below) takes over.
    if (backgroundStreams[sessionId]) {
      closeBackgroundEventStream(sessionId);
    }

    // 2. Clear the chat DOM bubbles.
    clearChatBubbles();

    // 2b. Reset the per-session Subsessions panel.
    clearSubsessions();

    // 3. Close the current event stream and re-open for new session.
    //    BUT: if the outgoing session has queued messages, don't close
    //    its /events channel — keep it alive as a background stream so
    //    we still receive chat_turn_done and can drain the queue.
    // Close the foreground stream normally.
    closeEventStream();

    // If the outgoing session has queued messages, open a dedicated
    // background event stream so we still receive chat_turn_done (or
    // chat_turn_error) and can drain the queue — even while another
    // session is focused.  The background stream has its own generation
    // counter and watchdog; it never checks the foreground
    // eventStreamGeneration so frames are not dropped.
    if (hasQueued && oldSessionId) {
      openBackgroundEventStream(oldSessionId);
    }

    // 4. Load history for the new session, THEN open the foreground event
    //    stream. The order matters: opening /events primes a
    //    chat_turn_resume frame for any in-flight turn on subscribe, and if
    //    that re-attach render runs before history renders, the in-flight
    //    round is appended above the persisted transcript and scrolled out of
    //    view — hiding it until the turn completes. Deferring openEventStream
    //    until history is in the DOM makes the re-attached round render below
    //    it, so switching away and back keeps the live round visible.
    loadHistory(openEventStream);

    // 5. Reload subsessions for the new session.
    fetchSubsessions();

    // 6. Update the active-row highlight.
    updateActiveHighlight();

    // 7. Reset idle timer.
    resetIdleTimer();
  }

  // Adopt a continuation session announced by the server (idle-timeout
  // compaction reroutes a turn into a fresh session and reports it in the
  // "done" frame). Unlike switchSession this keeps the visible transcript —
  // the current bubbles ARE the continuation's content — and only rebinds
  // the persisted id, the event stream, and the subsessions panel.
  function adoptSession(sessionId) {
    if (!sessionId || sessionId === activeSessionId) return;
    setActiveSessionId(sessionId);
    closeEventStream();
    openEventStream();
    clearSubsessions();
    fetchSubsessions();
    refreshSessions();
    updateActiveHighlight();
  }
  // ---- Live re-attach handlers (foreground turn over /events) ----------
  // These render an in-flight turn for the *currently viewed* session when
  // this tab does NOT own its POST (a second tab, or this tab after switching
  // away and back). The tab that owns the POST renders from the POST body and
  // ignores these echoes (isOwnPostSession guard).

  function isOwnPostSession(sid) {
    return activePostSessionId !== null && activePostSessionId === sid;
  }

  // Only act on frames for the session we're viewing, and only when we're not
  // the POST owner (which renders the same turn from its response body).
  function reattachApplies(frame) {
    return frame.session_id === activeSessionId && !isOwnPostSession(frame.session_id);
  }

  function handleReattachStart(frame) {
    if (!reattachApplies(frame)) return;
    if (reattachActive) return;
    reattachActive = true;
    reattachTurnId = frame.turn_id;
    // Render the operator's own message: this turn was started outside our
    // POST (drained queue, another tab) so no bubble exists for it yet.
    if (typeof frame.user_message === "string" && frame.user_message.length > 0) {
      addUserBubble(frame.user_message);
    }
    state = "sending";
    updateSendBusy();
    showTypingIndicator();
  }

  function handleReattachResume(frame) {
    // A late subscriber's replay of the in-progress turn: render what has been
    // emitted so far, then follow the live chat_token frames.
    if (!reattachApplies(frame)) return;
    reattachActive = true;
    reattachTurnId = frame.turn_id;
    // The dispatched message is only in the server-side coalescer until the
    // turn records — render its bubble so it doesn't look lost after
    // switching away and back mid-turn.
    if (typeof frame.user_message === "string" && frame.user_message.length > 0) {
      addUserBubble(frame.user_message);
    }
    currentAssistantBubble = null;
    rawAssistantText = "";
    if (typeof frame.content === "string" && frame.content.length > 0) {
      hideTypingIndicator();
      state = "streaming";
      updateSendBusy();
      appendToken(frame.content);
    } else {
      // Turn started but no tokens yet — show the typing indicator.
      state = "sending";
      updateSendBusy();
      showTypingIndicator();
    }
  }

  function handleReattachToken(frame) {
    if (!reattachApplies(frame)) return;
    if (!reattachActive) return;
    if (reattachTurnId && frame.turn_id && frame.turn_id !== reattachTurnId) return;
    if (state === "sending") {
      hideTypingIndicator();
      state = "streaming";
      updateSendBusy();
    }
    if (typeof frame.content === "string") appendToken(frame.content);
  }

  function handleReattachDone(frame) {
    if (frame.session_id !== activeSessionId) return;
    if (!reattachActive) return;
    if (reattachTurnId && frame.turn_id && frame.turn_id !== reattachTurnId) return;
    hideTypingIndicator();
    finaliseAssistantBubble();
    reattachActive = false;
    reattachTurnId = null;
    state = "idle";
    updateSendBusy();
    if (frame.timestamp) updateLastModelTimestamp(frame.timestamp);
    // The re-attached turn finished — now dispatch any messages the user
    // queued behind it.
    drainQueue();
  }

  function handleReattachError(frame) {
    if (frame.session_id !== activeSessionId) return;
    if (!reattachActive) return;
    if (reattachTurnId && frame.turn_id && frame.turn_id !== reattachTurnId) return;
    hideTypingIndicator();
    finaliseAssistantBubble();
    reattachActive = false;
    reattachTurnId = null;
    showError(frame.message || "Server error");
    state = "error";
    updateSendBusy();
  }

  // ---- Event stream lifecycle -----------------------------------------
  var eventStreamAbortController = null;
  var eventsStreamIntentionallyClosed = false;
  var eventStreamReconnectTimer = null;
  // Monotonic stream generation. Callbacks captured by an older
  // openEventStream() compare their generation against this and no-op when
  // stale. Without it, aborting the previous stream fires its pump catch
  // with AbortError AFTER eventsStreamIntentionallyClosed was reset to
  // false, so the stale stream scheduled a reconnect that aborted the new
  // healthy stream 5s later — a self-sustaining reconnect loop that left
  // /events effectively dead (subsession echo frames published to a key
  // with no subscriber are silently dropped).
  var eventStreamGeneration = 0;
  var eventStreamWatchdogTimer = null;

  // Background /events channels kept alive for sessions with queued messages
  // after the user switches focus away.  When chat_turn_done arrives on one
  // of these, the queued messages are drained server-side without waiting for
  // the user to return.
  var backgroundStreams = {};   // { sessionId: { abortController, generation } }

  function closeEventStream() {
    eventsStreamIntentionallyClosed = true;
    eventStreamGeneration++;
    if (eventStreamReconnectTimer) {
      clearTimeout(eventStreamReconnectTimer);
      eventStreamReconnectTimer = null;
    }
    if (eventStreamWatchdogTimer) {
      clearInterval(eventStreamWatchdogTimer);
      eventStreamWatchdogTimer = null;
    }
    if (eventStreamAbortController) {
      eventStreamAbortController.abort();
      eventStreamAbortController = null;
    }
  }

  function closeBackgroundEventStream(sessionId) {
    var entry = backgroundStreams[sessionId];
    if (!entry) return;
    if (entry._watchdogTimer) {
      clearInterval(entry._watchdogTimer);
      entry._watchdogTimer = null;
    }
    if (entry._reconnectTimer) {
      clearTimeout(entry._reconnectTimer);
      entry._reconnectTimer = null;
    }
    try { entry.abortController.abort(); } catch (_) {}
    delete backgroundStreams[sessionId];
  }

  // Open a standalone /events channel for a session the user left with
  // queued messages.  Unlike the foreground stream (openEventStream) this
  // one has its own generation counter and watchdog so it is never
  // invalidated by the foreground stream's lifecycle.  It only listens for
  // chat_turn_done / chat_turn_error — when the turn completes the queued
  // messages are drained via drainBackgroundSession().
  function openBackgroundEventStream(sessionId) {
    // Ensure an entry exists.
    if (!backgroundStreams[sessionId]) {
      backgroundStreams[sessionId] = {};
    }
    var entry = backgroundStreams[sessionId];

    // Bump this stream's own generation to invalidate any previous
    // background stream for the same session.
    entry.generation = (entry.generation || 0) + 1;
    var gen = entry.generation;

    // Clean up any prior background stream.
    if (entry.abortController) {
      try { entry.abortController.abort(); } catch (_) {}
    }
    if (entry._watchdogTimer) {
      clearInterval(entry._watchdogTimer);
      entry._watchdogTimer = null;
    }
    if (entry._reconnectTimer) {
      clearTimeout(entry._reconnectTimer);
      entry._reconnectTimer = null;
    }

    entry.abortController = new AbortController();
    var abortController = entry.abortController;

    var eventsUrl = apiBase() + "/events" +
                    "?session_id=" + encodeURIComponent(sessionId) +
                    "&owner_id=" + encodeURIComponent(ownerFor(sessionId));

    var lastActivity = Date.now();

    var controller = {
      onActivity: function () {
        lastActivity = Date.now();
      },
      onData: function (raw) {
        // Bail out if a newer background stream superseded this one.
        if (gen !== entry.generation) return;
        var frame;
        try { frame = JSON.parse(raw); }
        catch (_) { return; }

        if (frame.type === "chat_turn_done") {
          if (frame.session_id === sessionId) {
            drainBackgroundSession(sessionId);
          }
        } else if (frame.type === "chat_turn_error") {
          // A turn that errored won't produce chat_turn_done — drain
          // anyway so queued messages aren't stuck forever.
          if (frame.session_id === sessionId) {
            drainBackgroundSession(sessionId);
          }
        }
        // Ignore all other frame types — background streams only care
        // about turn completion.
      },
      onDone: function () {
        if (gen !== entry.generation) return;
        // Server closed the stream — reconnect after a short delay.
        scheduleBackgroundReconnect(sessionId);
      },
      error: function (err) {
        if (gen !== entry.generation) return;
        if (err && err.name === "AbortError") return;
        scheduleBackgroundReconnect(sessionId);
      }
    };

    function scheduleBackgroundReconnect(sid) {
      // Don't reconnect if the entry was removed (session focused / deleted).
      var e = backgroundStreams[sid];
      if (!e || e.generation !== gen) return;
      if (e._reconnectTimer) return;
      e._reconnectTimer = setTimeout(function () {
        if (backgroundStreams[sid]) {
          backgroundStreams[sid]._reconnectTimer = null;
        }
        openBackgroundEventStream(sid);
      }, 5000);
    }

    fetch(eventsUrl, {
      method: "GET",
      signal: abortController.signal
    }).then(function (response) {
      if (gen !== entry.generation) return;
      if (!response.ok) {
        scheduleBackgroundReconnect(sessionId);
        return;
      }
      var contentType = response.headers.get("content-type") || "";
      if (contentType.indexOf("text/event-stream") === -1) {
        scheduleBackgroundReconnect(sessionId);
        return;
      }
      lastActivity = Date.now();
      // Watchdog: if no bytes arrive for 20 s the connection is dead.
      entry._watchdogTimer = setInterval(function () {
        if (gen !== entry.generation) return;
        if (Date.now() - lastActivity > 20000) {
          clearInterval(entry._watchdogTimer);
          entry._watchdogTimer = null;
          try { abortController.abort(); } catch (_) {}
          scheduleBackgroundReconnect(sessionId);
        }
      }, 5000);
      var parser = processSSEStream(response.body, controller);
      parser.start();
    }).catch(function (err) {
      if (gen !== entry.generation) return;
      if (err && err.name === "AbortError") return;
      scheduleBackgroundReconnect(sessionId);
    });
  }

  // Drain queued messages for a session whose turn just completed while
  // the user was focused elsewhere.  Fetches the saved draft, POSTs each
  // queued message to the server, then clears the draft so the messages
  // are not re-dispatched when the user returns.
  function drainBackgroundSession(sessionId) {
    var entry = backgroundStreams[sessionId];
    // Guard against concurrent drains: if already draining or the entry
    // is gone (closed by foreground handler), stop.
    if (!entry || entry._draining) return;
    entry._draining = true;

    closeBackgroundEventStream(sessionId);

    drainSessionDraft({
      fetchDraft: function () {
        return fetch(apiBase() + "/sessions/" + encodeURIComponent(sessionId) + "/draft")
          .then(function (r) { return r.ok ? r.json() : null; });
      },
      sendItem: function (item) {
        var imagesForSend = [];
        if (Array.isArray(item.images)) {
          for (var j = 0; j < item.images.length; j++) {
            var pi = _base64ToPendingImage(
              item.images[j].data, item.images[j].media_type,
              item.images[j].filename || "image"
            );
            if (pi) imagesForSend.push(pi);
          }
        }
        var encodePromise = imagesForSend.length > 0
          ? encodeImagesFromList(imagesForSend)
          : Promise.resolve([]);
        return encodePromise.then(function (encodedImages) {
          var body = {
            message: item.text || "",
            session_id: sessionId,
            owner_id: ownerFor(sessionId)
          };
          if (item.messageId) body.message_id = item.messageId;
          if (encodedImages.length > 0) body.images = encodedImages;
          return fetch(serverUrl(), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          }).then(function (r) { return r.ok; });
        });
      },
      putDraft: function (items) {
        var queue = [];
        for (var i = 0; i < items.length; i++) {
          queue.push({ text: items[i].text, images: items[i].images || [], messageId: items[i].messageId });
        }
        return fetch(apiBase() + "/sessions/" + encodeURIComponent(sessionId) + "/draft", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ pending_images: [], queue: queue }),
          keepalive: true
        });
      }
    }).catch(function () { /* best-effort */ });
  }

  // Schedule exactly one reconnect. Without the guard, stacked onDone/error
  // callbacks (or repeated failures) each queue their own openEventStream(),
  // and since openEventStream() never aborted the prior stream, multiple live
  // /events fetches accumulated — each holding a server-side EventBus
  // subscription, so every loop/task frame was delivered (and rendered) once
  // per leaked subscription. That is the duplicate-bubble bug.
  function scheduleEventReconnect() {
    if (eventsStreamIntentionallyClosed) return;
    if (eventStreamReconnectTimer) return;  // one reconnect at a time
    eventStreamReconnectTimer = setTimeout(function () {
      eventStreamReconnectTimer = null;
      openEventStream();
    }, 5000);
  }

  // ---- Subsessions store (per-session, rebuilt from server snapshots) --
  // subsById: subsession_id → snapshot fields plus UI-only state:
  //   expanded, transcript ([{role, text, timestamp}]), transcriptLoaded,
  //   _transcriptLoading, _closing, _draft, and per-render DOM refs
  //   (_transcriptEl, _countdownEl, _msgInput, _msgBtn).
  // subsOrder: subsession_ids in arrival order; the tree display order is
  // recomputed per render by subsDisplayOrder().
  var subsById = {};
  var subsOrder = [];
  // Closed/failed/interrupted subsessions are hidden by default (they pile
  // up over time and crowd out the running ones); toggled via the panel's
  // "Show closed" button.
  var showTerminalSubs = false;
  // Focus mode: one subsession row fills the entire screen.
  var focusedSubId = null;
  // Most-recently expanded or interacted-with subsession (for keyboard shortcut).
  var selectedSubId = null;
  // Saved expanded state before focus (exact restore on exit).
  var focusPrevExpanded = null;

  var SUBS_SNAPSHOT_FIELDS = [
    "subsession_id", "kind", "owner_session_id", "parent_id", "depth",
    "title", "prompt", "model_level", "status", "created_at",
    "last_activity_at", "interval_seconds", "next_run_at",
    "include_previous_result", "runs", "max_runs", "last_result",
    "summary", "close_reason", "error"
  ];

  function isSubsTerminal(sub) {
    return sub.status === "closed" ||
           sub.status === "failed" ||
           sub.status === "interrupted";
  }

  function subsKindLabel(kind) {
    if (kind === "task") return "⚙ task";
    if (kind === "periodic") return "⏱ periodic";
    if (kind === "wait_for_event") return "⏳ event";
    if (kind === "user_chat") return "💬 chat";
    if (kind === "on_close") return "🔒 on-close";
    return "⚙ task";
  }

  function newSubsEntry() {
    return {
      expanded: false,
      transcript: [],
      transcriptLoaded: false,
      _transcriptLoading: false,
      _closing: false,
      _draft: "",
      // Count of unread messages in this subsession's own transcript.
      // Ancestor unread state is derived on the fly by subsUnreadTotal()
      // (own count + every descendant's), so a nested child's message
      // flags all of its ancestors.
      unreadSelf: 0
    };
  }

  // Unread messages that arrived for a subsession whose row is not known
  // yet (e.g. the message frame beat the subsession_started snapshot).
  // Applied once the row appears; see applyPendingUnread.
  var pendingUnread = {};

  // Total unread messages shown on a subsession title: this subsession's
  // own unread messages plus the totals of every descendant, so a nested
  // child's message marks every ancestor in the chain. Computed on the
  // fly rather than stored, which keeps it correct no matter what order
  // parent/child frames arrive in.
  function subsUnreadTotal(sub) {
    if (!sub) return 0;
    var total = sub.unreadSelf || 0;
    for (var i = 0; i < subsOrder.length; i++) {
      var child = subsById[subsOrder[i]];
      if (child && child.parent_id === sub.subsession_id) {
        total += subsUnreadTotal(child);
      }
    }
    return total;
  }

  // Clear a subsession's own unread count. Ancestor totals are derived on
  // the fly, so no propagation bookkeeping is needed here.
  function markSubsessionRead(sub) {
    if (sub) sub.unreadSelf = 0;
  }

  // Fold any queued unread messages into a newly-seen subsession row.
  function applyPendingUnread(sub) {
    var sid = sub.subsession_id;
    if (!pendingUnread[sid]) return;
    sub.unreadSelf = (sub.unreadSelf || 0) + pendingUnread[sid];
    delete pendingUnread[sid];
  }

  function applySubsSnapshot(sub, snap) {
    for (var i = 0; i < SUBS_SNAPSHOT_FIELDS.length; i++) {
      var field = SUBS_SNAPSHOT_FIELDS[i];
      if (snap[field] !== undefined) sub[field] = snap[field];
    }
    // A live user_chat subsession is the agent asking the user something —
    // keep its transcript + reply box visible without an extra click.
    if (sub.kind === "user_chat" && !isSubsTerminal(sub)) sub.expanded = true;
  }

  // Insert-or-merge a snapshot / partial-update frame, then re-render.
  // Used for both subsession_started (full snapshot) and subsession_updated
  // (subset of fields) — applySubsSnapshot only copies defined fields.
  function upsertSubsession(snap) {
    var sid = snap.subsession_id;
    if (!sid) return;
    var sub = subsById[sid];
    if (!sub) {
      sub = newSubsEntry();
      subsById[sid] = sub;
      subsOrder.push(sid);
    }
    applySubsSnapshot(sub, snap);
    renderSubsessionsList();
  }

  // Terminal frames (subsession_closed / subsession_failed) carry "reason"
  // rather than "close_reason" — merge that mapping on top of the snapshot.
  function applySubsTerminalFrame(frame) {
    var sid = frame.subsession_id;
    if (!sid) return;
    var sub = subsById[sid];
    if (!sub) {
      sub = newSubsEntry();
      subsById[sid] = sub;
      subsOrder.push(sid);
    }
    applySubsSnapshot(sub, frame);
    if (frame.reason !== undefined) sub.close_reason = frame.reason;
    sub._closing = false;
    applyPendingUnread(sub);
    renderSubsessionsList();
  }

  function handleSubsessionMessage(frame) {
    var sub = subsById[frame.subsession_id];
    if (!sub) {
      // Unknown row — remember the unread hit so it is applied when the
      // subsession snapshot arrives.
      pendingUnread[frame.subsession_id] =
        (pendingUnread[frame.subsession_id] || 0) + 1;
      return;
    }
    var msg = {
      role: frame.role || "assistant",
      text: frame.text || "",
      timestamp: frame.timestamp || 0
    };
    if (!subsTranscriptHas(sub, msg)) sub.transcript.push(msg);
    if (frame.timestamp) sub.last_activity_at = frame.timestamp;
    // Mark this subsession unread and propagate the count up the parent
    // chain, then refresh the affected rows' headers in place — a full
    // list re-render here would steal focus from the reply box while the
    // user is typing. A message landing in an expanded, visible row is
    // already on screen, so it does not count as unread (otherwise it
    // would leave a badge with no user action left to clear it).
    var alreadyVisible = sub.expanded && subsPanel.classList.contains("visible");
    if (!alreadyVisible) {
      sub.unreadSelf = (sub.unreadSelf || 0) + 1;
      renderSubsessionRow(sub);
      var ancId = sub.parent_id;
      while (ancId) {
        var anc = subsById[ancId];
        if (!anc) break;
        renderSubsessionRow(anc);
        ancId = anc.parent_id;
      }
      // Announce for screen-reader users (the badge/color change is visual).
      var announceEl = document.getElementById("subs-announce");
      if (announceEl) {
        announceEl.textContent = "New message in subsession " +
          (sub.title || "untitled");
      }
    }
    // Update the transcript in place (the body is untouched by the header
    // refresh above).
    if (sub.expanded && sub._transcriptEl) renderSubsTranscript(sub);
  }

  function subsTranscriptHas(sub, msg) {
    var t = sub.transcript || [];
    for (var i = t.length - 1; i >= 0; i--) {
      if (t[i].role === msg.role &&
          t[i].text === msg.text &&
          (t[i].timestamp || 0) === (msg.timestamp || 0)) {
        return true;
      }
    }
    return false;
  }

  // ---- Subsessions tree rendering --------------------------------------
  // Flatten the tree: top-level entries (parent_id === null) in created_at
  // order, each followed by its descendants depth-first (children in
  // created_at order). Orphans (unknown parent) fall back to the end.
  function subsDisplayOrder() {
    var childrenOf = {};
    var top = [];
    var i, sub;
    for (i = 0; i < subsOrder.length; i++) {
      sub = subsById[subsOrder[i]];
      if (!sub) continue;
      if (sub.parent_id) {
        if (!childrenOf[sub.parent_id]) childrenOf[sub.parent_id] = [];
        childrenOf[sub.parent_id].push(sub);
      } else {
        top.push(sub);
      }
    }
    function byCreated(a, b) { return (a.created_at || 0) - (b.created_at || 0); }
    top.sort(byCreated);
    var out = [];
    function walk(node) {
      out.push(node);
      var kids = childrenOf[node.subsession_id] || [];
      kids.sort(byCreated);
      for (var k = 0; k < kids.length; k++) walk(kids[k]);
    }
    for (i = 0; i < top.length; i++) walk(top[i]);
    for (i = 0; i < subsOrder.length; i++) {
      sub = subsById[subsOrder[i]];
      if (sub && out.indexOf(sub) === -1) out.push(sub);
    }
    return out;
  }

  // Reconciles the list in place rather than wiping and rebuilding it —
  // a full innerHTML="" on every subsession_updated frame (fired
  // frequently by an in-flight subsession) used to blow away the panel's
  // own scroll position on every refresh, and destroy+recreate the reply
  // textarea for any expanded user_chat row, stealing focus mid-keystroke.
  // Existing rows are reused and only their (cheap, non-interactive)
  // header is rebuilt; the transcript/reply-box body is never touched
  // here — see renderSubsessionRow.
  function renderSubsessionsList() {
    var order = subsDisplayOrder();
    var terminalCount = 0;
    var visible = [];
    for (var i = 0; i < order.length; i++) {
      if (isSubsTerminal(order[i])) terminalCount++;
      if (showTerminalSubs || !isSubsTerminal(order[i])) visible.push(order[i]);
    }
    updateSubsToggleTerminalButton(terminalCount);
    // If the focused subsession is no longer visible (e.g. it was closed
    // and terminal rows are hidden), exit focus mode gracefully — do not
    // re-render from inside the render loop (pass false).
    if (focusedSubId !== null) {
      var focussed = false;
      for (var v = 0; v < visible.length; v++) {
        if (visible[v].subsession_id === focusedSubId) { focussed = true; break; }
      }
      if (!focussed || !subsById[focusedSubId]) exitSubsFocus(false);
    }
    if (visible.length === 0) {
      subsList.innerHTML = "";
      var empty = document.createElement("div");
      empty.className = "subs-empty";
      empty.textContent = order.length === 0
        ? "No subsessions yet — the assistant spawns background work here."
        : "No running subsessions — " + terminalCount + " closed/failed " +
          "hidden (use the button above to show them).";
      subsList.appendChild(empty);
      return;
    }
    var seenIds = {};
    var prevEl = null;
    for (var j = 0; j < visible.length; j++) {
      var sub = visible[j];
      seenIds[sub.subsession_id] = true;
      var row = renderSubsessionRow(sub);
      var expectedNext = prevEl ? prevEl.nextSibling : subsList.firstChild;
      if (row !== expectedNext) subsList.insertBefore(row, expectedNext);
      prevEl = row;
    }
    // Drop rows for subsessions that are no longer visible (closed and
    // hidden, or gone entirely) — anything not touched above.
    var child = subsList.firstChild;
    while (child) {
      var next = child.nextSibling;
      if (!child._subsId || !seenIds[child._subsId]) subsList.removeChild(child);
      child = next;
    }
  }

  // Shows/labels the "Show closed (N)" toggle button; hidden entirely when
  // there are no terminal (closed/failed/interrupted) subsessions to hide.
  function updateSubsToggleTerminalButton(terminalCount) {
    var btn = document.getElementById("subs-toggle-terminal");
    if (!btn) return;
    if (terminalCount === 0) {
      btn.style.display = "none";
      return;
    }
    btn.style.display = "";
    btn.textContent = showTerminalSubs
      ? "Hide closed (" + terminalCount + ")"
      : "Show closed (" + terminalCount + ")";
  }

  // Builds (or rebuilds) *sub*'s row. The header — title/status/meta/
  // result/actions — has no interactive state and is cheap to throw away
  // and rebuild on every call. The body — transcript + reply textarea —
  // is expensive to lose (scroll position, focus, in-progress typing) so
  // it is built once per expand and left completely alone on subsequent
  // calls; transcript content updates go through renderSubsTranscript /
  // handleSubsessionMessage instead, which mutate it in place.
  function renderSubsessionRow(sub) {
    var terminal = isSubsTerminal(sub);
    var status = sub.status || "running";

    var row = sub._rowEl;
    if (!row) {
      row = document.createElement("div");
      sub._rowEl = row;
    }
    row._subsId = sub.subsession_id;
    row.className = "subs-row status-" + status +
      (terminal ? " terminal" : "") +
      (focusedSubId === sub.subsession_id ? " focused" : "") +
      (subsUnreadTotal(sub) > 0 ? " subs-row-unread" : "");
    // Indent children under their parent (depth 1 = top level).
    row.style.marginLeft = (((sub.depth || 1) - 1) * 14) + "px";

    var header = buildSubsHeader(sub, terminal, status);
    if (sub._headerEl) {
      row.replaceChild(header, sub._headerEl);
    } else {
      row.insertBefore(header, row.firstChild);
    }
    sub._headerEl = header;

    if (sub.expanded && !sub._bodyEl) {
      sub._bodyEl = buildSubsBody(sub, terminal);
      row.appendChild(sub._bodyEl);
    } else if (!sub.expanded && sub._bodyEl) {
      row.removeChild(sub._bodyEl);
      sub._bodyEl = null;
      sub._transcriptEl = null;
      sub._msgInput = null;
      sub._msgBtn = null;
    }

    return row;
  }

  function buildSubsHeader(sub, terminal, status) {
    var header = document.createElement("div");
    header.className = "subs-header";

    // Title line: kind icon+label, title, status pill, model-level badge.
    var titleLine = document.createElement("div");
    titleLine.className = "subs-title-line";

    var kindSpan = document.createElement("span");
    kindSpan.className = "subs-kind";
    kindSpan.textContent = subsKindLabel(sub.kind);
    titleLine.appendChild(kindSpan);

    var titleSpan = document.createElement("span");
    titleSpan.className = "subs-title";
    titleSpan.textContent = sub.title || "(untitled)";
    if (sub.prompt) titleSpan.title = truncateText(sub.prompt, 200);
    titleLine.appendChild(titleSpan);

    // Unread badge: shows the total unread messages in this subsession
    // and all of its descendants (see subsUnreadTotal). Rendered as
    // a count badge rather than color alone so the state is perceivable
    // without color vision.
    var unreadCount = subsUnreadTotal(sub);
    if (unreadCount > 0) {
      var unreadBadge = document.createElement("span");
      unreadBadge.className = "unread-badge";
      unreadBadge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
      unreadBadge.title = unreadCount + " unread message" +
        (unreadCount === 1 ? "" : "s") +
        " (this subsession and its children)";
      unreadBadge.setAttribute("aria-label", unreadBadge.title);
      titleLine.appendChild(unreadBadge);
    }

    var statusSpan = document.createElement("span");
    statusSpan.className = "subs-status status-" + status;
    statusSpan.textContent = sub._closing ? "closing" : status;
    titleLine.appendChild(statusSpan);

    if (sub.model_level) {
      var levelSpan = document.createElement("span");
      levelSpan.className = "subs-level";
      levelSpan.textContent = "L" + sub.model_level;
      levelSpan.title = "Model level " + sub.model_level;
      titleLine.appendChild(levelSpan);
    }
    header.appendChild(titleLine);

    // Meta line: periodic run counter + interval + live countdown;
    // close reason for terminal rows.
    var metaDiv = document.createElement("div");
    metaDiv.className = "subs-meta";
    var metaParts = [];
    if (sub.kind === "periodic") {
      var runLabel = "run " + (sub.runs || 0);
      if (sub.max_runs) runLabel += "/" + sub.max_runs;
      metaParts.push(runLabel);
      if (sub.interval_seconds) {
        metaParts.push("every " + formatInterval(sub.interval_seconds));
      }
    }
    if (terminal && sub.close_reason) metaParts.push(sub.close_reason);
    metaDiv.textContent = metaParts.join(" • ");
    sub._countdownEl = null;
    if (!terminal && sub.kind === "periodic" && sub.next_run_at) {
      var countdownSpan = document.createElement("span");
      countdownSpan.className = "subs-countdown";
      countdownSpan.textContent = subsCountdownLabel(sub);
      sub._countdownEl = countdownSpan;
      metaDiv.appendChild(countdownSpan);
    }
    if (metaDiv.textContent !== "" || metaDiv.firstChild) {
      header.appendChild(metaDiv);
    }

    // Latest result / summary / error line (one-liner, truncated).
    var resultText = sub.error || sub.summary || sub.last_result;
    if (resultText) {
      var resultDiv = document.createElement("div");
      resultDiv.className = "subs-result";
      resultDiv.textContent = truncateText(resultText, 160);
      resultDiv.title = truncateText(resultText, 400);
      if (sub.error) resultDiv.style.color = "#fca5a5";
      header.appendChild(resultDiv);
    }

    // Actions row: labeled expand/collapse + Close (active rows only).
    var actionsDiv = document.createElement("div");
    actionsDiv.className = "subs-actions";

    var expandBtn = document.createElement("button");
    expandBtn.type = "button";
    expandBtn.className = "subs-action-btn";
    if (sub.expanded) {
      expandBtn.textContent = "▾ Hide transcript";
      expandBtn.title = "Hide this subsession's conversation";
    } else {
      expandBtn.textContent = "▸ Transcript";
      expandBtn.title = "Show this subsession's conversation";
    }
    expandBtn.addEventListener("click", function () {
      sub.expanded = !sub.expanded;
      selectedSubId = sub.subsession_id;
      // Opening the transcript is the "read" signal: clear this
      // subsession's unread count (ancestor totals derive from it).
      if (sub.expanded) markSubsessionRead(sub);
      renderSubsessionsList();
    });
    actionsDiv.appendChild(expandBtn);

    // Focus button: expand one subsession to fill the screen.
    var focusBtn = document.createElement("button");
    focusBtn.type = "button";
    focusBtn.className = "subs-action-btn subs-focus-btn";
    var isFocused = focusedSubId === sub.subsession_id;
    focusBtn.textContent = isFocused ? "✕ Exit focus" : "⛶ Focus";
    focusBtn.title = isFocused
      ? "Restore the multi-panel layout (Esc)"
      : "Expand this subsession to fill the screen (Ctrl+Shift+F)";
    focusBtn.setAttribute("aria-pressed", isFocused ? "true" : "false");
    focusBtn.setAttribute("aria-label", focusBtn.textContent);
    focusBtn.addEventListener("click", function () {
      toggleSubsFocus(sub);
    });
    actionsDiv.appendChild(focusBtn);

    if (!terminal) {
      var closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.className = "subs-action-btn subs-close-btn";
      closeBtn.textContent = sub._closing ? "Closing…" : "Close";
      closeBtn.disabled = !!sub._closing;
      closeBtn.title = "Stop this subsession and report back";
      closeBtn.addEventListener("click", function () {
        closeSubsession(sub, closeBtn);
      });
      actionsDiv.appendChild(closeBtn);
    }
    header.appendChild(actionsDiv);

    return header;
  }

  function buildSubsBody(sub, terminal) {
    var wrapper = document.createElement("div");
    wrapper.className = "subs-body";

    var transcriptDiv = document.createElement("div");
    transcriptDiv.className = "subs-transcript";
    sub._transcriptEl = transcriptDiv;
    wrapper.appendChild(transcriptDiv);
    renderSubsTranscript(sub);
    // Lazy-load the transcript from the server on first expand.
    if (!sub.transcriptLoaded) loadSubsTranscript(sub);

    if (sub.kind === "user_chat" && !terminal) {
      wrapper.appendChild(buildSubsInputRow(sub));
    }

    return wrapper;
  }

  function renderSubsTranscript(sub) {
    var container = sub._transcriptEl;
    if (!container) return;
    container.innerHTML = "";
    var msgs = (sub.transcript || []).slice();
    msgs.sort(function (a, b) {
      return (a.timestamp || 0) - (b.timestamp || 0);
    });
    if (msgs.length === 0) {
      var placeholder = document.createElement("div");
      placeholder.className = "subs-msg subs-msg--system";
      placeholder.textContent = sub._transcriptLoading
        ? "Loading transcript…" : "No messages yet.";
      container.appendChild(placeholder);
      return;
    }
    var isUserChat = sub.kind === "user_chat";
    for (var i = 0; i < msgs.length; i++) {
      var msg = msgs[i];
      var role = msg.role || "assistant";
      var msgDiv = document.createElement("div");
      msgDiv.className = "subs-msg subs-msg--" + role;
      var roleLabel = document.createElement("span");
      roleLabel.className = "subs-msg-role";
      roleLabel.textContent = role === "user" ? "You"
        : role === "parent" ? "From main chat"
        : role === "system" ? "System" : "Assistant";
      msgDiv.appendChild(roleLabel);
      var textSpan = document.createElement("span");
      var msgText = msg.text || "";
      // Parse suggestions for assistant messages in user_chat subsessions.
      if (isUserChat && role === "assistant") {
        var parsed = parseSuggestions(msgText);
        textSpan.textContent = parsed.cleanText;
        msgDiv.appendChild(textSpan);
        container.appendChild(msgDiv);
        if (parsed.suggestions && parsed.suggestions.length > 0) {
          // Only the latest assistant message's chips are live; any earlier
          // set is stale (a newer message superseded that decision) and is
          // rendered inert.
          var isLatest = i === msgs.length - 1;
          renderSuggestionChips(parsed.suggestions, (function (s) {
            return function (text) { sendSubsessionMessage(s, text); };
          })(sub), msgDiv, !isLatest);
        }
      } else {
        textSpan.textContent = msgText;
        msgDiv.appendChild(textSpan);
        container.appendChild(msgDiv);
      }
    }
    container.scrollTop = container.scrollHeight;
  }

  function loadSubsTranscript(sub) {
    if (sub._transcriptLoading) return;
    sub._transcriptLoading = true;
    var url = apiBase() + "/subsessions/" +
              encodeURIComponent(sub.subsession_id) + "/transcript";
    fetch(url, { method: "GET" }).then(function (response) {
      if (!response.ok) return null;
      return response.json();
    }).then(function (data) {
      sub._transcriptLoading = false;
      sub.transcriptLoaded = true;
      if (data && Array.isArray(data.transcript)) {
        // Merge with any SSE-delivered messages (dedupe by
        // timestamp+role+text).
        for (var i = 0; i < data.transcript.length; i++) {
          var raw = data.transcript[i];
          var msg = {
            role: raw.role || "assistant",
            text: raw.text || "",
            timestamp: raw.timestamp || 0
          };
          if (!subsTranscriptHas(sub, msg)) sub.transcript.push(msg);
        }
      }
      renderSubsTranscript(sub);
    }).catch(function () {
      sub._transcriptLoading = false;
      renderSubsTranscript(sub);
    });
  }

  function buildSubsInputRow(sub) {
    var inputRow = document.createElement("div");
    inputRow.className = "subs-input-row";

    var msgArea = document.createElement("textarea");
    msgArea.rows = 1;
    msgArea.placeholder = "Reply to this subsession…";
    msgArea.setAttribute("aria-label",
      "Reply to subsession: " + (sub.title || "untitled"));
    // Restore the draft so a re-render doesn't eat a half-typed reply.
    if (sub._draft) msgArea.value = sub._draft;
    msgArea.addEventListener("input", function () {
      sub._draft = msgArea.value;
    });

    var sendMsgBtn = document.createElement("button");
    sendMsgBtn.type = "button";
    sendMsgBtn.className = "subs-send-btn";
    sendMsgBtn.textContent = "Send";
    sendMsgBtn.title = "Send this reply to the subsession (Enter)";
    sendMsgBtn.addEventListener("click", function () {
      var text = msgArea.value.trim();
      if (!text) return;
      sendSubsessionMessage(sub, text);
    });

    msgArea.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        sendMsgBtn.click();
      }
    });

    msgArea.addEventListener("focus", function () {
      selectedSubId = sub.subsession_id;
    });

    sub._msgInput = msgArea;
    sub._msgBtn = sendMsgBtn;
    inputRow.appendChild(msgArea);
    inputRow.appendChild(sendMsgBtn);
    return inputRow;
  }

  function sendSubsessionMessage(sub, text) {
    if (sub._msgBtn) {
      sub._msgBtn.disabled = true;
      sub._msgBtn.textContent = "Sending…";
    }
    if (sub._msgInput) sub._msgInput.disabled = true;
    var url = apiBase() + "/subsessions/" +
              encodeURIComponent(sub.subsession_id) + "/message";
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text })
    }).then(function (resp) {
      if (resp.status === 409) {
        showError("This subsession has already finished — " +
                  "reply in the main chat instead.");
      } else if (!resp.ok) {
        showError("Failed to send message (HTTP " + resp.status + ")");
      } else {
        // Accepted (202) — don't append a local bubble directly; re-sync
        // the transcript from the server instead so the message renders
        // even when the /events SSE channel is down (the echoed
        // subsession_message frame is published fire-and-forget and is
        // silently dropped if this tab holds no live subscription for the
        // owner session). loadSubsTranscript dedupes by exact
        // timestamp+role+text, so an SSE echo arriving too is harmless.
        sub._draft = "";
        if (sub._msgInput) sub._msgInput.value = "";
        loadSubsTranscript(sub);
      }
    }).catch(function (err) {
      showError("Failed to send message: " + (err.message || "Network error"));
    }).then(function () {
      // Re-enable the CURRENT input (a re-render may have replaced the DOM;
      // buildSubsInputRow keeps the entry-stored refs up to date).
      if (sub._msgBtn) {
        sub._msgBtn.disabled = false;
        sub._msgBtn.textContent = "Send";
      }
      if (sub._msgInput) {
        sub._msgInput.disabled = false;
        sub._msgInput.focus();
      }
    });
  }

  function closeSubsession(sub, closeBtn) {
    closeBtn.disabled = true;
    closeBtn.textContent = "Closing…";
    sub._closing = true;
    var url = apiBase() + "/subsessions/" +
              encodeURIComponent(sub.subsession_id) + "/close";
    fetch(url, { method: "POST" })
      .then(function (resp) {
        if (!resp.ok) {
          sub._closing = false;
          resp.text().then(function (body) {
            showError("Close failed: " + (body || resp.statusText));
          }).catch(function () {
            showError("Close failed: " + resp.statusText);
          });
          renderSubsessionsList();
        }
        // On success, rely on the subsession_closed SSE frame to
        // mark the row terminal.
      })
      .catch(function (err) {
        sub._closing = false;
        showError("Close failed: " + (err.message || "Network error"));
        renderSubsessionsList();
      });
  }

  // ---- Subsession focus mode -------------------------------------------

  /** Expand *sub* to fill the screen; ESC / button restores the layout. */
  function toggleSubsFocus(sub) {
    if (focusedSubId === sub.subsession_id) {
      exitSubsFocus();
    } else {
      enterSubsFocus(sub);
    }
  }

  function enterSubsFocus(sub) {
    // Ensure the panel is visible (shortcut may trigger while hidden).
    openSubsessionsPanel();
    // Switching focus from another subsession: restore that one's prior
    // expanded state before saving this one's.
    if (focusedSubId !== null && focusedSubId !== sub.subsession_id) {
      var prev = subsById[focusedSubId];
      if (prev && typeof focusPrevExpanded === "boolean") {
        prev.expanded = focusPrevExpanded;
      }
    }
    focusedSubId = sub.subsession_id;
    selectedSubId = sub.subsession_id;
    // Force the row open so the user can read the conversation.
    focusPrevExpanded = sub.expanded;
    sub.expanded = true;
    document.body.classList.add("subs-focus-mode");
    // Render fresh: add .focused to the row, rebuild header button text.
    renderSubsessionsList();
    var headerTitle = subsPanel.querySelector(".subs-header-title");
    if (headerTitle) headerTitle.textContent = sub.title || "(untitled)";
    announceScreenReader(
      "Subsession \"" + (sub.title || "untitled") +
      "\" focused — press Escape to restore the multi-panel layout."
    );
  }

  function exitSubsFocus(rerender) {
    var wasFocusedId = focusedSubId;
    focusedSubId = null;
    document.body.classList.remove("subs-focus-mode");
    // Restore the expanded state the row had before focus.
    var prevSub = wasFocusedId ? subsById[wasFocusedId] : null;
    if (prevSub && typeof focusPrevExpanded === "boolean") {
      prevSub.expanded = focusPrevExpanded;
    }
    focusPrevExpanded = null;
    var headerTitle = subsPanel.querySelector(".subs-header-title");
    if (headerTitle) headerTitle.textContent = "Subsessions";
    if (rerender !== false) renderSubsessionsList();
    announceScreenReader("Focus mode exited — multi-panel layout restored.");
  }

  /** Visible, non-terminal subsession for Ctrl+Shift+F shortcut. */
  function getSelectedSub() {
    var order = subsDisplayOrder();
    var visible = [];
    for (var i = 0; i < order.length; i++) {
      if (showTerminalSubs || !isSubsTerminal(order[i])) visible.push(order[i]);
    }
    if (visible.length === 0) return null;
    if (selectedSubId) {
      for (var j = 0; j < visible.length; j++) {
        if (visible[j].subsession_id === selectedSubId) return visible[j];
      }
    }
    return visible[0];
  }

  /** Announce a layout change to screen readers via #sr-announce. */
  function announceScreenReader(msg) {
    var el = document.getElementById("sr-announce");
    if (!el) return;
    el.textContent = "";
    window.setTimeout(function () { el.textContent = msg; }, 30);
  }

  // ---- Live countdown for periodic rows (wall-clock; 1s tick) ----------
  function subsCountdownLabel(sub) {
    if (!sub.next_run_at) return "";
    var remaining = Math.floor(sub.next_run_at - Date.now() / 1000);
    var when;
    if (remaining <= 0) {
      when = "due";
    } else {
      var h = Math.floor(remaining / 3600);
      var m = Math.floor((remaining % 3600) / 60);
      var s = remaining % 60;
      if (h > 0) when = "in " + h + "h " + m + "m";
      else if (m > 0) when = "in " + m + "m " + s + "s";
      else when = "in " + s + "s";
    }
    return " • next run " + when;
  }

  setInterval(function () {
    for (var i = 0; i < subsOrder.length; i++) {
      var sub = subsById[subsOrder[i]];
      if (sub && sub._countdownEl) {
        sub._countdownEl.textContent = subsCountdownLabel(sub);
      }
    }
  }, 1000);

  function truncateText(text, maxLen) {
    if (!text) return "";
    var firstLine = text.split("\n")[0];
    if (firstLine.length <= maxLen) return firstLine;
    return firstLine.slice(0, maxLen) + "…";
  }

  function formatInterval(seconds) {
    if (seconds < 60) return seconds + "s";
    if (seconds < 3600) return (seconds / 60).toFixed(1).replace(/\.0$/, "") + "m";
    return (seconds / 3600).toFixed(1).replace(/\.0$/, "") + "h";
  }

  // ---- Subsessions snapshot on load -------------------------------------
  function fetchSubsessions() {
    var url = apiBase() + "/subsessions" +
              "?session_id=" + encodeURIComponent(activeSessionId);

    fetch(url, { method: "GET" }).then(function (response) {
      if (!response.ok) return;
      return response.json();
    }).then(function (data) {
      if (!data || !Array.isArray(data.subsessions)) return;
      // Rebuild the store from the snapshot, preserving UI-only state
      // (expanded rows, loaded transcripts, drafts) for surviving ids.
      var old = subsById;
      subsById = {};
      subsOrder = [];
      for (var i = 0; i < data.subsessions.length; i++) {
        var snap = data.subsessions[i];
        var sid = snap.subsession_id;
        if (!sid) continue;
        var sub = old[sid] || newSubsEntry();
        applySubsSnapshot(sub, snap);
        // If unread messages arrived before this snapshot, fold them in.
        applyPendingUnread(sub);
        subsById[sid] = sub;
        subsOrder.push(sid);
      }
      renderSubsessionsList();
      // If the loaded session has active (non-terminal) subsessions,
      // open the panel so the operator can see them — this matters
      // especially for periodic sessions whose subsessions ran
      // in the background before the operator selected the session.
      if (subsOrder.length > 0) {
        var hasActive = false;
        for (var j = 0; j < subsOrder.length; j++) {
          if (!isSubsTerminal(subsById[subsOrder[j]])) {
            hasActive = true;
            break;
          }
        }
        if (hasActive) {
          openSubsessionsPanel();
        }
      }
    }).catch(function () {
      // Silently ignore fetch failures — the panel just stays stale.
    });
  }

  function clearSubsessions() {
    subsById = {};
    subsOrder = [];
    renderSubsessionsList();
  }

  function openSubsessionsPanel() {
    if (!subsPanel.classList.contains("visible")) {
      subsPanel.classList.add("visible");
      setSubsPanelVisible(true);
      positionResizeHandle();
    }
  }

  // ---- Helpers ---------------------------------------------------------
  function scrollToBottom() {
    // Only auto-scroll if the user is already near the bottom —
    // don't hijack the viewport when they've scrolled up to read history.
    var threshold = 50; // px from bottom
    if ((chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight) < threshold) {
      chatEl.scrollTop = chatEl.scrollHeight;
    }
  }

  function scheduleForceScrollToBottom() {
    // Defer the unconditional scroll until after the browser has laid out
    // newly inserted DOM (double rAF).  Without this, scrollHeight is
    // stale and the scroll lands short of the true bottom — a long-standing
    // bug on session switch and initial page load.
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        chatEl.scrollTop = chatEl.scrollHeight;
      });
    });
  }

  function setConnectionStatus(ok) {
    if (ok) { connDot.classList.remove("error"); }
    else    { connDot.classList.add("error"); }
  }

  function showError(message) {
    errorMsgEl.textContent = message;
    errorBanner.classList.add("visible");
    setConnectionStatus(false);
  }

  function hideError() {
    errorBanner.classList.remove("visible");
    // Don't immediately flip to green — only go green on next successful
    // stream start or completion.
  }

  errorDismiss.addEventListener("click", function () { hideError(); });

  // ---- Memory (cognee) health banner -----------------------------------
  // The memory backend fails in ways that are invisible from the chat: recall
  // returns "" on any fault, so replies keep streaming, just without any
  // recalled context. GET /health carries the backend's own degraded flag —
  // poll it and make the degradation visible instead of leaving the operator
  // to infer it from the container logs.
  var MEMORY_POLL_MS = 60000;
  var memoryBanner = document.getElementById("memory-banner");

  function pollMemoryHealth() {
    return fetch(apiBase() + "/health", { method: "GET" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (data) renderMemoryBanner(memoryBanner, data.memory);
      })
      .catch(function () {
        // A failed /health poll says nothing about the memory backend (the
        // connection dot already covers reachability) — leave the banner as-is
        // rather than flapping it on every transient network blip.
      });
  }

  pollMemoryHealth();
  setInterval(pollMemoryHealth, MEMORY_POLL_MS);

  // ---- Typing indicator ------------------------------------------------
  function showTypingIndicator() {
    if (typingIndicatorEl) return;            // already visible
    typingIndicatorEl = document.createElement("div");
    typingIndicatorEl.id = "typing-indicator";
    typingIndicatorEl.className = "visible";
    for (var i = 0; i < 3; i++) {
      var dot = document.createElement("span");
      dot.className = "dot";
      typingIndicatorEl.appendChild(dot);
    }
    var label = document.createElement("span");
    label.className = "activity-label";
    typingIndicatorEl.appendChild(label);
    chatEl.appendChild(typingIndicatorEl);
    scrollToBottom();
  }

  function hideTypingIndicator() {
    if (!typingIndicatorEl) return;
    typingIndicatorEl.remove();
    typingIndicatorEl = null;
  }

  // Live "what's it doing" caption inside the typing indicator, fed by
  // "activity" frames on the /events channel (see handleActivityFrame).
  // A no-op when no turn is in flight (typingIndicatorEl is null) — activity
  // frames only arrive during one, but a frame arriving just after the
  // indicator was hidden (race with the "done" frame) must not resurrect it.
  function updateActivityLabel(text) {
    if (!typingIndicatorEl) return;
    var label = typingIndicatorEl.querySelector(".activity-label");
    if (label) label.textContent = text;
  }

  function handleActivityFrame(frame) {
    var text;
    if (frame.kind === "tool_call") {
      text = "🔧 " + (frame.tool_name || "tool") + "(" + (frame.detail || "") + ")";
    } else if (frame.kind === "tool_result") {
      text = frame.is_error ? "⚠️ tool error — " + frame.detail : "✓ " + frame.detail;
    } else if (frame.kind === "thinking") {
      text = "💭 thinking…";
    } else {
      return;  // "text" kind: the real reply arrives via the normal token frame
    }
    updateActivityLabel(text);
  }

  // ---- Send button busy state ------------------------------------------
  // While the assistant is replying the send button LOOKS disabled (and its
  // tooltip explains what's happening) but stays clickable so messages
  // typed mid-reply are queued (see messageQueue).
  function updateSendBusy() {
    if (isBusy()) {
      sendBtn.classList.add("busy");
      sendBtn.title = "Assistant is replying — new messages are queued";
    } else {
      sendBtn.classList.remove("busy");
      sendBtn.title = "Send message (Enter)";
    }
    updateCancelQueuedButton();
  }

  // ---- Markdown rendering ----------------------------------------------
  function renderMarkdown(raw) {
    if (typeof marked === "undefined" || typeof DOMPurify === "undefined") {
      // Fallback: escape and wrap with pre-wrap (graceful degradation).
      var d = document.createElement("div");
      d.textContent = raw;
      return d.innerHTML;
    }
    var html = marked.parse(raw);
    return DOMPurify.sanitize(html);
  }

  // ---- Suggested answer options ----------------------------------------
  // Chip rendering and stale-disable helpers live in ./suggestions.js.

  // Submit a suggestion as a user reply in the main chat.
  function submitMainChatSuggestion(text) {
    msgInput.value = text;
    submitMessage();
  }

  // ---- Message bubbles -------------------------------------------------
  function clearChatBubbles() {
    // Remove all bubble elements, typing indicator, and inline notices
    // from the chat container so no messages bleed across sessions.
    var children = chatEl.querySelectorAll(".bubble, #typing-indicator, .suggestion-chips");
    for (var i = 0; i < children.length; i++) {
      children[i].remove();
    }
    currentAssistantBubble = null;
    rawAssistantText = "";
    typingIndicatorEl = null;
    if (lastModelTimestampEl) { lastModelTimestampEl.remove(); lastModelTimestampEl = null; }
    // Also clear queued messages — they belong to the old session.
    messageQueue = [];
    // Drop any in-progress re-attach render — its bubble is being removed.
    reattachActive = false;
    reattachTurnId = null;
    // Reset state so the composer is not blocked.
    if (state === "sending" || state === "streaming") {
      state = "idle";
    }
    updateSendBusy();
  }

  // Insert the compaction summary card at the top of the transcript, with a
  // toggle that reveals/hides the turns it covers.  The hidden bubbles are
  // already in the DOM (rendered by loadHistory) so the toggle is instant
  // and no second fetch is needed.
  function insertCompactedSummary(summaryText, coveredTurns, hiddenEls) {
    var card = document.createElement("div");
    card.className = "bubble assistant compacted-summary";
    var title = document.createElement("div");
    title.className = "compacted-summary-title";
    title.textContent = "Summary of the earlier conversation";
    card.appendChild(title);
    var body = document.createElement("div");
    body.className = "compacted-summary-body";
    body.innerHTML = renderMarkdown(summaryText);
    card.appendChild(body);
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "compacted-toggle";
    var expanded = false;
    function label() {
      return (expanded ? "Hide the " : "Show the ") + coveredTurns +
        " earlier exchange" + (coveredTurns === 1 ? "" : "s") +
        (expanded ? "" : " covered by this summary");
    }
    toggle.textContent = label();
    toggle.addEventListener("click", function () {
      expanded = !expanded;
      for (var k = 0; k < hiddenEls.length; k++) {
        hiddenEls[k].classList.toggle("compacted-hidden", !expanded);
      }
      toggle.textContent = label();
    });
    card.appendChild(toggle);
    // Place the card where the covered turns begin (the top of the list).
    chatEl.insertBefore(card, chatEl.firstChild);
    return card;
  }

  function addUserBubble(text) {
    var div = document.createElement("div");
    div.className = "bubble user";
    div.innerHTML = renderMarkdown(text);
    chatEl.appendChild(div);
    scrollToBottom();
    return div;
  }

  function addAssistantBubble(text) {
    var div = document.createElement("div");
    div.className = "bubble assistant";
    var parsed = parseSuggestions(text);
    div.innerHTML = renderMarkdown(parsed.cleanText);
    chatEl.appendChild(div);
    if (parsed.suggestions && parsed.suggestions.length > 0) {
      renderSuggestionChips(parsed.suggestions, submitMainChatSuggestion, div);
    }
    scrollToBottom();
    return div;
  }

  // Main-chat notification bubbles for TOP-LEVEL subsession events
  // (the /events dispatcher only calls this when parent_id is null).
  function addNotificationBubble(frame) {
    var div = document.createElement("div");
    var typeClass = "";
    var text = "";
    var title = frame.title || "(untitled)";
    if (frame.type === "subsession_result") {
      typeClass = "result";
      var runLabel = (frame.run !== undefined && frame.run !== null)
        ? " run " + frame.run : "";
      var resultText = frame.text || "";
      if (resultText.length > 200) resultText = resultText.slice(0, 200) + "…";
      text = "⏱ '" + title + "'" + runLabel + ": " + resultText;
    } else if (frame.type === "subsession_closed") {
      typeClass = "completed";
      text = "Subsession '" + title + "' closed (" +
             (frame.reason || "done") + "): " + (frame.summary || "");
    } else if (frame.type === "subsession_failed") {
      typeClass = "failed";
      text = "Subsession '" + title + "' failed: " +
             (frame.error || frame.summary || "");
    } else {
      return; // ignore unknown types
    }
    div.className = "bubble notification " + typeClass;
    div.innerHTML = renderMarkdown(text);
    chatEl.appendChild(div);
    scrollToBottom();
  }

  function createAssistantBubble() {
    if (currentAssistantBubble) return currentAssistantBubble;
    // A new assistant turn supersedes any pending decision — retire its chips.
    disableStaleSuggestionChips(chatEl);
    var div = document.createElement("div");
    div.className = "bubble assistant";
    div.textContent = "";
    chatEl.appendChild(div);
    currentAssistantBubble = div;
    rawAssistantText = "";
    return div;
  }

  function appendToken(token) {
    var bubble = createAssistantBubble();
    rawAssistantText += token;
    // Hide a trailing (partial or complete) ```suggestions block while
    // streaming so the raw fence never shows; finaliseAssistantBubble
    // re-parses the full text and renders the chips.
    bubble.textContent = stripStreamingSuggestions(rawAssistantText);
    scrollToBottom();
  }

  function finaliseAssistantBubble() {
    if (currentAssistantBubble) {
      if (rawAssistantText === "") {
        currentAssistantBubble.textContent = "(empty response)";
      } else {
        var parsed = parseSuggestions(rawAssistantText);
        currentAssistantBubble.innerHTML = renderMarkdown(parsed.cleanText);
        if (parsed.suggestions && parsed.suggestions.length > 0) {
          renderSuggestionChips(parsed.suggestions, submitMainChatSuggestion, currentAssistantBubble);
        }
      }
      scrollToBottom();
    }
    currentAssistantBubble = null;
    rawAssistantText = "";
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // processSSEStream is now in sse-parser.js (imported at module top).

  // ---- Persistent /events SSE channel ----------------------------------
  function openEventStream() {
    eventsStreamIntentionallyClosed = false;
    // Abort any prior stream before opening a new one so we never run two
    // /events fetches at once (each would hold its own server-side EventBus
    // subscription → duplicate frames). Also cancel a pending reconnect.
    // Bumping the generation first makes every callback captured by the
    // prior stream (including its pump's AbortError catch) a stale no-op.
    var gen = ++eventStreamGeneration;
    if (eventStreamReconnectTimer) {
      clearTimeout(eventStreamReconnectTimer);
      eventStreamReconnectTimer = null;
    }
    if (eventStreamWatchdogTimer) {
      clearInterval(eventStreamWatchdogTimer);
      eventStreamWatchdogTimer = null;
    }
    if (eventStreamAbortController) {
      try { eventStreamAbortController.abort(); } catch (_) {}
      eventStreamAbortController = null;
    }
    var eventsUrl = apiBase() + "/events" +
                    "?session_id=" + encodeURIComponent(activeSessionId) +
                    "&owner_id=" + encodeURIComponent(ownerFor(activeSessionId));

    // Read-liveness watchdog: the server writes a keepalive comment every
    // 5s, so a healthy stream always produces reads. A silently-dead TCP
    // connection (laptop sleep, network change) leaves reader.read()
    // pending forever with no error — without this, the tab keeps a
    // zombie /events subscription until reload.
    var lastActivity = Date.now();

    var eventsController = {
      onActivity: function () {
        lastActivity = Date.now();
      },
      onData: function (raw) {
        if (gen !== eventStreamGeneration) return;  // stale stream
        var frame;
        try { frame = JSON.parse(raw); }
        catch (_) { return; /* skip unparsable frames */ }

        if (frame.type === "subsession_started") {
          // Full snapshot — insert/replace the row.
          upsertSubsession(frame);
          // A user_chat subsession starting means the agent is asking the
          // user something — make it prominent: open the panel (the row
          // itself auto-expands in applySubsSnapshot).
          if (frame.kind === "user_chat") {
            openSubsessionsPanel();
          }
        } else if (frame.type === "subsession_updated") {
          // Partial update — merge into the existing row.
          upsertSubsession(frame);
        } else if (frame.type === "subsession_message") {
          // Transcript message (includes the echo of our own POSTs).
          handleSubsessionMessage(frame);
        } else if (frame.type === "subsession_result") {
          // Periodic run result — surface top-level results in the chat.
          if (frame.parent_id === null || frame.parent_id === undefined) {
            addNotificationBubble(frame);
          }
        } else if (frame.type === "subsession_closed" ||
                   frame.type === "subsession_failed") {
          // Terminal frames — mark the row closed/failed and surface
          // top-level completions in the chat.
          applySubsTerminalFrame(frame);
          if (frame.parent_id === null || frame.parent_id === undefined) {
            addNotificationBubble(frame);
          }
        } else if (frame.type === "activity") {
          // Live claudeSDK tool/thinking activity for the in-flight turn.
          handleActivityFrame(frame);
        } else if (frame.type === "agent_message") {
          // A background-triggered agent reply (e.g. reacting to a
          // subsession closing) — not a live /chat response, so it arrives
          // here instead of as a token/done frame. Render it as a normal
          // assistant bubble.
          if (frame.text) addAssistantBubble(frame.text);
        } else if (frame.type === "session_model") {
          // The agent escalated this session to a stronger model. Update the
          // badges immediately; the next session-list refetch confirms it.
          if (frame.session_id === activeSessionId) {
            renderActiveModel(frame.model_name, !!frame.escalated);
            if (frame.model_level != null) setActiveModelLevel(frame.model_level);
          }
          for (var mi = 0; mi < sessionsList.length; mi++) {
            if (sessionsList[mi].session_id === frame.session_id) {
              sessionsList[mi].model_name = frame.model_name;
              sessionsList[mi].model_level = frame.model_level;
              sessionsList[mi].model_escalated = !!frame.escalated;
              break;
            }
          }
          renderSessionList({ sessions: sessionsList });
        } else if (frame.type === "notification") {
          // Push notification from the agent's notify_user tool.
          // The browser shows a native notification when permission is
          // granted; silently ignored otherwise (same as the server-side
          // silent drop when no client is connected).
          if ("Notification" in window && Notification.permission === "granted") {
            new Notification(frame.title || "Notification", {
              body: frame.body || "",
            });
          }
          // Refresh the unread badge/panel. Both live and replayed missed
          // notifications are persisted server-side with read=false, so the
          // unread API reflects them regardless of desktop-permission state.
          fetchUnreadNotifications();
        } else if (frame.type === "chat_turn_started") {
          handleReattachStart(frame);
        } else if (frame.type === "chat_turn_resume") {
          handleReattachResume(frame);
        } else if (frame.type === "chat_token") {
          handleReattachToken(frame);
        } else if (frame.type === "chat_turn_done") {
          // If this session has a background event stream (the user queued
          // messages, then switched away), drain those messages now that
          // the turn completed — even though the session isn't focused.
          var bgSid = frame.session_id;
          if (bgSid && backgroundStreams[bgSid]) {
            drainBackgroundSession(bgSid);
          }
          handleReattachDone(frame);
        } else if (frame.type === "chat_turn_error") {
          // A turn that errored won't produce chat_turn_done — drain any
          // background queued messages so they're not stuck forever.
          var bgSidErr = frame.session_id;
          if (bgSidErr && backgroundStreams[bgSidErr]) {
            drainBackgroundSession(bgSidErr);
          }
          handleReattachError(frame);
        }
        // ignore unknown types gracefully
      },
      onDone: function () {
        // Stream closed by server — reconnect after a short delay,
        // unless the stream was intentionally closed (session switch)
        // or superseded by a newer stream.
        if (gen !== eventStreamGeneration) return;  // stale stream
        if (eventsStreamIntentionallyClosed) return;
        scheduleEventReconnect();
      },
      error: function (_err) {
        // Network error or stream failure — reconnect after a short delay.
        // A stale stream's pump lands here with AbortError when a newer
        // openEventStream() aborted it; it must NOT schedule a reconnect
        // (that reconnect would abort the healthy new stream, forever).
        if (gen !== eventStreamGeneration) return;  // stale stream
        if (eventsStreamIntentionallyClosed) return;
        scheduleEventReconnect();
      }
    };

    // Create a new AbortController so closeEventStream() can abort this fetch.
    eventStreamAbortController = new AbortController();
    var abortController = eventStreamAbortController;

    fetch(eventsUrl, {
      method: "GET",
      signal: abortController.signal
    }).then(function (response) {
      if (gen !== eventStreamGeneration) return;  // stale stream
      if (!response.ok) {
        scheduleEventReconnect();
        return;
      }
      var contentType = response.headers.get("content-type") || "";
      if (contentType.indexOf("text/event-stream") === -1) {
        scheduleEventReconnect();
        return;
      }
      // (Re)connected — re-sync the subsessions snapshot so any frames
      // missed while disconnected are reflected in the panel.
      fetchSubsessions();
      lastActivity = Date.now();
      eventStreamWatchdogTimer = setInterval(function () {
        if (gen !== eventStreamGeneration) return;  // cleared by successor
        if (Date.now() - lastActivity > 20000) {
          // No bytes (not even the 5s keepalive) for 20s — the connection
          // is dead even though reader.read() never rejected. Tear it
          // down and reconnect.
          clearInterval(eventStreamWatchdogTimer);
          eventStreamWatchdogTimer = null;
          try { abortController.abort(); } catch (_) {}
          scheduleEventReconnect();
        }
      }, 5000);
      var parser = processSSEStream(response.body, eventsController);
      parser.start();
    }).catch(function (err) {
      // Don't reconnect if aborted (session switch) or superseded.
      if (gen !== eventStreamGeneration) return;
      if (err && err.name === "AbortError") return;
      scheduleEventReconnect();
    });
  }

  // ---- History loading -------------------------------------------------
  function loadHistory(onComplete) {
    var historyUrl = apiBase() + "/history" +
                     "?session_id=" + encodeURIComponent(activeSessionId) +
                     "&owner_id=" + encodeURIComponent(ownerFor(activeSessionId));

    fetch(historyUrl, { method: "GET" }).then(function (response) {
      if (!response.ok) return;
      return response.json();
    }).then(function (data) {
      if (!data || !Array.isArray(data.turns)) return;
      var turns = data.turns;
      // A compacted (summarised) session opens on its summary: the turns
      // the summary covers are rendered but hidden behind an explicit
      // "show earlier messages" toggle, the rest render normally.
      var compactedIndex = 0;
      if (typeof data.compacted_summary === "string" && data.compacted_summary &&
          typeof data.compacted_turn_index === "number" && data.compacted_turn_index > 0) {
        compactedIndex = Math.min(data.compacted_turn_index, turns.length);
      }
      var hiddenEls = [];
      for (var i = 0; i < turns.length; i++) {
        var turn = turns[i];
        if (Array.isArray(turn) && turn.length >= 2) {
          var u = addUserBubble(turn[0]);
          var a = addAssistantBubble(turn[1]);
          if (i < compactedIndex) {
            u.classList.add("compacted-turn", "compacted-hidden");
            a.classList.add("compacted-turn", "compacted-hidden");
            hiddenEls.push(u, a);
          }
        }
      }
      if (compactedIndex > 0) {
        insertCompactedSummary(data.compacted_summary, compactedIndex, hiddenEls);
      }
      scheduleForceScrollToBottom();
      // Restore any saved draft (queued messages / pending images).
      restoreDraft();
    }).catch(function () {
      // Silently ignore network errors — empty chat is fine.
    }).then(function () {
      // Run the continuation (e.g. opening the foreground /events stream) only
      // AFTER the persisted transcript has rendered. Opening the stream earlier
      // races the history fetch: the server primes a chat_turn_resume frame for
      // an in-flight turn on subscribe, so if that frame is handled before
      // history renders, the in-flight round (user input + live progress) is
      // appended *above* the persisted turns and then scrolled out of view by
      // the force-scroll-to-bottom, hiding it until the turn completes. Loading
      // history first guarantees the re-attached round renders below history.
      if (typeof onComplete === "function") onComplete();
    });
  }

  // ---- Send logic ------------------------------------------------------
  function submitMessage() {
    // Read and trim input.
    var message = msgInput.value.trim();

    // Snapshot the current pending images for this message.
    var imagesForSend = pendingImages.slice();

    // Require at least text OR images.
    if (!message && imagesForSend.length === 0) return;

    resetIdleTimer();

    msgInput.value = "";
    // Auto-resize textarea back to 1 row
    msgInput.style.height = "";

    hideError();
    clearAttachError();
    clearPendingImages();

    // The operator answered — retire any pending suggestion chips so an old
    // chip can never submit a stale reply to a superseded question.
    disableStaleSuggestionChips(chatEl);

    // Create the user bubble — if we're busy, mark it queued.
    var el = addUserBubble(message);
    // Append image thumbnails to the user bubble.
    if (imagesForSend.length > 0) {
      var imgsDiv = document.createElement("div");
      imgsDiv.className = "bubble-images";
      for (var i = 0; i < imagesForSend.length; i++) {
        var thumb = document.createElement("img");
        thumb.src = imagesForSend[i].objectURL;
        thumb.alt = imagesForSend[i].file.name;
        imgsDiv.appendChild(thumb);
      }
      el.insertBefore(imgsDiv, el.firstChild);
    }

    var messageId = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID()
      : (Math.random().toString(36).slice(2) + Date.now().toString(36));

    if (isBusy()) {
      el.classList.add("queued");
      addCancelButton(el, messageId);
    }

    messageQueue.push({ text: message, el: el, images: imagesForSend, messageId: messageId });
    drainQueue();
    updateCancelQueuedButton();
  }

  function drainQueue() {
    // Do not dispatch while a request is in flight.
    if (isBusy()) return;
    if (messageQueue.length === 0) { updateCancelQueuedButton(); saveDraft(); return; }

    var item = messageQueue.shift();
    item.el.classList.remove("queued");
    removeCancelButton(item.el);
    startRequest(item.text, item.images || [], item.messageId);
    updateCancelQueuedButton();
  }

  // ---- Cancel queued messages ------------------------------------------

  function addCancelButton(bubbleEl, messageId) {
    // Only add if one isn't already present.
    if (bubbleEl.querySelector(".cancel-queued-btn")) return;
    var btn = document.createElement("button");
    btn.className = "cancel-queued-btn";
    btn.type = "button";
    btn.textContent = "\u00d7";
    btn.title = "Cancel this queued message";
    btn.setAttribute("aria-label", "Cancel queued message");
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      cancelQueuedMessage(messageId, bubbleEl);
    });
    bubbleEl.appendChild(btn);
  }

  function removeCancelButton(bubbleEl) {
    var btn = bubbleEl.querySelector(".cancel-queued-btn");
    if (btn) btn.remove();
  }

  function cancelQueuedMessage(messageId, bubbleEl) {
    // 1. Try to remove from the client-side queue (not yet dispatched).
    var found = false;
    for (var i = 0; i < messageQueue.length; i++) {
      if (messageQueue[i].messageId === messageId) {
        // Revoke any pending image object URLs.
        var imgs = messageQueue[i].images || [];
        for (var j = 0; j < imgs.length; j++) {
          if (imgs[j].objectURL) URL.revokeObjectURL(imgs[j].objectURL);
        }
        messageQueue.splice(i, 1);
        found = true;
        break;
      }
    }

    // 2. Remove the DOM bubble.
    if (bubbleEl && bubbleEl.parentNode) {
      bubbleEl.remove();
    }

    updateCancelQueuedButton();

    // 3. If not in client queue, ask the server (may be in coalescer).
    if (!found && activeSessionId) {
      fetch(apiBase() + "/chat/queue/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: activeSessionId,
          message_id: messageId
        })
      }).then(function (r) { return r.json(); }).then(function (data) {
        if (data && data.processing) {
          // Message already processing — it can't be cancelled.
          // The bubble is already removed; nothing more to do.
        }
      }).catch(function () {
        // Best-effort; bubble already removed.
      });
    }
  }

  function cancelAllQueued() {
    // 1. Revoke image URLs and clear the client-side queue.
    for (var i = 0; i < messageQueue.length; i++) {
      var imgs = messageQueue[i].images || [];
      for (var j = 0; j < imgs.length; j++) {
        if (imgs[j].objectURL) URL.revokeObjectURL(imgs[j].objectURL);
      }
      if (messageQueue[i].el && messageQueue[i].el.parentNode) {
        messageQueue[i].el.remove();
      }
    }
    messageQueue = [];

    updateCancelQueuedButton();

    // 2. Also tell the server to drop any pending batch.
    if (activeSessionId) {
      fetch(apiBase() + "/chat/queue/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: activeSessionId })
      }).catch(function () {
        // Best-effort.
      });
    }
  }

  function updateCancelQueuedButton() {
    if (messageQueue.length > 0 && isBusy()) {
      cancelQueuedBtn.style.display = "";
      cancelQueuedBtn.textContent = "Cancel queued (" + messageQueue.length + ")";
    } else {
      cancelQueuedBtn.style.display = "none";
    }
  }

  function startRequest(message, pendingForSend, messageId) {
    // The existing network/streaming body of the old sendMessage, minus
    // the user-bubble creation (the bubble already exists).
    showTypingIndicator();
    setConnectionStatus(true);

    state = "sending";
    updateSendBusy();

    // Encode images if any; then POST.
    var encodePromise = pendingForSend.length > 0
      ? encodeImagesFromList(pendingForSend)
      : Promise.resolve([]);

    encodePromise.then(function (encodedImages) {
      doPost(message, encodedImages, messageId);
    }).catch(function (err) {
      hideTypingIndicator();
      showError(err.message || "Failed to encode images");
      state = "error";
      updateSendBusy();
    });
  }

  function encodeImagesFromList(list) {
    var promises = [];
    for (var i = 0; i < list.length; i++) {
      promises.push(encodeImage(list[i].file));
    }
    return Promise.all(promises);
  }

  function doPost(message, encodedImages, messageId) {
    var requestSessionId = activeSessionId;
    // Track this POST so switchSession can abandon it (its turn keeps running
    // server-side and is re-attached via /events on return) and so the
    // /events echo of this same turn is ignored while we own the POST.
    var abortController = new AbortController();
    activePostAbort = abortController;
    activePostSessionId = requestSessionId;
    function clearPost() {
      if (activePostAbort === abortController) {
        activePostAbort = null;
        activePostSessionId = null;
      }
    }
    var streamController = {
      onData: function (raw) {
        // Ignore frames from a request that started on a different session.
        if (activeSessionId !== requestSessionId) return;
        var frame;
        try { frame = JSON.parse(raw); }
        catch (_) { return; /* skip unparsable frames */ }

        if (frame.type === "token") {
          // First token — hide typing indicator and enter streaming.
          if (state === "sending") {
            hideTypingIndicator();
            state = "streaming";
            // Remove the previous model-message timestamp.
            if (lastModelTimestampEl) { lastModelTimestampEl.remove(); lastModelTimestampEl = null; }
          }
          var content = frame.content;
          if (typeof content === "string") {
            appendToken(content);
          }
        } else if (frame.type === "done") {
          clearPost();
          hideTypingIndicator();
          finaliseAssistantBubble();
          setConnectionStatus(true);
          state = "idle";
          updateSendBusy();
          // Show the timestamp of the last model message.
          updateLastModelTimestamp(frame.timestamp);
          // The server may have rerouted this turn into a continuation
          // session (idle-timeout compaction) — adopt it before anything
          // below reads activeSessionId, so any queued messages target the
          // session the turn actually landed in.
          if (frame.session_id && frame.session_id !== requestSessionId) {
            adoptSession(frame.session_id);
          }
          // Automatically dispatch the next queued message (FIFO).
          drainQueue();
        } else if (frame.type === "error") {
          clearPost();
          hideTypingIndicator();
          finaliseAssistantBubble();
          showError(frame.message || "Server error");
          state = "error";
          updateSendBusy();
          // Dispatch the next queued message: after a failed turn the queued
          // message IS the retry the operator already typed — parking it
          // until they type something else left "(queued)" bubbles stuck
          // after every error (operator-reported). Each drain sends one
          // message, so a persistent failure empties the queue one visible
          // error at a time instead of looping.
          state = "idle";
          drainQueue();
        }
      },
      error: function (err) {
        // A session switch aborts this POST on purpose (the turn keeps
        // running server-side and re-attaches via /events). The abort
        // surfaces here through the SSE parser's read loop — it is benign,
        // so clean up quietly without flashing "operation was aborted".
        if (err && err.name === "AbortError") { clearPost(); return; }
        clearPost();
        hideTypingIndicator();
        finaliseAssistantBubble();
        showError(err.message || "Network error — is the server running?");
        state = "error";
        updateSendBusy();
        // Same as above: queued messages stay; next submit resumes draining.
      },
      // The SSE parser calls onDone when the stream ends gracefully (the
      // ReadableStream's reader.read() resolves with done=true).  For the
      // POST /chat path, an unexpected stream close while we are still
      // "streaming" (no "done" frame from the server) is an error.
      onDone: function () {
        if (state === "streaming") {
          clearPost();
          hideTypingIndicator();
          finaliseAssistantBubble();
          showError("Server closed the connection unexpectedly");
          state = "error";
          updateSendBusy();
        }
      }
    };

    var url = serverUrl();

    var body = {
      message: message,
      session_id: activeSessionId,
      owner_id: ownerFor(activeSessionId)
    };
    if (messageId) body.message_id = messageId;
    if (encodedImages.length > 0) {
      body.images = encodedImages;
    }

    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: abortController.signal
    }).then(function (response) {
      if (!response.ok) {
        // Non-2xx — try to read an error body, else show status.
        return response.text().then(function (txt) {
          var msg;
          try {
            var errBody = JSON.parse(txt);
            msg = errBody.error || errBody.detail || errBody.message || ("HTTP " + response.status);
          } catch (_) {
            msg = txt || ("HTTP " + response.status);
          }
          // Special-case 409 (session is busy / awaiting operator action).
          if (response.status === 409) {
            msg = "⏳ " + (msg || "Session is busy") +
                  " — wait for the current operation to complete, then try again.";
          }
          throw new Error(msg);
        });
      }

      var contentType = response.headers.get("content-type") || "";
      if (contentType.indexOf("text/event-stream") === -1) {
        // Not SSE — read body and show as error.
        return response.text().then(function (txt) {
          throw new Error("Unexpected response: " + txt.slice(0, 200));
        });
      }

      var parser = processSSEStream(response.body, streamController);
      parser.start();
    }).catch(function (err) {
      // Intentional abort on session switch — the turn continues server-side
      // and is re-attached via /events; not an error to surface.
      if (err && err.name === "AbortError") { clearPost(); return; }
      streamController.error(err);
    });
  }

  // ---- Event listeners -------------------------------------------------
  sendBtn.addEventListener("click", submitMessage);

  cancelQueuedBtn.addEventListener("click", function () {
    cancelAllQueued();
  });

  msgInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submitMessage();
    }
  });

  // Auto-resize textarea
  msgInput.addEventListener("input", function () {
    msgInput.style.height = "";
    msgInput.style.height = Math.min(msgInput.scrollHeight, 120) + "px";
  });

  // ---- Image attach / file picker --------------------------------------
  attachBtn.addEventListener("click", function () {
    fileInput.click();
  });

  fileInput.addEventListener("change", function () {
    if (fileInput.files && fileInput.files.length > 0) {
      validateAndAddFiles(fileInput.files);
      fileInput.value = "";  // reset so re-selecting the same file works
    }
  });

  // ---- Clipboard paste (image) -----------------------------------------
  msgInput.addEventListener("paste", function (e) {
    var items = (e.clipboardData && e.clipboardData.items);
    if (!items) return;
    var imageFiles = [];
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      if (item.type && item.type.indexOf("image/") === 0) {
        var blob = item.getAsFile();
        if (blob) imageFiles.push(blob);
      }
    }
    if (imageFiles.length > 0) {
      e.preventDefault();  // don't paste a broken image URL into the textarea
      validateAndAddFiles(imageFiles);
    }
  });

  // ---- Drag-and-drop onto the composer area -----------------------------
  var composerEl = document.getElementById("composer");
  composerEl.addEventListener("dragover", function (e) {
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  composerEl.addEventListener("drop", function (e) {
    e.preventDefault();
    var files = e.dataTransfer.files;
    if (files && files.length > 0) {
      validateAndAddFiles(files);
    }
  });

  // ---- Session panel toggle and resize --------------------------------
  sessionsToggle.addEventListener("click", function (e) {
    e.stopPropagation();
    var opening = !sessionsPanel.classList.contains("visible");
    sessionsPanel.classList.toggle("visible");
    setSessionsPanelVisible(sessionsPanel.classList.contains("visible"));
    if (opening) {
      positionSessionsResizeHandle();
      // Refresh session list from server when opening.
      refreshSessions();
      // Sync CSS custom property so the push-layout margin tracks the panel width.
      document.documentElement.style.setProperty('--sessions-width', sessionsPanel.getBoundingClientRect().width + 'px');
    } else {
      hideSessionsResizeHandle();
    }
  });

  sessionsDismiss.addEventListener("click", function (e) {
    e.stopPropagation();
    sessionsPanel.classList.remove("visible");
    setSessionsPanelVisible(false);
    hideSessionsResizeHandle();
  });

  sessionsPanel.addEventListener("click", function (e) {
    e.stopPropagation();
  });

  document.addEventListener("keydown", function (e) {
    // Focus mode owns Escape for the subsessions panel.
    if (e.key === "Escape" && focusedSubId === null &&
        sessionsPanel.classList.contains("visible")) {
      sessionsPanel.classList.remove("visible");
      setSessionsPanelVisible(false);
      hideSessionsResizeHandle();
    }
  });

  // "New chat" button
  newChatBtn.addEventListener("click", function () {
    newChatBtn.disabled = true;
    newChatBtn.textContent = "Creating\u2026";
    createNewSession().then(function (data) {
      newChatBtn.disabled = false;
      newChatBtn.textContent = "+ New chat";
      if (data && data.session_id) {
        // Switch into the new session.  We inline the switch steps
        // (rather than calling switchSession) to avoid a double render:
        // switchSession calls updateActiveHighlight() on the cached
        // (stale) list, then we'd call refreshSessions() for a second
        // render.  Here refreshSessions() handles the list update AND
        // the highlight in one pass.
        setActiveSessionId(data.session_id);
        clearChatBubbles();
        clearSubsessions();
        closeEventStream();
        // Open /events only after history renders (see switchSession/loadHistory)
        // so a re-attached in-flight round never renders above the transcript.
        loadHistory(openEventStream);
        fetchSubsessions();
        refreshSessions();
        resetIdleTimer();
      }
    }).catch(function (err) {
      newChatBtn.disabled = false;
      newChatBtn.textContent = "+ New chat";
      showError(err.message || "Failed to create session");
    });
  });


  // ---- Sessions panel resize ------------------------------------------
  var sessionsResizeDragging = false;
  var sessionsResizeStartX = 0;
  var sessionsResizeStartWidth = 0;

  function positionSessionsResizeHandle() {
    var rect = sessionsPanel.getBoundingClientRect();
    sessionsResizeHandle.style.display = "block";
    sessionsResizeHandle.style.left = (rect.right) + "px";
  }

  function hideSessionsResizeHandle() {
    sessionsResizeHandle.style.display = "none";
  }

  sessionsResizeHandle.addEventListener("mousedown", function (e) {
    e.preventDefault();
    sessionsResizeDragging = true;
    sessionsResizeStartX = e.clientX;
    sessionsResizeStartWidth = sessionsPanel.getBoundingClientRect().width;
    sessionsResizeHandle.classList.add("active");
    document.body.style.userSelect = "none";
  });

  document.addEventListener("mousemove", function (e) {
    if (!sessionsResizeDragging) return;
    var dx = e.clientX - sessionsResizeStartX;
    var newWidth = sessionsResizeStartWidth + dx;
    newWidth = Math.max(220, Math.min(newWidth, window.innerWidth * 0.8));
    sessionsPanel.style.width = newWidth + "px";
    positionSessionsResizeHandle();
    document.documentElement.style.setProperty('--sessions-width', newWidth + 'px');
  });

  document.addEventListener("mouseup", function () {
    if (!sessionsResizeDragging) return;
    sessionsResizeDragging = false;
    sessionsResizeHandle.classList.remove("active");
    document.body.style.userSelect = "";
    positionSessionsResizeHandle();
  });

  window.addEventListener("resize", function () {
    if (!sessionsPanel.classList.contains("visible")) return;
    var currentWidth = sessionsPanel.getBoundingClientRect().width;
    var maxWidth = window.innerWidth * 0.8;
    if (currentWidth > maxWidth) {
      sessionsPanel.style.width = maxWidth + "px";
    } else if (currentWidth < 220) {
      sessionsPanel.style.width = "220px";
    }
    positionSessionsResizeHandle();
    document.documentElement.style.setProperty('--sessions-width', sessionsPanel.getBoundingClientRect().width + 'px');
  });

  // ---- Subsessions panel toggle — no auto-close on outside click.
  var subsDismiss = subsPanel.querySelector(".dismiss");
  subsToggle.addEventListener("click", function (e) {
    e.stopPropagation();
    var opening = !subsPanel.classList.contains("visible");
    subsPanel.classList.toggle("visible");
    setSubsPanelVisible(subsPanel.classList.contains("visible"));
    if (opening) {
      // Opening the panel reveals every expanded transcript, so those
      // subsessions are now read — clear their unread state and recompute
      // ancestors so no badge sticks once the messages are on screen.
      for (var i = 0; i < subsOrder.length; i++) {
        var s = subsById[subsOrder[i]];
        if (s && s.expanded) markSubsessionRead(s);
      }
      renderSubsessionsList();
      positionResizeHandle();
      document.documentElement.style.setProperty('--subsessions-width', subsPanel.getBoundingClientRect().width + 'px');
    } else {
      hideResizeHandle();
    }
  });

  subsDismiss.addEventListener("click", function (e) {
    e.stopPropagation();
    subsPanel.classList.remove("visible");
    setSubsPanelVisible(false);
    hideResizeHandle();
  });

  var subsFocusExit = document.getElementById("subs-focus-exit");
  subsFocusExit.addEventListener("click", function (e) {
    e.stopPropagation();
    exitSubsFocus();
  });

  var subsToggleTerminal = document.getElementById("subs-toggle-terminal");
  subsToggleTerminal.addEventListener("click", function (e) {
    e.stopPropagation();
    showTerminalSubs = !showTerminalSubs;
    renderSubsessionsList();
  });

  subsPanel.addEventListener("click", function (e) {
    e.stopPropagation();
  });

  document.addEventListener("keydown", function (e) {
    // Escape exits focus mode first; panel-close second.
    if (e.key === "Escape" && focusedSubId !== null) {
      e.preventDefault();
      exitSubsFocus();
      return;
    }
    if (e.key === "Escape" && subsPanel.classList.contains("visible")) {
      subsPanel.classList.remove("visible");
      setSubsPanelVisible(false);
      hideResizeHandle();
    }
  });

  // Ctrl+Shift+F — focus/submit the currently-selected subsession.
  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey &&
        (e.key === "F" || e.key === "f")) {
      var sub = getSelectedSub();
      if (sub) { e.preventDefault(); toggleSubsFocus(sub); }
    }
  });

  // ---- Resize handle for subsessions panel -----------------------------
  var resizeDragging = false;
  var resizeStartX = 0;
  var resizeStartWidth = 0;

  function positionResizeHandle() {
    var rect = subsPanel.getBoundingClientRect();
    subsResizeHandle.style.display = "block";
    subsResizeHandle.style.left = rect.left + "px";
  }

  function hideResizeHandle() {
    subsResizeHandle.style.display = "none";
  }

  subsResizeHandle.addEventListener("mousedown", function (e) {
    e.preventDefault();
    resizeDragging = true;
    resizeStartX = e.clientX;
    resizeStartWidth = subsPanel.getBoundingClientRect().width;
    subsResizeHandle.classList.add("active");
    document.body.style.userSelect = "none";
  });

  document.addEventListener("mousemove", function (e) {
    if (!resizeDragging) return;
    var dx = resizeStartX - e.clientX;
    var newWidth = resizeStartWidth + dx;
    newWidth = Math.max(260, Math.min(newWidth, window.innerWidth * 0.9));
    subsPanel.style.width = newWidth + "px";
    positionResizeHandle();
    document.documentElement.style.setProperty('--subsessions-width', newWidth + 'px');
  });

  document.addEventListener("mouseup", function () {
    if (!resizeDragging) return;
    resizeDragging = false;
    subsResizeHandle.classList.remove("active");
    document.body.style.userSelect = "";
    positionResizeHandle();
  });

  window.addEventListener("resize", function () {
    if (!subsPanel.classList.contains("visible")) return;
    // Re-clamp the panel width so it doesn't overflow the viewport after
    // a browser window resize.  The inline width set during drag can
    // override the CSS max-width, so we re-apply the clamp here.
    var currentWidth = subsPanel.getBoundingClientRect().width;
    var maxWidth = window.innerWidth * 0.9;
    if (currentWidth > maxWidth) {
      subsPanel.style.width = maxWidth + "px";
    } else if (currentWidth < 260) {
      subsPanel.style.width = "260px";
    }
    positionResizeHandle();
    document.documentElement.style.setProperty('--subsessions-width', subsPanel.getBoundingClientRect().width + 'px');
  });

  // ---- Initial state ---------------------------------------------------
  setConnectionStatus(true);  // optimistic green; turns red on first error
  renderSubsessionsList();    // show the empty state until the snapshot lands

  // Populate the per-session model selector and wire its change handler.
  // Re-polled every 60s so the header badge tracks llmio's provider
  // failover state (and the slot-resolved model names) near-live.
  loadModelOptions();
  setInterval(loadModelOptions, 60000);
  (function () {
    var modelSel = document.getElementById("model-selector");
    if (modelSel) modelSel.addEventListener("change", onModelSelectorChange);
  })();

  // ---- Missed-notification badge + panel -------------------------------
  // Store-and-forward: notify_user notifications are persisted server-side
  // and replayed over /events on connect. The badge surfaces the count of
  // notifications the user has not yet acknowledged (read=false); opening
  // the panel marks them read so the badge clears and stays clear on
  // refresh. Native desktop notifications are unaffected — the /events
  // "notification" branch still fires the browser Notifications API.
  function renderNotificationBadge() {
    if (!notificationsBadge) return;
    var n = unreadNotifications.length;
    notificationsBadge.textContent = String(n);
    notificationsBadge.hidden = n === 0;
  }

  function formatNotifTimestamp(ts) {
    if (!ts) return "";
    var d = new Date(ts);
    if (isNaN(d.getTime())) return ts;
    return d.toLocaleString();
  }

  function renderNotificationsList() {
    if (!notificationsList) return;
    notificationsList.innerHTML = "";
    if (unreadNotifications.length === 0) {
      var empty = document.createElement("div");
      empty.className = "notif-empty";
      empty.textContent = "No missed notifications.";
      notificationsList.appendChild(empty);
      return;
    }
    for (var i = 0; i < unreadNotifications.length; i++) {
      var rec = unreadNotifications[i];
      var row = document.createElement("div");
      row.className = "notif-row";

      var title = document.createElement("div");
      title.className = "notif-title";
      title.textContent = rec.title || "Notification";
      row.appendChild(title);

      if (rec.body) {
        var body = document.createElement("div");
        body.className = "notif-body";
        body.textContent = rec.body;
        row.appendChild(body);
      }

      var meta = document.createElement("div");
      meta.className = "notif-meta";
      var when = formatNotifTimestamp(rec.ts);
      var src = rec.source_session ? ("session " + rec.source_session) : "";
      meta.textContent = [when, src].filter(Boolean).join(" · ");
      row.appendChild(meta);

      notificationsList.appendChild(row);
    }
  }

  function fetchUnreadNotifications() {
    // Best-effort: a failed fetch must never break the chat UI.
    return fetch(apiBase() + "/notifications/unread", { method: "GET" })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (data) {
        unreadNotifications = Array.isArray(data) ? data : [];
        renderNotificationBadge();
        renderNotificationsList();
      })
      .catch(function () { /* leave the current state untouched */ });
  }

  function markNotificationsRead() {
    // Empty body → the server marks every currently-unread record read.
    return fetch(apiBase() + "/notifications/read", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    })
      .then(function () {
        unreadNotifications = [];
        renderNotificationBadge();
        renderNotificationsList();
      })
      .catch(function () { /* best-effort */ });
  }

  function openNotificationsPanel() {
    if (!notificationsPanel) return;
    notificationsPanel.classList.add("visible");
    // Render what was missed FIRST, then mark those records read so the
    // panel still lists them while the badge clears.
    renderNotificationsList();
    markNotificationsRead();
  }

  function closeNotificationsPanel() {
    if (notificationsPanel) notificationsPanel.classList.remove("visible");
  }

  function toggleNotificationsPanel() {
    if (!notificationsPanel) return;
    if (notificationsPanel.classList.contains("visible")) {
      closeNotificationsPanel();
    } else {
      openNotificationsPanel();
    }
  }

  if (notificationsToggle) {
    notificationsToggle.addEventListener("click", toggleNotificationsPanel);
  }
  if (notificationsDismiss) {
    notificationsDismiss.addEventListener("click", closeNotificationsPanel);
  }

  // Request browser notification permission early so the agent's
  // notify_user tool can push native alerts.  Silently ignored when the
  // browser does not support the Notifications API or when permission
  // was previously denied.
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission();
  }

  // Bootstrap: fetch sessions, pick the active one, then load history/events.

  fetchSessions().then(function (data) {
    // Determine active session: server-reported active, or newest, or local fallback.
    var sid = data.active_session_id;
    if (!sid && data.sessions && data.sessions.length > 0) {
      sid = data.sessions[0].session_id;
    }
    var localSid = getActiveSessionId();
    if (localSid && data.sessions) {
      // If the locally stored session still exists on the server, prefer it.
      for (var i = 0; i < data.sessions.length; i++) {
        if (data.sessions[i].session_id === localSid) {
          sid = localSid;
          break;
        }
      }
    }
    if (sid) {
      setActiveSessionId(sid);
    }
    updateUnreadFromList(data.sessions || []);
    fetchUnreadNotifications();
    renderSessionList(data);
    // Open /events only after history renders (see switchSession/loadHistory)
    // so re-attaching to an in-flight turn on page load renders the live round
    // below the transcript instead of racing above it.
    loadHistory(openEventStream);
    fetchSubsessions();
    restoreSubsPanelState();
    restoreSessionsPanelState();
    resetIdleTimer();
  }).catch(function () {
    // If sessions endpoint is unavailable, fall back to local active session.
    var localSid = getActiveSessionId();
    if (localSid) {
      setActiveSessionId(localSid);
    } else {
      // Last resort (the sessions endpoint is unreachable): a throwaway
      // session id so the page still functions. Never the owner id — that
      // would mint a session whose id collides with the shared owner. Once
      // /sessions answers again this id is not in the server list, so the
      // bootstrap above discards it.
      setActiveSessionId(randomId());
    }
    loadHistory(openEventStream);
    fetchSubsessions();
    restoreSubsPanelState();
    restoreSessionsPanelState();
    resetIdleTimer();
  });

  // ---- Periodic session-list refresh -----------------------------------
  var SESSION_REFRESH_INTERVAL_MS = 20000;  // 20 seconds
  var sessionRefreshTimer = setInterval(function () {
    // Only refresh when the page is visible to avoid wasted fetches.
    if (document.hidden) return;
    refreshSessions();
  }, SESSION_REFRESH_INTERVAL_MS);

  window.addEventListener("beforeunload", function () {
    saveDraft();
    if (sessionRefreshTimer) {
      clearInterval(sessionRefreshTimer);
      sessionRefreshTimer = null;
    }
  });

  // Persist drafts when the tab loses focus.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      saveDraft();
    }
  });

  // ---- Settings panel --------------------------------------------------
  var settingsToggle = document.getElementById("settings-toggle");
  var settingsPanel = document.getElementById("settings-panel");
  var settingsDismiss = settingsPanel.querySelector(".dismiss");
  var settingsResizeHandle = document.getElementById("settings-resize-handle");

  // Panel visibility (localStorage-backed).
  var SETTINGS_PANEL_KEY = PROJECT_TITLE + "-settings-panel-visible";

  function getSettingsPanelVisible() {
    try { return localStorage.getItem(SETTINGS_PANEL_KEY) === "true"; }
    catch (_) { return false; }
  }

  function setSettingsPanelVisible(visible) {
    try { localStorage.setItem(SETTINGS_PANEL_KEY, visible ? "true" : "false"); } catch (_) {}
  }

  let _configPanelReady = false;
  async function _initConfigPanel() {
    if (_configPanelReady) return;
    const el = document.getElementById('config-panel-mount');
    if (!el) return;
    try {
      const { mountConfigPanel } = await import('/static/vendor/vanilla.js');
      mountConfigPanel(el, { title: 'Settings' });
      _configPanelReady = true;
    } catch (_err) {
      el.textContent =
        'Settings panel unavailable — vendor assets missing. ' +
        'Run scripts/vendor-ui.sh for local dev or rebuild the Docker image.';
    }
  }

  function openSettingsPanel() {
    settingsPanel.classList.add("visible");
    positionSettingsResizeHandle();
    document.documentElement.style.setProperty('--settings-width', settingsPanel.getBoundingClientRect().width + 'px');
    setSettingsPanelVisible(true);
    _initConfigPanel();
  }

  // Expose for the AppShell's Settings link (top-level module scope).
  window.__chatOpenSettingsPanel = openSettingsPanel;

  function closeSettingsPanel() {
    settingsPanel.classList.remove("visible");
    setSettingsPanelVisible(false);
    hideSettingsResizeHandle();
  }

  function restoreSettingsPanelState() {
    if (getSettingsPanelVisible()) { openSettingsPanel(); }
  }

  settingsToggle.addEventListener("click", function (e) {
    e.stopPropagation();
    if (settingsPanel.classList.contains("visible")) {
      closeSettingsPanel();
    } else {
      openSettingsPanel();
    }
  });

  settingsDismiss.addEventListener("click", function (e) {
    e.stopPropagation();
    closeSettingsPanel();
  });

  settingsPanel.addEventListener("click", function (e) {
    e.stopPropagation();
  });

  document.addEventListener("keydown", function (e) {
    // Focus mode owns Escape for the subsessions panel.
    if (e.key === "Escape" && focusedSubId === null &&
        settingsPanel.classList.contains("visible")) {
      closeSettingsPanel();
    }
  });

  // Settings panel resize.
  var settingsResizeDragging = false;
  var settingsResizeStartX = 0;
  var settingsResizeStartWidth = 0;

  function positionSettingsResizeHandle() {
    var rect = settingsPanel.getBoundingClientRect();
    settingsResizeHandle.style.display = "block";
    settingsResizeHandle.style.left = rect.left + "px";
  }

  function hideSettingsResizeHandle() {
    settingsResizeHandle.style.display = "none";
  }

  settingsResizeHandle.addEventListener("mousedown", function (e) {
    e.preventDefault();
    settingsResizeDragging = true;
    settingsResizeStartX = e.clientX;
    settingsResizeStartWidth = settingsPanel.getBoundingClientRect().width;
    settingsResizeHandle.classList.add("active");
    document.body.style.userSelect = "none";
  });

  document.addEventListener("mousemove", function (e) {
    if (!settingsResizeDragging) return;
    var dx = settingsResizeStartX - e.clientX;
    var newWidth = settingsResizeStartWidth + dx;
    newWidth = Math.max(260, Math.min(newWidth, window.innerWidth * 0.9));
    settingsPanel.style.width = newWidth + "px";
    positionSettingsResizeHandle();
    document.documentElement.style.setProperty('--settings-width', newWidth + 'px');
  });

  document.addEventListener("mouseup", function () {
    if (!settingsResizeDragging) return;
    settingsResizeDragging = false;
    settingsResizeHandle.classList.remove("active");
    document.body.style.userSelect = "";
    positionSettingsResizeHandle();
  });

  window.addEventListener("resize", function () {
    if (!settingsPanel.classList.contains("visible")) return;
    var currentWidth = settingsPanel.getBoundingClientRect().width;
    var maxWidth = window.innerWidth * 0.9;
    if (currentWidth > maxWidth) {
      settingsPanel.style.width = maxWidth + "px";
    } else if (currentWidth < 260) {
      settingsPanel.style.width = "260px";
    }
    positionSettingsResizeHandle();
    document.documentElement.style.setProperty('--settings-width', settingsPanel.getBoundingClientRect().width + 'px');
  });

  function formatFieldLabel(key) {
    return key.replace(/_/g, " ");
  }

  // ---- Presets editor (periodic.sessions) ------------------------------

  function renderPresetsEditor(path, presets) {
    var container = document.createElement("div");
    container.className = "presets-editor";

    var header = document.createElement("div");
    header.className = "presets-editor-header";
    header.textContent = formatFieldLabel("sessions");
    container.appendChild(header);

    var list = document.createElement("div");
    list.className = "presets-editor-list";
    container.appendChild(list);

    // Hidden textarea for serialization.
    var hidden = document.createElement("textarea");
    hidden.style.display = "none";
    hidden.setAttribute("data-path", path);
    hidden.setAttribute("data-type", "array");
    hidden.value = JSON.stringify(presets, null, 2);
    container.appendChild(hidden);

    rebuildPresetRows(list, presets, path);

    // Add button.
    var addBtn = document.createElement("button");
    addBtn.className = "preset-add-btn";
    addBtn.textContent = "+ Add Preset";
    addBtn.addEventListener("click", function () {
      addPreset(container, path);
    });
    container.appendChild(addBtn);

    return container;
  }

  function rebuildPresetRows(list, presets, path) {
    list.innerHTML = "";
    if (!presets || presets.length === 0) {
      var empty = document.createElement("div");
      empty.className = "preset-empty";
      empty.textContent = "No presets configured.";
      list.appendChild(empty);
      return;
    }
    for (var i = 0; i < presets.length; i++) {
      var row = renderPresetRow(presets[i], i, path);
      list.appendChild(row);
    }
  }

  function renderPresetRow(preset, index, path) {
    var row = document.createElement("div");
    row.className = "preset-row";
    row.setAttribute("data-preset-index", index);

    var summary = document.createElement("span");
    summary.className = "preset-summary";

    var nameEl = document.createElement("span");
    nameEl.className = "preset-name";
    nameEl.textContent = preset.name || "(unnamed)";
    summary.appendChild(nameEl);

    var detail = document.createElement("span");
    detail.className = "preset-detail";
    var interval = preset.schedule_interval_seconds != null
      ? preset.schedule_interval_seconds
      : DEFAULT_SCHEDULE_INTERVAL_SECONDS;
    var parts = ["every " + interval + "s"];
    if (preset.model_level != null) {
      parts.push("L" + preset.model_level);
    }
    if (preset.enabled === false) {
      parts.push("disabled");
    }
    detail.textContent = parts.join(" · ");
    summary.appendChild(detail);

    row.appendChild(summary);

    var buttons = document.createElement("span");
    buttons.className = "preset-row-buttons";

    var editBtn = document.createElement("button");
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", function () {
      showPresetForm(row, preset, index, path);
    });
    buttons.appendChild(editBtn);

    var delBtn = document.createElement("button");
    delBtn.className = "preset-delete-btn";
    delBtn.textContent = "Del";
    delBtn.addEventListener("click", function () {
      deletePreset(row, index, path);
    });
    buttons.appendChild(delBtn);

    row.appendChild(buttons);

    return row;
  }

  function showPresetForm(row, preset, index, path) {
    // Remove any existing form first.
    var existingForm = row.querySelector(".preset-form");
    if (existingForm) {
      existingForm.remove();
      return; // toggle off
    }

    // Close any other open forms in the same editor.
    var editor = row.closest(".presets-editor");
    if (editor) {
      var openForms = editor.querySelectorAll(".preset-form");
      for (var f = 0; f < openForms.length; f++) {
        openForms[f].remove();
      }
    }

    var form = document.createElement("div");
    form.className = "preset-form";

    // Name
    var nameRow = makeFormRow("Name");
    var nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.value = preset.name || "";
    nameRow.appendChild(nameInput);
    form.appendChild(nameRow);

    // Initial prompt — the one message the scheduler posts into the fresh
    // session. Write it as a complete task brief.
    var promptRow = makeFormRow("Initial prompt");
    var promptInput = document.createElement("textarea");
    promptInput.rows = 5;
    promptInput.value = preset.initial_prompt || "";
    promptRow.appendChild(promptInput);
    form.appendChild(promptRow);

    // Interval — the whole scheduling contract.
    var intervalRow = makeFormRow("Interval (s)");
    var intervalInput = document.createElement("input");
    intervalInput.type = "number";
    intervalInput.step = "any";
    intervalInput.min = "300";
    intervalInput.value = preset.schedule_interval_seconds != null
      ? preset.schedule_interval_seconds
      : DEFAULT_SCHEDULE_INTERVAL_SECONDS;
    intervalRow.appendChild(intervalInput);
    form.appendChild(intervalRow);

    // Model level (optional — blank means use global default)
    var modelRow = makeFormRow("Model Level");
    var modelInput = document.createElement("input");
    modelInput.type = "number";
    modelInput.min = "1";
    modelInput.max = "3";
    modelInput.placeholder = "global default";
    modelInput.value = preset.model_level != null ? preset.model_level : "";
    modelRow.appendChild(modelInput);
    form.appendChild(modelRow);

    // Enabled
    var enabledRow = makeFormRow("");
    var enabledLabel = document.createElement("label");
    enabledLabel.style.display = "inline-flex";
    enabledLabel.style.alignItems = "center";
    var enabledCheck = document.createElement("input");
    enabledCheck.type = "checkbox";
    enabledCheck.checked = preset.enabled !== false;
    enabledLabel.appendChild(enabledCheck);
    enabledLabel.appendChild(document.createTextNode(" Enabled"));
    enabledRow.appendChild(enabledLabel);
    form.appendChild(enabledRow);

    // Actions
    var actions = document.createElement("div");
    actions.className = "preset-form-actions";

    var saveBtn = document.createElement("button");
    saveBtn.className = "preset-save-btn";
    saveBtn.textContent = index < 0 ? "Add" : "Save";
    saveBtn.addEventListener("click", function () {
      var ml = modelInput.value.trim();
      var newPreset = {
        name: nameInput.value.trim(),
        initial_prompt: promptInput.value,
        schedule_interval_seconds:
          Number(intervalInput.value) || DEFAULT_SCHEDULE_INTERVAL_SECONDS,
        model_level: ml !== "" ? Number(ml) : null,
        enabled: enabledCheck.checked
      };
      savePresetForm(row, path, index, newPreset);
    });
    actions.appendChild(saveBtn);

    var cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", function () {
      form.remove();
      // If this was a new (unsaved) preset, remove the row too.
      if (index < 0) {
        row.remove();
      }
    });
    actions.appendChild(cancelBtn);

    form.appendChild(actions);
    row.appendChild(form);
  }

  function makeFormRow(labelText) {
    var r = document.createElement("div");
    r.className = "preset-form-row";
    if (labelText) {
      var lbl = document.createElement("label");
      lbl.textContent = labelText;
      r.appendChild(lbl);
    }
    return r;
  }

  function savePresetForm(row, path, index, newPreset) {
    // Find the presets editor container.
    var container = row.closest(".presets-editor");
    if (!container) return;

    // Read current presets from the hidden textarea.
    var hidden = container.querySelector("textarea[data-path=\"" + path + "\"]");
    if (!hidden) return;

    var presets;
    try { presets = JSON.parse(hidden.value); }
    catch (_) { presets = []; }
    if (!Array.isArray(presets)) presets = [];

    if (index >= 0) {
      presets[index] = newPreset;
    } else {
      presets.push(newPreset);
    }

    // Update hidden textarea.
    hidden.value = JSON.stringify(presets, null, 2);

    // Rebuild the visual rows.
    var list = container.querySelector(".presets-editor-list");
    if (list) rebuildPresetRows(list, presets, path);
  }

  function deletePreset(row, index, path) {
    var container = row.closest(".presets-editor");
    if (!container) return;

    var hidden = container.querySelector("textarea[data-path=\"" + path + "\"]");
    if (!hidden) return;

    var presets;
    try { presets = JSON.parse(hidden.value); }
    catch (_) { presets = []; }
    if (!Array.isArray(presets)) presets = [];

    presets.splice(index, 1);

    hidden.value = JSON.stringify(presets, null, 2);

    var list = container.querySelector(".presets-editor-list");
    if (list) rebuildPresetRows(list, presets, path);
  }

  function addPreset(container, path) {
    // Find the list.
    var list = container.querySelector(".presets-editor-list");
    if (!list) return;

    // Create a temporary row for the new preset.
    var row = document.createElement("div");
    row.className = "preset-row";
    row.setAttribute("data-preset-index", -1);

    var summary = document.createElement("span");
    summary.className = "preset-summary";
    summary.textContent = "New preset";
    row.appendChild(summary);

    list.appendChild(row);

    var emptyPreset = {
      name: "",
      initial_prompt: "",
      schedule_interval_seconds: DEFAULT_SCHEDULE_INTERVAL_SECONDS,
      enabled: true
    };
    showPresetForm(row, emptyPreset, -1, path);
  }

  restoreSettingsPanelState();
})();
