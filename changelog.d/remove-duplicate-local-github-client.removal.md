Remove the local `GitHubClient` fallback and `GithubSettings` (skill.md, token, api_base_url) that
intercepted `component_request(component_id="github", ...)` calls locally. GitHub access — Actions
status plus repo read/update/create — now goes exclusively through central-deploy's `github` roster
component, matching every other component and removing a second, drifting implementation of the same
capability.
