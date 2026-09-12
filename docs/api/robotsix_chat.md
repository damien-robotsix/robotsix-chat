# robotsix_chat HTTP API

The robotsix-chat server exposes a JSON + SSE HTTP API for the chat agent and its
supporting subsystems (sessions, subsessions, config, diagnostics, GitHub automation,
metrics, and mobile-SSO auth).

## Discoverability

The API is self-describing at runtime:

- `GET /openapi.json` — the OpenAPI 3.0.2 document describing every endpoint, its
  method, path parameters, and request/response contract.
- `GET /docs` — a SwaggerUI page that renders `/openapi.json` in a browser.

Both are served by the app itself, so they sit behind the same central-deploy gateway
auth layer as every other endpoint. Scripts and tools that call `/chat`, `/sessions`,
`/config`, `/github/*` etc. can discover the exact contract from `/openapi.json`
without reading server source.

## Versioning

Every endpoint is registered at its historical root path (so existing un-versioned
callers keep working during the transition) **and**, for the stable contract, under
the `/api/v1` prefix — e.g. `POST /chat` is also served at `POST /api/v1/chat`. The
two ops probes, `/health` and `/metrics`, are deliberately root-only.

## Endpoints

All rows below except `/health` and `/metrics` are also served under the `/api/v1`
prefix (e.g. `POST /api/v1/chat`).

| Method   | Path                                                            | Summary                                                            |
| -------- | --------------------------------------------------------------- | ------------------------------------------------------------------ |
| `GET`    | `/health`                                                        | Liveness probe — returns the service status.                       |
| `GET`    | `/metrics`                                                       | Prometheus metrics scrape endpoint.                                |
| `GET`    | `/auth/login`                                                    | Mint a subject token and redirect to the mobile SSO app.           |
| `GET`    | `/auth/callback`                                                 | Mobile SSO callback — exchange an auth code for a session.         |
| `POST`   | `/chat/auth/mobile-token`                                        | Exchange a subject token for a short-lived bearer access token.    |
| `GET`    | `/admin/disk`                                                    | Report on-disk usage of the service volumes.                       |
| `POST`   | `/admin/prune`                                                   | Prune stale or excess stored data.                                 |
| `POST`   | `/mill-events`                                                   | Ingest a mill (workflow) event.                                    |
| `POST`   | `/chat`                                                          | Submit a chat message; the reply streams back over SSE.            |
| `POST`   | `/chat/queue/cancel`                                             | Cancel a queued (not yet running) chat turn.                       |
| `GET`    | `/events`                                                        | Server-Sent Events stream of chat and agent events.                |
| `GET`    | `/history`                                                       | List the conversation history.                                     |
| `GET`    | `/models`                                                        | List the available chat model levels.                              |
| `GET`    | `/sessions`                                                      | List chat sessions.                                                |
| `POST`   | `/sessions`                                                      | Create a new chat session.                                         |
| `POST`   | `/sessions/{session_id}/model`                                   | Set the model level used by a session.                             |
| `DELETE` | `/sessions/{session_id}`                                         | Delete a chat session.                                             |
| `POST`   | `/sessions/{session_id}/close`                                   | Close a chat session.                                              |
| `GET`    | `/periodic/definitions`                                          | List periodic (scheduled) session definitions.                     |
| `POST`   | `/periodic/definitions/{name}/run`                               | Run a periodic session definition on demand.                       |
| `GET`    | `/sessions/{session_id}/draft`                                   | Fetch the current draft for a session.                             |
| `PUT`    | `/sessions/{session_id}/draft`                                   | Save the current draft for a session.                              |
| `GET`    | `/subsessions`                                                   | List subsessions.                                                  |
| `GET`    | `/subsessions/{sub_id}`                                          | Fetch a subsession.                                                |
| `GET`    | `/subsessions/{sub_id}/transcript`                               | Fetch a subsession transcript.                                     |
| `POST`   | `/subsessions/{sub_id}/message`                                  | Send a message into a subsession.                                  |
| `POST`   | `/subsessions/{sub_id}/close`                                    | Close a subsession.                                                |
| `POST`   | `/chat/github/repos`                                             | Create a GitHub repository.                                        |
| `PATCH`  | `/chat/github/repos/{owner}/{repo}/settings`                     | Update GitHub repository settings.                                 |
| `PUT`    | `/chat/github/repos/{owner}/{repo}/actions/secrets/{secret_name}` | Set a GitHub Actions repository secret.                            |
| `POST`   | `/chat/github/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches` | Dispatch a GitHub Actions workflow.                |
| `GET`    | `/chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`   | Fetch a GitHub Actions job log.                                    |
| `GET`    | `/chat-skill`                                                    | Return the chat skill definition.                                  |
| `GET`    | `/config`                                                        | Read the effective configuration.                                  |
| `GET`    | `/config/deploy`                                                 | Read the deploy-plane configuration.                               |
| `PUT`    | `/config`                                                        | Save/update the configuration.                                     |
| `GET`    | `/config/versions`                                               | List the configuration versions.                                   |
| `GET`    | `/config/versions/{version}`                                     | Read a specific configuration version.                             |
| `GET`    | `/config/versions/{version}/diff`                                | Diff two configuration versions.                                   |
| `POST`   | `/config/rollback`                                               | Roll back to a previous configuration version.                     |
| `POST`   | `/diagnostics/events`                                            | Record a diagnostic event.                                         |
| `GET`    | `/diagnostics/events`                                            | List the recorded diagnostic events.                               |

The authoritative, machine-readable contract — including per-endpoint summaries,
path parameters, and request/response bodies — is `GET /openapi.json`. The table
above is a human-readable index; when the two disagree, the schema wins.
