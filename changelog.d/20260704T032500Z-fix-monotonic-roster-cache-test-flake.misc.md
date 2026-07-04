Fix boot-time flake in the roster-cache expiry test: anchor the fake timestamp to time.monotonic()
instead of absolute 0.0, which reads as fresh on newly booted CI runners.
