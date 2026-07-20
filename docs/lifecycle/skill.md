# deploy-lifecycle-api skill

The deploy-lifecycle API provides inspection and mutation of the central-deploy management
plane: service inventory, live status and health, configuration/environment snapshots, and
(when permitted by the deploy server's per-repo access toggle) service restart, config-write,
and env-write. All secret values in config and environment responses are masked as `***`
server-side by `_mask_secrets`.

## Allowed operations

| Tool                              | HTTP                          | Description                                                          |
| --------------------------------- | ----------------------------- | -------------------------------------------------------------------- |
| `list_lifecycle_services`         | `GET /services`               | List all managed services and status.                                |
| `get_lifecycle_service_status`    | `GET /services/{name}/status` | Live status + health-check history.                                  |
| `get_lifecycle_service_config`    | `GET /services/{name}/config` | Service configuration (secrets masked).                              |
| `get_lifecycle_service_env`       | `GET /services/{name}/env`    | Runtime environment (secrets masked).                                |
| `watch_service_redeploy`          | (polls config + status)       | Block until a service config changes (redeploy detected) or timeout. |
| `restart_lifecycle_service`       | `POST /services/{name}/restart` | Restart a service (requires per-repo access toggle).               |
| `update_lifecycle_service_config` | `PUT /services/{name}/config`   | Update service configuration (requires per-repo access toggle).    |
| `update_lifecycle_service_env`    | `PUT /services/{name}/env`      | Update service environment (requires per-repo access toggle).      |

### `watch_service_redeploy`

A polling tool that blocks until a lifecycle-managed service is redeployed or a timeout expires. It
snapshots the service config at call time and polls every `poll_interval_seconds` (default 15 s, min
5 s) until the config body changes — indicating that a new deployment has rolled out. The tool
returns early as soon as a change is detected, reporting the elapsed time, poll count, and the
service's current status.

**When to use it:** after a fix is merged into a component repo (e.g. robotsix-mill) and the
deployed service is still running the stale digest. Call `watch_service_redeploy` to wait for the
redeploy instead of retrying the same operation against the old deployment — this breaks the
redraft-loop pattern where every attempt hits the same stale code.

**Timeout behaviour:** if `max_wait_seconds` (default 300 s) expires without a config change, the
tool returns a message recommending the operator trigger a manual redeploy via the central-deploy
dashboard. It does NOT auto-spawn background monitors — each call is a single, self-contained
polling session.

## Restricted operations (per-repo access toggle)

The following mutation endpoints are available as tools but succeed only when the deploy
server's per-repo access toggle is enabled for this component.  When the toggle is not
enabled the calls return a 403 error — the agent should treat that as "not permitted"
and not retry:

- `POST /services/{name}/restart` — restart a service
- `PUT  /services/{name}/config` — update service configuration
- `PUT  /services/{name}/env` — update service environment

The following endpoints remain forbidden — no tool exists for them and the agent must
not attempt to reach them through any other path:

- `POST /services/{name}/redeploy` — redeploy a service
- `DELETE /services/{name}` — remove a service registration

## Safety

The five read-only tools are pure reads — they make no state changes and can be called
freely for diagnostics and investigation.  The three mutation tools (restart, config-write,
env-write) make real state changes and are gated by the deploy server's per-repo access
toggle.  Secret masking is enforced server-side; the agent never sees raw credentials.
