Remove the embedded HTTP Basic auth system (robotsix-standards component
standard: authentication is centralized at the central-deploy gateway).
Deletes `chat/auth.py`, the `AuthSettings` model, the `AUTH_*` env overrides
and compose slots, and the related docs — the server ships no user-facing
auth; outside the gateway, auth is the operator's responsibility.
