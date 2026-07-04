# deploy-lifecycle-api skill

The deploy-lifecycle API provides **read-only** inspection of the
central-deploy management plane: service inventory, live status and
health, and configuration/environment snapshots.  All secret values in
config and environment responses are masked as `***` server-side by
`_mask_secrets`.

## Allowed operations

| Tool                          | HTTP                              | Description                              |
| ----------------------------- | --------------------------------- | ---------------------------------------- |
| `list_lifecycle_services`     | `GET /services`                   | List all managed services and status.    |
| `get_lifecycle_service_status`| `GET /services/{name}/status`     | Live status + health-check history.      |
| `get_lifecycle_service_config`| `GET /services/{name}/config`     | Service configuration (secrets masked).  |
| `get_lifecycle_service_env`   | `GET /services/{name}/env`        | Runtime environment (secrets masked).    |

## Forbidden operations

The following mutation endpoints exist on the lifecycle server but are
**not registered** under this component id — the agent has no tool to
call them and must not attempt to reach them through any other path:

- `POST /services/{name}/restart` — restart a service
- `POST /services/{name}/redeploy` — redeploy a service
- `PUT  /services/{name}/config`  — update service configuration
- `PUT  /services/{name}/env`     — update service environment
- `DELETE /services/{name}`       — remove a service registration

## Safety

All four registered tools are pure reads — they make no state changes
and can be called freely for diagnostics and investigation.  Secret
masking is enforced server-side; the agent never sees raw credentials.
