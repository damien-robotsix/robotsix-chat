component_request now honours roster auth metadata: 'basic' entries resolve
username_env/password_env into HTTP Basic Auth and 'header' entries resolve token_env into the named
header (e.g. X-API-Key), with explicit provisioning errors when the env vars are absent. Enables the
langfuse and deploy virtual components (central-deploy #333).
