When the subsession pool is at capacity (max_concurrent hit), the assistant now returns
a detailed workaround message instead of a generic error.  The response tells the agent
(or operator) to: (1) call list_subsessions and close/pause idle monitors to free a
slot, (2) poll the ticket/subject manually in the conversation instead of starting a
monitor, or (3) retry later once an active monitor closes on its own — and explicitly
warns not to retry in a tight loop since the pool will not free itself.
