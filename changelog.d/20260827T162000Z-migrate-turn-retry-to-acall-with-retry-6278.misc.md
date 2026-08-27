Migrate the subsession turn-retry loop off its hand-rolled backoff onto
`robotsix_http.acall_with_retry`, keeping the transient classification (OpenRouter hiccups, GitHub
rate limits, malformed provider responses) as chat-side domain knowledge. Removes the now-unread
`subsessions.transient_error_backoff_base` and `subsessions.transient_error_backoff_cap` settings;
`transient_error_max_retries` still controls the attempt count.
