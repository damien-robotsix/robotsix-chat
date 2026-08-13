# Gateway Route — read-only routing diagnostic

You have a `check_gateway_route` tool that checks whether a service has an active edge-gateway
route. central-deploy derives its edge routing table automatically from the component registry: every
registered, routable component with `id == <slug>` is published at `<slug>.<gateway_base_domain>`,
and the registry entry's container name/port is the upstream. There is no per-service routing rule —
a route "exists" exactly when the slug is present in the registry.

## When to use it

- Central-deploy's "Fetch Spec" step returned a 404 or a missing-header error and you want to know
  immediately whether the service has a gateway route, instead of probing the public URL and
  comparing with container health manually.
- Confirm that a newly onboarded service is actually reachable through the edge at
  `<slug>.<gateway_base_domain>`.
- Pinpoint a missing route before reporting to the user — the tool returns the current
  vhost → upstream mapping and a direct "route present / missing" conclusion.

## Allowed operation

| Tool                 | Description                                                                             |
| -------------------- | --------------------------------------------------------------------------------------- |
| `check_gateway_route` | Reads central-deploy's component registry and compares it with the expected route.       |

The tool signature is:

```python
check_gateway_route(service_slug: str) -> str
```

## Return value

A JSON string with these fields:

- `service_slug` — the slug you supplied
- `gateway_base_domain` — the configured fleet base domain
- `expected_route` — `"<service_slug>.<gateway_base_domain>"` (the vhost central-deploy would publish)
- `route_present` — `true` when the slug appears in the registry (so the edge publishes it)
- `matching_mappings` — registry entries whose `component_id` equals the slug, with `vhost` and `upstream`
- `current_mappings` — the full current vhost → upstream mapping derived from the registry
- `diagnosis` — a human-readable conclusion
- `error` — non-empty when the registry could not be read, the slug is invalid, or a required
  setting is unset

## Interpretation

- `route_present: true` — the service is registered; the edge publishes
  `<slug>.<gateway_base_domain>` to the listed upstream. A 404 from central-deploy's spec fetch is
  therefore unlikely to be a gateway-route problem.
- `route_present: false` — the service is missing from the registry. The edge derives no vhost for
  it, so no gateway route exists. This is the "missing route" case: report it to the user and
  route the remediation to onboarding/infra rather than continuing to probe the public URL.

## Safety

- **Read-only** — the tool performs a single `GET` against central-deploy's component registry and
  makes no state changes.
- **No user-supplied URL** — the endpoint is fixed (`GET /components/suggest`); the slug is only
  compared against registry ids and never placed in the request path.
- **Authenticated** — the request carries the configured central-deploy `X-API-Key`, injected
  server-side; the agent never sees the credential.
- **Timeout** — the request has a short timeout (default 30 s); one request per call.
