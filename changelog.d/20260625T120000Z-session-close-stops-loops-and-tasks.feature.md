Added the ability to **close a session**, which stops its background work. A new
`DELETE /sessions/{session_id}` endpoint stops every check loop and cancels every in-flight
background sub-agent task owned by the session (via `CheckLoopRegistry.stop_all_for_session` /
`TaskRegistry.cancel_all_for_session`), deletes the session and its history (reassigning the owner's
active session, or creating a fresh empty one when none remain), and returns the loop/task stop
counts. The sessions panel gains a per-session delete (×) button. This completes the per-session
lifecycle: a recurring check now survives restarts and runs until it is explicitly stopped **or its
session is closed**.
