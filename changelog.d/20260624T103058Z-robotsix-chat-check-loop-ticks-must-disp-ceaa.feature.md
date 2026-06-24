Check-loop ticks now trigger a serialized foreground agent run.  On each
non-suppressed tick the agent answers the tick result and the reply is
recorded into the owner's active session and streamed to the browser as a
visible assistant bubble (``loop_reply`` SSE frame).  Tick results are also
rendered inline in the chat as distinct "check-loop" bubbles.  Runs are
serialized per owner so a tick-triggered run cannot race a user message.
The tick-triggered agent is built without check-loop tools, preventing
infinite recursion.
