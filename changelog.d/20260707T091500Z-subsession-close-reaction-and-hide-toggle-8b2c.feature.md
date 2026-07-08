When a subsession closes (or reports a periodic run result) with the main chat as its parent, the
main agent now runs a real reaction turn instead of silently stashing the raw summary into history —
it can comment on, continue from, or acknowledge the outcome, and the reply is pushed live to a
connected browser as a new `agent_message` SSE frame. Falls back to the old passive record if no
agent is wired yet or the reaction turn itself fails, so the outcome is never lost.

The subsessions panel also hides closed/failed/interrupted subsessions by default now (they piled up
and crowded out running ones) — a "Show closed (N)" toggle in the panel header reveals them on
demand.
