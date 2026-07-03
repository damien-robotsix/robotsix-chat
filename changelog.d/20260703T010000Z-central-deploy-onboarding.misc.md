Make robotsix-chat deployable via central-deploy: rewrite `deploy/docker-compose.yml` to the deploy
contract (version header, single primary service, GHCR image, named `chat-data` volume, env secret
slots, `claude-mount` label) and retire the Watchtower stack. Align the container with the
standardized robotsix layout: user `app` (uid 1001), home `/home/app` — matching central-deploy's
new `/home/app/.claude` claude-mount target. Deployment docs (deploy/README.md, user guide,
AGENT.md) rewritten accordingly.
