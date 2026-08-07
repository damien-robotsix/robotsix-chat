Removed the one-time config import from central-deploy: the `POST /config/import`
endpoint, the first-boot bootstrap, and the `lifecycle.config_import_enabled` /
`config_import_url` settings. It was the migration path onto per-component config
ownership; that migration is complete, the feature has been disabled by default
since it shipped, and central-deploy is decommissioning the export endpoint it
depends on.
