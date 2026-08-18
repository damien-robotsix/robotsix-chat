Correct the autonomous-sessions restart guidance in the autonomous protocol prompt.
The OPERATOR CONFIGURATION GUIDANCE block no longer falsely claims changes take
effect without a restart — `autonomous.sessions` edits require a chat-service
restart because `AutonomousRunner._definitions` is resolved once at startup.
Adds a CONFIG-APPLY-AND-VERIFY protocol mandating the agent follow through:
arm a continuation, restart, and verify via `GET /autonomous/definitions` when
`auto_self_restart` is ON; or surface the pending-restart state for operator
authorization when OFF.  The AUTO SELF-RESTART block now carves out
`autonomous.sessions` changes as a valid auto-self-restart reason.
