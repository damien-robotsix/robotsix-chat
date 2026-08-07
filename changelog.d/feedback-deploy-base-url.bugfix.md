The feedback runner's repo-roster lookup now uses the configured
`lifecycle.base_url` instead of a hardcoded `http://central-deploy:8100`. That
hostname resolves only on the deploy stack's internal compose network, so a chat
attached to `central-deploy-proxy` failed DNS on every lookup and silently fell
back to `["robotsix-chat"]` — narrowing feedback to a single repo with nothing
but a log line to show for it.
