Spawning a subsession (task, user_chat, or periodic) always crashed the new worker with
`asyncio.run() cannot be called from a running event loop`. `create_agent_from_settings` calls
`fetch_roster_sync`, which uses `asyncio.run()` internally — safe only when called before the
server's event loop starts. `_subsession_worker` runs as a task on that already-running loop, so
it now builds the agent in a worker thread instead of calling the factory directly.
