Board writes that fail due to broker unavailability are now automatically
retried with exponential backoff (initial ~15 min, max 4 hr, ±20 % jitter);
retry state is persisted to `.data/board_write_queue.json` and inspectable
via the new `get_board_write_queue_status` tool.
