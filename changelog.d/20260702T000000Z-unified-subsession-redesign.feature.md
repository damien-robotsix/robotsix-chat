Redesign the chat system around a unified **subsession** model: the main agent (now on llmio Level
4, `claude-fable-5`) spawns background sub-agents of three kinds — one-shot `task`, recurring
`periodic`, and user-facing `user_chat` side-chats — each at a model level (1–4) picked by task
difficulty, with depth-limited nesting, mid-run steering messages, external close, and a summary
delivered to the parent conversation on every close path. Replaces `delegate_task` background tasks,
check loops, and the pending-questions thread system (endpoints, SSE events, tools, config, and UI
panels removed); the browser UI gains a single Subsessions panel with live status, expandable
transcripts, per-subsession chat for `user_chat`, and clearer labeled controls.
