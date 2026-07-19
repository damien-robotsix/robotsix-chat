# deploy-lifecycle-api skill

The deploy-lifecycle API provides **read-only** inspection of the central-deploy management plane:
service inventory, live status and health, and configuration/environment snapshots. All secret
values in config and environment responses are masked as `***` server-side by `_mask_secrets`.

## Allowed operations

| Tool                           | HTTP                          | Description                             |
| ------------------------------ | ----------------------------- | --------------------------------------- |
| `list_lifecycle_services`      | `GET /services`               | List all managed services and status.   |
| `get_lifecycle_service_status` | `GET /services/{name}/status` | Live status + health-check history.     |
| `get_lifecycle_service_config` | `GET /services/{name}/config` | Service configuration (secrets masked). |
| `get_lifecycle_service_env`    | `GET /services/{name}/env`    | Runtime environment (secrets masked).   |
| `watch_service_redeploy`       | (polls config + status)       | Block until a service config changes (redeploy detected) or timeout. |

### `watch_service_redeploy`

A polling tool that blocks until a lifecycle-managed service is redeployed
or a timeout expires. It snapshots the service config at call time and
polls every `poll_interval_seconds` (default 15 s, min 5 s) until the
config body changes — indicating that a new deployment has rolled out.
The tool returns early as soon as a change is detected, reporting the
elapsed time, poll count, and the service's current status.

**When to use it:** after a fix is merged into a component repo (e.g.
robotsix-mill) and the deployed service is still running the stale
digest. Call `watch_service_redeploy` to wait for the redeploy instead
of retrying the same operation against the old deployment — this breaks
the redraft-loop pattern where every attempt hits the same stale code.

**Timeout behaviour:** if `max_wait_seconds` (default 300 s) expires
without a config change, the tool returns a message recommending the
operator trigger a manual redeploy via the central-deploy dashboard.
It does NOT auto-spawn background monitors — each call is a single,
self-contained polling session.

## Forbidden operations

The following mutation endpoints exist on the lifecycle server but are **not registered** under this
component id — the agent has no tool to call them and must not attempt to reach them through any
other path:

- `POST /services/{name}/restart` — restart a service
- `POST /services/{name}/redeploy` — redeploy a service
- `PUT  /services/{name}/config` — update service configuration
- `PUT  /services/{name}/env` — update service environment
- `DELETE /services/{name}` — remove a service registration

## Safety

All four registered tools are pure reads — they make no state changes and can be called freely for
diagnostics and investigation. Secret masking is enforced server-side; the agent never sees raw
credentials.
