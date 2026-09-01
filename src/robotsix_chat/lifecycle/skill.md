# deploy-lifecycle-api skill

The deploy-lifecycle API provides inspection and mutation of the central-deploy management plane:
service inventory, live status and health, environment snapshots, and (when permitted by the deploy
server's per-repo access toggle) service restart and env-write. Configuration management is owned by
each component internally — use the component's own `/config` endpoints for configuration access,
not the lifecycle API. All secret values in environment responses are masked as `***` server-side by
`_mask_secrets`.

## Allowed operations

| Tool                           | HTTP                                 | Description                                                                |
| ------------------------------ | ------------------------------------ | -------------------------------------------------------------------------- |
| `list_lifecycle_services`      | `GET /services`                      | List all managed services and status.                                      |
| `get_lifecycle_service_status` | `GET /services/{name}/status`        | Live status + health-check history.                                        |
| `get_lifecycle_service_env`    | `GET /services/{name}/env`           | Runtime environment (secrets masked).                                      |
| `restart_lifecycle_service`    | `POST /services/{name}/restart`      | Restart a service (requires per-repo access toggle).                       |
| `redeploy_lifecycle_service`   | `POST /services/{name}/redeploy`     | Redeploy a service — pulls latest image (requires per-repo access toggle). |
| `verify_lifecycle_deployment`  | `GET /services/{name}/status` (poll) | Poll service until healthy and (optionally) image matches expected ref.    |
| `self_restart`                 | `POST /chat/services/{name}/restart` | Restart this service (named via `lifecycle.service_name`).                 |
| `update_lifecycle_service_env` | `PUT /services/{name}/env`           | Update service environment (requires per-repo access toggle).              |

## Configuration ownership

Configuration is owned by each component internally — use the component's own `/config` endpoints
(e.g. `GET /config`, `PUT /config`, `POST /config/rollback`) for configuration reads and writes, not
the lifecycle API. The lifecycle API no longer exposes config-store endpoints
(`GET /services/{name}/config`, `PUT /services/{name}/config`).

## Restricted operations (per-repo access toggle)

The following mutation endpoints are available as tools but succeed only when the deploy server's
per-repo access toggle is enabled for this component. When the toggle is not enabled the calls
return a 403 error — the agent should treat that as "not permitted" and not retry:

- `POST /services/{name}/restart` — restart a service
- `POST /services/{name}/redeploy` — redeploy a service (pulls the latest image)
- `PUT  /services/{name}/env` — update service environment

**Enabling the toggle:** This is a per-repo setting in the central-deploy dashboard (labelled
`allow_chat_access` or `chat_agent_mutatable`), not a chat-component config key or environment
variable. See the
[deployment guide](../user-guide/deployment.md#4-chat-agent-mutation-access-allow_chat_access) for
step-by-step instructions.

## Deploy registration — check the toggle up front

Registering a component in the deploy plane (`POST /chat/services`) and every other chat-agent
mutation below is gated by the same per-repo `allow_chat_access` / `chat_agent_mutatable` toggle.
When it is off, all of these calls return `403`, so a deploy-registration flow that does not warn
early will run several steps and only stall at the first mutation. **Warn the user about this
requirement up front, before starting the workflow:**

1. The moment the user asks to register or deploy a component (or perform any chat-agent mutation),
   state the precondition: "Registering/deploying a component requires the
   `allow_chat_access` / `chat_agent_mutatable` toggle to be enabled for this repo in the
   central-deploy dashboard. If it is off, deployment operations will be blocked with 403."
1. If the user has not confirmed the toggle is already on, ask them to enable it in the
   central-deploy dashboard **before** you proceed, and wait for confirmation.
1. Only once the toggle is confirmed enabled should you run the registration/deployment steps.

**There is no API to read the toggle's current state** — the read-only tools
(`list_lifecycle_services`, `get_lifecycle_service_status`, `get_lifecycle_service_env`) never
report it, and the agent learns it only via a 403 on a mutation call. Because you cannot verify it
yourself, treat the toggle as possibly-off and get explicit early confirmation rather than assuming
the operation will succeed. This avoids the stalled turns and back-and-forth caused by discovering
the 403 mid-flow.

## Self-restart

`self_restart` restarts the agent's **own** service via `POST /chat/services/{name}/restart`, where
`{name}` is the configured `lifecycle.service_name` (the deploy server has no bare `/self/restart`
route). It is granted by the same `allow_chat_access` / `chat_agent_mutatable` flag as the other
chat-agent restart calls. Use this after a deploy that changed the agent's own capabilities (new
component, tool, skill, or permission) so the new capability is picked up. When
`lifecycle.service_name` is unset the tool returns a clear "not configured" message rather than
attempting a call.

This tool cannot restart other managed services — for those, use `restart_lifecycle_service`.

### URL protocol handling

If `central_deploy.url` lacks a URL scheme (e.g. `central-deploy:8100`), the client prepends the
configured `lifecycle.default_protocol` (default `"http"`). URLs with recognised schemes (`http`,
`https`) are left unchanged. An empty `base_url` produces a clear error message from `self_restart`
instead of a cryptic protocol error — configure `central_deploy.url` to the deploy-lifecycle API
address (e.g. `http://central-deploy:8100`) to enable the feature.

### Retry behaviour

On transient failures (server 5xx errors, network timeouts, connection failures) `self_restart`
retries with exponential backoff before reporting failure. Non-retryable errors (4xx client errors
such as 403 Forbidden or 404 Not Found) are returned immediately without retrying.

Retry parameters are configurable via the `lifecycle` settings:

| Setting                              | Default | Description                                                                                            |
| ------------------------------------ | ------- | ------------------------------------------------------------------------------------------------------ |
| `lifecycle.self_restart_max_retries` | `3`     | Maximum retries for transient failures. The initial attempt + retries = `max_retries + 1` total calls. |

When all retries are exhausted the method returns a combined error message describing the number of
attempts made and the last error received.

The following endpoints remain forbidden — no tool exists for them and the agent must not attempt to
reach them through any other path:

- `DELETE /services/{name}` — remove a service registration

## Post-merge deployment verification

After merging a PR that changes a tracked component (cost-fix, infrastructure change, or any code
change that produces a new container image), **do not close the ticket** until the new image is
confirmed running in production:

1. **Trigger redeploy** — call `redeploy_lifecycle_service` for the affected service.
1. **Verify deployment** — call `verify_lifecycle_deployment` with the expected image reference
   (e.g. the tag or digest the PR was built from). This polls the service status until the running
   image matches and the service is healthy.
1. **Re-measure metrics** — after the deployment is confirmed, check the key metrics the ticket was
   meant to improve (cost, latency, error rate, etc.) to confirm the change had the intended effect.
1. **Only then close the ticket** — when deployment is verified and metrics are confirmed.

The `verify_lifecycle_deployment` tool accepts an `expected_image_ref` that is matched by substring
against the running image identifier. You can pass a tag (`:main`), a digest prefix
(`sha256:abc123`), or a full reference. When `expected_image_ref` is empty, the tool verifies only
that the service is running and healthy.

## Safety

The three read-only tools are pure reads — they make no state changes and can be called freely for
diagnostics and investigation. The two mutation tools (restart, env-write) make real state changes
and are gated by the deploy server's per-repo access toggle. The `self_restart` tool is a mutation
that restarts the agent's own service (named via `lifecycle.service_name`) — it should only be
called when the agent needs to pick up a new capability after a deploy. Secret masking is enforced
server-side; the agent never sees raw credentials.
