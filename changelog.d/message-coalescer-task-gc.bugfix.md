Fix an in-flight chat message occasionally failing to persist a reply: `MessageCoalescer`'s
background processor task was created via `asyncio.create_task()` without retaining a strong
reference — the one place in the codebase that didn't follow the established pattern of storing the
task in a long-lived set with a done-callback. An unreferenced task can be silently
garbage-collected before it completes, aborting the agent run before the reply is ever recorded.
