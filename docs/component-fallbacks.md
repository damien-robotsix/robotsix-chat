# Component Fallbacks Configuration

## Overview

The robotsix-chat system connects to multiple external components (mail, mill, calendar, etc.) through the central-deploy roster. When the central-deploy API is unavailable or incomplete, the chat service falls back to pre-configured fallback URLs defined in `central_deploy.component_fallbacks`.

Without proper fallback entries, transient roster outages can cause cascading failures and lost functionality.

## Purpose

Component fallbacks serve two critical functions:

1. **Resilience during roster outages**: If central-deploy is temporarily down, the chat can continue operating by using cached or fallback component URLs.
2. **Graceful degradation**: If a component is missing from the roster (e.g., after a redeploy), the service can still attempt to reach it via the fallback URL instead of failing immediately.

## Configuration

Fallback entries are defined in `config/config.json` under `central_deploy.component_fallbacks`:

```json
{
  "central_deploy": {
    "component_fallbacks": {
      "calendar": "http://calendar:8080",
      "mail": "http://mail:8080",
      "mill": "http://mill:8080"
    }
  }
}
```

### Format

- **Key**: Component ID (must match the ID returned by `GET /chat/components` from central-deploy)
- **Value**: Base URL for the component (must be a valid `http://` or `https://` URL)

### Critical Components

The following components are considered critical and **must always have fallback entries**:

| Component | Service | Fallback URL Pattern |
|-----------|---------|----------------------|
| `calendar` | Calendar service (scheduling, availability) | `http://calendar:8080` |
| `mail` | Mail service (notifications, alerts) | `http://mail:8080` |
| `mill` | Mill service (ticket management, board state) | `http://mill:8080` |

## Adding a New Component

When integrating a new component:

1. **Register in central-deploy**: Add the component to `GET /chat/components` with its base URL and skill documentation (see [robotsix-central-deploy](https://github.com/damien-robotsix/robotsix-central-deploy) repo).

2. **Add a fallback entry**: Update `config/config.json` to add a fallback entry for the new component:
   ```json
   "component_fallbacks": {
     "new-component-id": "http://new-component:8080"
   }
   ```

3. **Update the critical list**: If the component is critical to chat functionality, add it to the `CRITICAL_COMPONENTS` list in `scripts/check_component_fallbacks.py` so CI validation will catch future misconfigurations.

4. **Add documentation**: Update this file to document the new component.

## Validation

A CI check (`scripts/check_component_fallbacks.py`) validates that all critical components have complete fallback entries:

- Ensures each critical component ID exists in `component_fallbacks`
- Validates that fallback URLs are non-empty and properly formatted (http/https)
- Fails the CI build if any critical component is missing a fallback

### Running the validation locally

```bash
python scripts/check_component_fallbacks.py
```

### Output examples

**Success**:
```
$ python scripts/check_component_fallbacks.py
$ echo $?
0
```

**Failure (missing component)**:
```
$ python scripts/check_component_fallbacks.py
MISSING: component 'mail' (Mail service (notifications, alerts)) has no fallback entry in central_deploy.component_fallbacks

To fix: add missing components to central_deploy.component_fallbacks in config/config.json
Example: '"<component_id>": "http://<host>:<port>"'
See docs/configuration.md for details.
$ echo $?
1
```

## See Also

- [Configuration Guide](docs/configuration.md#central_deploy) - Full documentation on `central_deploy` settings
- [Component Access](src/robotsix_chat/component_access/roster.py) - How fallbacks are used at runtime
- [Central Deploy](https://github.com/damien-robotsix/robotsix-central-deploy) - Component roster source of truth
