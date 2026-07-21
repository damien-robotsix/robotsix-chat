(function () {
  "use strict";

  // ---- DOM refs --------------------------------------------------------
  const chatEl       = document.getElementById("chat");
  const summaryContainerEl = document.getElementById("summary-container");
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
  // (currentRequestSessionId removed — unused; cross-session guard uses
  //  the requestSessionId captured inside doPost instead.)

  // ---- Summary overlay: keep chat padding in sync with summary height ---
  // Uses a ResizeObserver so the absolutely-positioned summary never
  // participates in layout — the chat's scroll offset is stable regardless
  // of summary visibility or size changes.
  if (summaryContainerEl && chatEl) {
    var summaryResizeObserver = new ResizeObserver(function (entries) {
      var h = 0;
      for (var i = 0; i < entries.length; i++) {
        h = entries[i].contentRect.height;
      }
      chatEl.style.setProperty("--summary-height", h + "px");
    });
    summaryResizeObserver.observe(summaryContainerEl);
  }

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
      pendingImages.push({ file: f, objectURL: objectURL, mediaType: f.type });
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

  // ---- Conversation client id (localStorage-backed) --------------------
  // A stable per-browser id sent with every message so the server can thread
  // consecutive messages into one conversation (and reset to a new one after
  // it's been idle). Persisted so a page reload continues the conversation.
  var PROJECT_TITLE = document.querySelector('meta[name="project-title"]').content;
  var CLIENT_ID_KEY = PROJECT_TITLE + "-client-id";

  function randomId() {
    try {
      if (window.crypto && window.crypto.randomUUID) {
        return window.crypto.randomUUID();
      }
    } catch (_) {}
    return "c-" + Date.now().toString(36) + "-" +
      Math.random().toString(36).slice(2, 10);
  }

  function getClientId() {
    try {
      var id = localStorage.getItem(CLIENT_ID_KEY);
      if (!id) { id = randomId(); localStorage.setItem(CLIENT_ID_KEY, id); }
      return id;
    } catch (_) {
      // Private mode / storage disabled — fall back to a per-session id so the
      // request still works (continuity just won't survive a reload).
      return randomId();
    }
  }

  var clientId = getClientId();

  // ---- Session management (localStorage-backed) -----------------------
  var ACTIVE_SESSION_KEY = PROJECT_TITLE + "-active-session-id";
  var SUBS_PANEL_KEY = PROJECT_TITLE + "-subsessions-panel-visible";
  var SESSIONS_PANEL_KEY = PROJECT_TITLE + "-sessions-panel-visible";
  var UNREAD_SESSION_KEY = PROJECT_TITLE + "-unread-sessions";
  var activeSessionId = null;
  var sessionsList = [];        // cached session list from server

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
    var url = apiBase() + "/sessions?owner_id=" + encodeURIComponent(clientId);
    return fetch(url, { method: "GET" }).then(function (r) {
      if (!r.ok) throw new Error("Failed to fetch sessions");
      return r.json();
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
      titleDiv.textContent = s.title || "Untitled";
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
    var url = apiBase() + "/sessions/" + encodeURIComponent(sid) +
              "?owner_id=" + encodeURIComponent(clientId);
    return fetch(url, { method: "DELETE" }).then(function (r) {
      if (!r.ok && r.status !== 404) throw new Error("delete failed");
      return r.json().catch(function () { return {}; });
    }).then(function (data) {
      // If we closed the active session, switch to the server-chosen
      // replacement (it always returns one) so the chat view stays valid.
      if (sid === activeSessionId && data && data.active_session_id) {
        switchSession(data.active_session_id);
      }
      refreshSessions();
    }).catch(function () {
      // Best-effort: refresh anyway so the list reflects server state.
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

    // 1. Persist the new active session_id.
    setActiveSessionId(sessionId);

    // 1b. Clear unread highlight for this session.
    markSessionRead(sessionId);

    // 2. Clear the chat DOM bubbles.
    clearChatBubbles();

    // 2b. Reset the per-session Subsessions panel.
    clearSubsessions();

    // 3. Close the current event stream and re-open for new session.
    closeEventStream();
    openEventStream();

    // 4. Load history for the new session.
    loadHistory();

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
    if (kind === "user_chat") return "💬 chat";
    return "⚙ task";
  }

  function newSubsEntry() {
    return {
      expanded: false,
      transcript: [],
      transcriptLoaded: false,
      _transcriptLoading: false,
      _closing: false,
      _draft: ""
    };
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
    renderSubsessionsList();
  }

  function handleSubsessionMessage(frame) {
    var sub = subsById[frame.subsession_id];
    if (!sub) return;  // unknown row — the next snapshot fetch picks it up
    var msg = {
      role: frame.role || "assistant",
      text: frame.text || "",
      timestamp: frame.timestamp || 0
    };
    if (!subsTranscriptHas(sub, msg)) sub.transcript.push(msg);
    if (frame.timestamp) sub.last_activity_at = frame.timestamp;
    // Update the transcript in place — a full list re-render here would
    // steal focus from the reply box while the user is typing.
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
    row.className = "subs-row status-" + status + (terminal ? " terminal" : "");
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
      renderSubsessionsList();
    });
    actionsDiv.appendChild(expandBtn);

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
          renderSuggestionChips(parsed.suggestions, (function (s) {
            return function (text) { sendSubsessionMessage(s, text); };
          })(sub), msgDiv);
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
        subsById[sid] = sub;
        subsOrder.push(sid);
      }
      renderSubsessionsList();
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
  // Parses a ```suggestions fenced block from the assistant message text.
  // Returns { cleanText: string, suggestions: string[] | null }.
  // cleanText has the fenced block removed; suggestions is null when no
  // block is present.
  var SUGGESTIONS_RE = /```suggestions\s*\n([\s\S]*?)```/;

  function parseSuggestions(raw) {
    var match = SUGGESTIONS_RE.exec(raw);
    if (!match) return { cleanText: raw, suggestions: null };

    var blockContent = match[1];
    var lines = blockContent.split("\n");
    var suggestions = [];
    for (var i = 0; i < lines.length; i++) {
      var trimmed = lines[i].trim();
      if (trimmed) suggestions.push(trimmed);
    }

    var cleanText = raw.slice(0, match.index) + raw.slice(match.index + match[0].length);
    // Collapse trailing blank lines that may be left behind.
    cleanText = cleanText.replace(/\n{3,}$/, "\n\n").trimEnd();

    return {
      cleanText: cleanText,
      suggestions: suggestions.length > 0 ? suggestions : null
    };
  }

  function renderSuggestionChips(suggestions, onSubmit, afterElement) {
    var container = document.createElement("div");
    container.className = "suggestion-chips";
    for (var i = 0; i < suggestions.length; i++) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "suggestion-chip";
      chip.textContent = suggestions[i];
      chip.title = "Click to reply: " + suggestions[i];
      chip.addEventListener("click", (function (text) {
        return function () { onSubmit(text); };
      })(suggestions[i]));
      container.appendChild(chip);
    }
    // Insert after the bubble element so chips sit below the message.
    if (afterElement && afterElement.parentNode) {
      afterElement.parentNode.insertBefore(container, afterElement.nextSibling);
    }
    return container;
  }

  // Submit a suggestion as a user reply in the main chat.
  function submitMainChatSuggestion(text) {
    msgInput.value = text;
    submitMessage();
  }

  // ---- Message bubbles -------------------------------------------------
  function clearChatBubbles() {
    // Remove all bubble elements, typing indicator, summary banner,
    // and inline notices from the chat container so no messages bleed
    // across sessions.
    var children = chatEl.querySelectorAll(".bubble, #typing-indicator, .suggestion-chips");
    for (var i = 0; i < children.length; i++) {
      children[i].remove();
    }
    clearSummary();
    currentAssistantBubble = null;
    rawAssistantText = "";
    typingIndicatorEl = null;
    if (lastModelTimestampEl) { lastModelTimestampEl.remove(); lastModelTimestampEl = null; }
    // Also clear queued messages — they belong to the old session.
    messageQueue = [];
    // Reset state so the composer is not blocked.
    if (state === "sending" || state === "streaming") {
      state = "idle";
    }
    updateSendBusy();
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
    bubble.textContent = rawAssistantText;
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

  // ---- Conversation summary --------------------------------------------
  var summaryBannerEl = null;
  var summaryFetchController = null;  // AbortController for in-flight fetch

  function clearSummary() {
    if (summaryBannerEl) {
      summaryBannerEl.remove();
      summaryBannerEl = null;
    }
    if (summaryFetchController) {
      summaryFetchController.abort();
      summaryFetchController = null;
    }
  }

  function refreshSummary() {
    if (!activeSessionId) return;
    // Abort any in-flight summary fetch for this session.
    if (summaryFetchController) {
      summaryFetchController.abort();
      summaryFetchController = null;
    }
    // Don't fetch if there are no bubbles (empty session).
    var bubbles = chatEl.querySelectorAll(".bubble.user, .bubble.assistant");
    if (bubbles.length === 0) {
      clearSummary();
      return;
    }

    // Show loading state on existing banner or create a new one.
    if (!summaryBannerEl) {
      summaryBannerEl = document.createElement("div");
      summaryBannerEl.className = "summary-banner";
      summaryContainerEl.appendChild(summaryBannerEl);
    }
    var body = summaryBannerEl.querySelector(".summary-body");
    if (!body) {
      var header = document.createElement("div");
      header.className = "summary-header";
      header.textContent = "▾ Summary";
      header.addEventListener("click", function () {
        summaryBannerEl.classList.toggle("collapsed");
      });
      summaryBannerEl.appendChild(header);
      body = document.createElement("div");
      body.className = "summary-body";
      summaryBannerEl.appendChild(body);
    }
    body.innerHTML = "<span class=\"summary-loading\">Updating…</span>";

    var ctrl = new AbortController();
    summaryFetchController = ctrl;

    var url = apiBase() + "/summary";
    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: activeSessionId, owner_id: clientId }),
      signal: ctrl.signal
    }).then(function (response) {
      summaryFetchController = null;
      if (!response.ok) return;
      return response.json();
    }).then(function (data) {
      if (!data || !summaryBannerEl) return;
      renderSummary(data);
    }).catch(function (err) {
      summaryFetchController = null;
      if (err && err.name === "AbortError") return;
      // Silently ignore — summary is best-effort.
    });
  }

  function renderSummary(data) {
    if (!summaryBannerEl) return;
    var body = summaryBannerEl.querySelector(".summary-body");
    if (!body) return;

    var value = data.summary;
    if (value && typeof value === "string" && value.trim()) {
      body.innerHTML = "<div class=\"summary-text\">" +
              renderMarkdown(value.trim()) + "</div>";
    } else {
      body.innerHTML = "<span class=\"summary-loading\">No summary available yet.</span>";
    }
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // ---- SSE stream parser -----------------------------------------------
  function processSSEStream(body, controller) {
    // We use a ReadableStream to pipe fetch body chunks into an SSE line
    // parser.  Each \n\n-terminated block is decoded and dispatched.
    var reader = body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";

    function pump() {
      reader.read().then(function (result) {
        if (result.done) {
          // Stream ended.  If we were still streaming (no "done" frame
          // from /chat), treat as error.  Otherwise, if the controller
          // provides an onDone callback (e.g. the /events channel), call
          // it so the consumer can reconnect.
          if (controller.onDone) {
            controller.onDone();
          } else if (state === "streaming") {
            controller.error(new Error("Server closed the connection unexpectedly"));
          }
          return;
        }

        buffer += decoder.decode(result.value, { stream: true });
        // Normalise \r\n → \n and strip stray \r for robustness.
        buffer = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
        var lines = buffer.split("\n");
        // Keep the last (possibly incomplete) segment in the buffer.
        buffer = lines.pop();

        var currentData = "";
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i];
          if (line.startsWith("data: ")) {
            // Accumulate multi-line data fields (some SSE impls split
            // JSON across several data: lines, though ours doesn't).
            currentData += line.slice(6);
          } else if (line === "data:") {
            currentData += "";
          } else if (line === "") {
            // Empty line = end of event.  Process accumulated data.
            if (currentData !== "") {
              controller.onData(currentData);
              currentData = "";
            }
          }
          // Ignore lines with "event:", "id:", "retry:", or comments.
        }

        if (controller.onActivity) controller.onActivity();
        return pump();
      }).catch(function (err) {
        controller.error(err);
      });
    }

    return { start: pump };
  }

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
                    "&owner_id=" + encodeURIComponent(clientId);

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
  function loadHistory() {
    var historyUrl = apiBase() + "/history" +
                     "?session_id=" + encodeURIComponent(activeSessionId) +
                     "&owner_id=" + encodeURIComponent(clientId);

    fetch(historyUrl, { method: "GET" }).then(function (response) {
      if (!response.ok) return;
      return response.json();
    }).then(function (data) {
      if (!data || !Array.isArray(data.turns)) return;
      var turns = data.turns;
      for (var i = 0; i < turns.length; i++) {
        var turn = turns[i];
        if (Array.isArray(turn) && turn.length >= 2) {
          addUserBubble(turn[0]);
          addAssistantBubble(turn[1]);
        }
      }
      scheduleForceScrollToBottom();
      // Refresh the conversation summary once history is loaded.
      refreshSummary();
    }).catch(function () {
      // Silently ignore network errors — empty chat is fine.
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
    if (messageQueue.length === 0) { updateCancelQueuedButton(); return; }

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
          hideTypingIndicator();
          finaliseAssistantBubble();
          setConnectionStatus(true);
          state = "idle";
          updateSendBusy();
          // Show the timestamp of the last model message.
          updateLastModelTimestamp(frame.timestamp);
          // The server may have rerouted this turn into a continuation
          // session (idle-timeout compaction) — adopt it before anything
          // below reads activeSessionId, so the summary refresh and any
          // queued messages target the session the turn actually landed in.
          if (frame.session_id && frame.session_id !== requestSessionId) {
            adoptSession(frame.session_id);
          }
          // Refresh the conversation summary after each turn.
          refreshSummary();
          // Automatically dispatch the next queued message (FIFO).
          drainQueue();
        } else if (frame.type === "error") {
          hideTypingIndicator();
          finaliseAssistantBubble();
          showError(frame.message || "Server error");
          state = "error";
          updateSendBusy();
          // Leave queued messages in place — the user can trigger
          // drainQueue() by submitting another message later.
        }
      },
      error: function (err) {
        hideTypingIndicator();
        finaliseAssistantBubble();
        showError(err.message || "Network error — is the server running?");
        state = "error";
        updateSendBusy();
        // Same as above: queued messages stay; next submit resumes draining.
      }
    };

    var url = serverUrl();

    var body = { message: message, session_id: activeSessionId, owner_id: clientId };
    if (messageId) body.message_id = messageId;
    if (encodedImages.length > 0) {
      body.images = encodedImages;
    }

    fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (!response.ok) {
        // Non-2xx — try to read an error body, else show status.
        return response.text().then(function (txt) {
          var msg;
          try {
            var errBody = JSON.parse(txt);
            msg = errBody.error || errBody.message || ("HTTP " + response.status);
          } catch (_) {
            msg = txt || ("HTTP " + response.status);
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
    if (e.key === "Escape" && sessionsPanel.classList.contains("visible")) {
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
        openEventStream();
        loadHistory();
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
    if (e.key === "Escape" && subsPanel.classList.contains("visible")) {
      subsPanel.classList.remove("visible");
      setSubsPanelVisible(false);
      hideResizeHandle();
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
    renderSessionList(data);
    loadHistory();
    fetchSubsessions();
    restoreSubsPanelState();
    restoreSessionsPanelState();
    openEventStream();
    resetIdleTimer();
  }).catch(function () {
    // If sessions endpoint is unavailable, fall back to local active session.
    var localSid = getActiveSessionId();
    if (localSid) {
      setActiveSessionId(localSid);
    } else {
      // Last resort: use clientId as a fallback session_id for backwards compat.
      setActiveSessionId(clientId);
    }
    loadHistory();
    fetchSubsessions();
    restoreSubsPanelState();
    restoreSessionsPanelState();
    openEventStream();
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
    if (sessionRefreshTimer) {
      clearInterval(sessionRefreshTimer);
      sessionRefreshTimer = null;
    }
  });

  // ---- Settings panel --------------------------------------------------
  var settingsToggle = document.getElementById("settings-toggle");
  var settingsPanel = document.getElementById("settings-panel");
  var settingsDismiss = settingsPanel.querySelector(".dismiss");
  var settingsResizeHandle = document.getElementById("settings-resize-handle");
  var settingsForm = document.getElementById("settings-form");
  var settingsError = document.getElementById("settings-error");
  var settingsSaveBtn = document.getElementById("settings-save-btn");
  var settingsSaveStatus = document.getElementById("settings-save-status");
  var settingsConfigData = null;  // cached config from server
  var settingsConfigPath = "config/config.json";  // default

  // Panel visibility (localStorage-backed).
  var SETTINGS_PANEL_KEY = PROJECT_TITLE + "-settings-panel-visible";

  function getSettingsPanelVisible() {
    try { return localStorage.getItem(SETTINGS_PANEL_KEY) === "true"; }
    catch (_) { return false; }
  }

  function setSettingsPanelVisible(visible) {
    try { localStorage.setItem(SETTINGS_PANEL_KEY, visible ? "true" : "false"); } catch (_) {}
  }

  function openSettingsPanel() {
    settingsPanel.classList.add("visible");
    positionSettingsResizeHandle();
    document.documentElement.style.setProperty('--settings-width', settingsPanel.getBoundingClientRect().width + 'px');
    setSettingsPanelVisible(true);
    loadSettings();
  }

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
    if (e.key === "Escape" && settingsPanel.classList.contains("visible")) {
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

  // ---- Settings load / render ------------------------------------------

  // Known secret keys (suffix match) — displayed as password fields.
  var SECRET_KEY_SUFFIXES = [
    "_api_key", "_api_token", "_secret_key", "_private_key", "_github_token",
    "api_key", "api_token", "secret_key", "private_key", "github_token",
    "public_key", "deploy_api_key"
  ];

  function isSecretKey(key) {
    for (var i = 0; i < SECRET_KEY_SUFFIXES.length; i++) {
      if (key === SECRET_KEY_SUFFIXES[i] || key.indexOf(SECRET_KEY_SUFFIXES[i]) === key.length - SECRET_KEY_SUFFIXES[i].length) {
        return true;
      }
    }
    return false;
  }

  // Top-level section ordering — groups known sections first.
  var SECTION_ORDER = [
    "llmio_model_level", "llmio_api_key", "summary_model_level",
    "server_host", "server_port", "idle_timeout_minutes",
    "compaction_min_turns", "log_level", "log_json_format",
    "cors_allow_origins", "correlation_id_header",
    "max_images_per_message", "max_image_bytes", "allowed_image_media_types",
    "memory", "langfuse", "central_deploy", "mail",
    "conversation", "diagnostics", "refdocs", "render_url",
    "knowledge", "self_review", "version_check", "component_client",
    "subsessions", "direct_repo", "github_security", "repo_study",
    "lifecycle", "notification", "http_probe", "feedback"
  ];

  function fieldKeySort(a, b) {
    var ai = SECTION_ORDER.indexOf(a);
    var bi = SECTION_ORDER.indexOf(b);
    if (ai === -1 && bi === -1) return a < b ? -1 : a > b ? 1 : 0;
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  }

  function loadSettings() {
    settingsError.style.display = "none";
    settingsSaveStatus.textContent = "";
    settingsSaveStatus.className = "";
    settingsForm.innerHTML = "<div class=\"settings-empty\">Loading\u2026</div>";

    fetch(apiBase() + "/config", { method: "GET" })
      .then(function (r) {
        if (!r.ok) throw new Error("Failed to load config (HTTP " + r.status + ")");
        return r.json();
      })
      .then(function (data) {
        settingsConfigData = data;
        renderSettingsForm(data);
      })
      .catch(function (err) {
        settingsForm.innerHTML = "<div class=\"settings-empty\">Failed to load: " + escapeHtml(err.message) + "</div>";
      });
  }

  function renderSettingsForm(data) {
    settingsForm.innerHTML = "";
    if (!data || Object.keys(data).length === 0) {
      settingsForm.innerHTML = "<div class=\"settings-empty\">No config loaded — the config file may be empty or missing.</div>";
      return;
    }

    var keys = Object.keys(data).sort(fieldKeySort);
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      var value = data[key];
      var section = renderFieldSection(key, value, key, 0);
      if (section) settingsForm.appendChild(section);
    }
  }

  function renderFieldSection(key, value, path, depth) {
    if (value !== null && typeof value === "object" && !Array.isArray(value)) {
      // Nested object — collapsible section.
      return renderObjectSection(key, value, path, depth);
    }
    // Scalar or array — inline field.
    var wrap = document.createElement("div");
    wrap.className = "settings-field";
    wrap.setAttribute("data-path", path);

    var label = document.createElement("label");
    label.textContent = formatFieldLabel(key);
    wrap.appendChild(label);

    if (Array.isArray(value)) {
      var arrInput = document.createElement("textarea");
      arrInput.rows = Math.min(value.length + 1, 6);
      arrInput.value = JSON.stringify(value, null, 2);
      arrInput.setAttribute("data-path", path);
      arrInput.setAttribute("data-type", "array");
      wrap.appendChild(arrInput);
      var arrHint = document.createElement("span");
      arrHint.className = "field-hint";
      arrHint.textContent = "JSON array";
      wrap.appendChild(arrHint);
    } else if (typeof value === "boolean") {
      var cbLabel = document.createElement("label");
      cbLabel.style.display = "inline-flex"; cbLabel.style.alignItems = "center";
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = value;
      cb.setAttribute("data-path", path);
      cb.setAttribute("data-type", "boolean");
      cbLabel.appendChild(cb);
      cbLabel.appendChild(document.createTextNode(" " + (value ? "on" : "off")));
      cb.addEventListener("change", function () {
        cbLabel.childNodes[1].textContent = " " + (cb.checked ? "on" : "off");
      });
      wrap.appendChild(cbLabel);
    } else if (typeof value === "number") {
      var numInput = document.createElement("input");
      numInput.type = "number";
      numInput.value = value;
      numInput.setAttribute("data-path", path);
      numInput.setAttribute("data-type", "number");
      if (Number.isInteger(value)) numInput.step = "1";
      else numInput.step = "any";
      wrap.appendChild(numInput);
    } else {
      var inputType = isSecretKey(key) ? "password" : "text";
      var input = document.createElement("input");
      input.type = inputType;
      input.value = typeof value === "string" ? value : JSON.stringify(value);
      input.setAttribute("data-path", path);
      input.setAttribute("data-type", inputType === "password" ? "secret" : "string");
      wrap.appendChild(input);
      if (inputType === "password" && value === "***") {
        var hint = document.createElement("span");
        hint.className = "field-hint";
        hint.textContent = "Leave unchanged to keep current secret";
        wrap.appendChild(hint);
      }
    }
    return wrap;
  }

  function renderObjectSection(key, obj, path, depth) {
    var section = document.createElement("div");
    section.className = "settings-section";

    var header = document.createElement("div");
    header.className = "settings-section-header";

    var icon = document.createElement("span");
    icon.className = "toggle-icon";
    icon.textContent = "\u25bc";  // ▼
    header.appendChild(icon);

    var title = document.createElement("span");
    title.textContent = formatFieldLabel(key);
    header.appendChild(title);

    var body = document.createElement("div");
    body.className = "settings-section-body";

    section.appendChild(header);
    section.appendChild(body);

    var collapsed = depth > 0;  // auto-collapse nested sections
    if (collapsed) { body.style.display = "none"; icon.textContent = "\u25b6"; }

    header.addEventListener("click", function () {
      if (body.style.display === "none") {
        body.style.display = "";
        icon.textContent = "\u25bc";
      } else {
        body.style.display = "none";
        icon.textContent = "\u25b6";
      }
    });

    var childKeys = Object.keys(obj).sort(fieldKeySort);
    for (var i = 0; i < childKeys.length; i++) {
      var childKey = childKeys[i];
      var childValue = obj[childKey];
      var childPath = path + "." + childKey;
      var childEl = renderFieldSection(childKey, childValue, childPath, depth + 1);
      if (childEl) body.appendChild(childEl);
    }

    return section;
  }

  function formatFieldLabel(key) {
    return key.replace(/_/g, " ");
  }

  // ---- Settings save ---------------------------------------------------

  function serializeSettingsForm() {
    var result = {};
    var inputs = settingsForm.querySelectorAll("[data-path]");
    for (var i = 0; i < inputs.length; i++) {
      var el = inputs[i];
      var path = el.getAttribute("data-path");
      var type = el.getAttribute("data-type");
      var value;

      if (el.tagName === "TEXTAREA") {
        try { value = JSON.parse(el.value); }
        catch (_) { value = el.value; }
      } else if (el.type === "checkbox") {
        value = el.checked;
      } else if (el.type === "number") {
        if (el.value === "" || el.value === null) {
          value = null;
        } else {
          var num = Number(el.value);
          value = isNaN(num) ? el.value : num;
        }
      } else {
        value = el.value;
      }

      setNestedValue(result, path, value);
    }
    return result;
  }

  function setNestedValue(obj, path, value) {
    var parts = path.split(".");
    var current = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      if (!current[parts[i]] || typeof current[parts[i]] !== "object") {
        current[parts[i]] = {};
      }
      current = current[parts[i]];
    }
    current[parts[parts.length - 1]] = value;
  }

  settingsSaveBtn.addEventListener("click", function () {
    if (!settingsConfigData) return;

    // Clear previous errors.
    clearSettingsErrors();

    var body = serializeSettingsForm();
    settingsSaveBtn.disabled = true;
    settingsSaveStatus.textContent = "Saving\u2026";
    settingsSaveStatus.className = "";

    fetch(apiBase() + "/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (data) {
        return { status: r.status, data: data };
      });
    }).then(function (result) {
      settingsSaveBtn.disabled = false;
      if (result.status === 200) {
        settingsSaveStatus.textContent = "Saved.";
        settingsSaveStatus.className = "success";
        // Reload to pick up any server-side normalizations.
        loadSettings();
      } else if (result.status === 422) {
        settingsSaveStatus.textContent = "Validation failed.";
        settingsSaveStatus.className = "error";
        showSettingsError(result.data.detail || result.data.error || "Validation failed");
      } else {
        settingsSaveStatus.textContent = "Error: " + (result.data.error || "HTTP " + result.status);
        settingsSaveStatus.className = "error";
        showSettingsError(result.data.error || result.data.detail || "Unknown error");
      }
    }).catch(function (err) {
      settingsSaveBtn.disabled = false;
      settingsSaveStatus.textContent = "Error: " + err.message;
      settingsSaveStatus.className = "error";
      showSettingsError(err.message);
    });
  });

  function showSettingsError(msg) {
    settingsError.style.display = "block";
    settingsError.textContent = msg;
    // Try to highlight specific fields mentioned in the error.
    highlightErrorFields(msg);
  }

  function clearSettingsErrors() {
    settingsError.style.display = "none";
    settingsError.textContent = "";
    var errFields = settingsForm.querySelectorAll(".field-error");
    for (var i = 0; i < errFields.length; i++) {
      errFields[i].classList.remove("field-error");
      var msgEl = errFields[i].querySelector(".field-error-msg");
      if (msgEl) msgEl.remove();
    }
  }

  function highlightErrorFields(errorMsg) {
    // Pydantic validation errors mention field paths — try to match them.
    // e.g. "memory.embedding.endpoint" or "memory.llm.api_key"
    var fieldPattern = /([a-z_]+(?:\.[a-z_]+)*)/gi;
    var seen = {};
    var match;
    while ((match = fieldPattern.exec(errorMsg)) !== null) {
      var fieldPath = match[1].toLowerCase();
      if (seen[fieldPath]) continue;
      seen[fieldPath] = true;

      var fieldEl = settingsForm.querySelector("[data-path=\"" + fieldPath + "\"]");
      if (fieldEl) {
        var wrap = fieldEl.closest(".settings-field");
        if (wrap) {
          wrap.classList.add("field-error");
          var msgEl = document.createElement("span");
          msgEl.className = "field-error-msg";
          msgEl.textContent = "see error above";
          wrap.appendChild(msgEl);
        }
      }
    }
  }

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  // Restore settings panel state on load.
  restoreSettingsPanelState();
})();
