# Mobile SSO endpoints

The [robotsix-chat-mobile](https://github.com/damien-robotsix/robotsix-chat-mobile) app
authenticates against the chat backend through a
[tinyauth](https://github.com/steveiliop56/tinyauth)-based single sign-on (SSO) flow. Two HTTP
endpoints implement the contract:

| Method | Path                      | Purpose                                                                                |
| ------ | ------------------------- | -------------------------------------------------------------------------------------- |
| `GET`  | `/auth/login`             | Mint a subject token for a tinyauth-authenticated user and redirect it back to the app |
| `POST` | `/chat/auth/mobile-token` | Exchange a subject token for a short-lived bearer access token                         |

Both endpoints share the app's `baseUrl` (same origin as `/chat`), so the public edge must route
them to the chat backend. `/chat/auth/mobile-token` in particular must be reachable **without** an
existing browser SSO session — the app calls it programmatically with only the subject token — so
any tinyauth bypass for that path is gated by the subject-token validation itself, not by an edge
session.

## Token model

| Token         | Issued by                      | Lifetime               | Binding     | Notes                                                               |
| ------------- | ------------------------------ | ---------------------- | ----------- | ------------------------------------------------------------------- |
| Subject token | `GET /auth/login`              | ~30–90 days, revocable | Single user | Opaque or signed. Cached by the app; re-used to mint access tokens. |
| Access token  | `POST /chat/auth/mobile-token` | ≤60 min                | Single user | Bearer token for subsequent API calls. Re-exchanged on expiry.      |

Both tokens are bound to exactly one authenticated user. The app caches the access token until it
expires and then silently re-exchanges the subject token for a fresh one. When an access token is
rejected with `401`, the app clears its stored subject token and prompts the user to log in again —
so an expired or invalid subject token **must** yield `401` at the exchange endpoint.

## `GET /auth/login`

Opened by the app in an external browser. The user authenticates at the tinyauth edge; the
authenticated request then reaches the chat backend, which mints a subject token bound to the
authenticated user and redirects the browser back into the app.

### Query parameters

| Parameter  | Required | Description                                                                                                      |
| ---------- | -------- | ---------------------------------------------------------------------------------------------------------------- |
| `redirect` | yes      | The app callback URI to redirect to with the minted token appended. Must satisfy the redirect allowlist (below). |

**Redirect allowlist** — the `redirect` value is validated component-by-component and rejected
unless it matches exactly:

- scheme `robotsixchat`
- host `auth`
- path `/callback`

i.e. only `robotsixchat://auth/callback` is accepted. This prevents open-redirect / token-leakage
attacks: because the subject token is appended to the redirect target, an attacker who could supply
an arbitrary `redirect` would exfiltrate a valid, single-user-bound token.

**Security** — identity is derived **only** from the tinyauth-forwarded identity (e.g. the
`Remote-User` header set at the edge). The endpoint never mints a token for an unauthenticated
request.

**Response** — `HTTP 302` redirect to:

```text
{redirect}?token={subject_token}
```

For example:

```text
Location: robotsixchat://auth/callback?token=<subject-token>
```

### Error responses

| Status | Condition                                                          |
| ------ | ------------------------------------------------------------------ |
| `401`  | Request is not authenticated at the tinyauth edge.                 |
| `400`  | `redirect` is missing, malformed, or fails the redirect allowlist. |

## `POST /chat/auth/mobile-token`

Called programmatically by the app to exchange a cached subject token for a short-lived bearer
access token. Requires **no** prior browser SSO session — the subject token itself is the
credential.

**Request body** (`application/json`)

```json
{
  "token": "<subject-token>"
}
```

**Response body** (`application/json`)

```json
{
  "access_token": "<access-token>",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

- `access_token` — the short-lived bearer token to send as `Authorization: Bearer <access_token>` on
  subsequent API calls. (The app also accepts `token` as the access-token field name for backward
  compatibility.)
- `token_type` — always `Bearer`.
- `expires_in` — access-token lifetime in seconds (≤ 3600).

**Security** — the endpoint validates the subject token (signature/opaque lookup, expiry,
revocation, single-user binding) and issues an access token bound to the same user. It does not
require or consult a browser SSO session.

### Error responses

| Status | Condition                                                                                                    |
| ------ | ------------------------------------------------------------------------------------------------------------ |
| `401`  | Subject token is invalid, expired, or revoked. The app clears the stored subject token and prompts re-login. |
| `400`  | Request body is missing or malformed (e.g. no `token` field).                                                |

## Complete flow

1. The app opens `GET /auth/login?redirect=robotsixchat://auth/callback` in an external browser.

1. The user authenticates at the tinyauth edge; the authenticated request reaches the chat backend.

1. The backend mints a subject token bound to the user and responds with
   `HTTP 302 Location: robotsixchat://auth/callback?token=<subject-token>`.

1. The OS hands the `robotsixchat://` deep link back to the app, which extracts and caches the
   subject token.

1. The app exchanges it for an access token:

   ```http
   POST /chat/auth/mobile-token
   Content-Type: application/json

   {"token": "<subject-token>"}
   ```

   ```json
   {"access_token": "<access-token>", "token_type": "Bearer", "expires_in": 3600}
   ```

1. The app calls the chat API with the bearer token:

   ```http
   POST /chat
   Authorization: Bearer <access-token>
   Content-Type: application/json

   {"message": "..."}
   ```

1. When the access token expires, the app silently re-exchanges the cached subject token (step 5).
   If the exchange returns `401`, the app clears the subject token and restarts the login flow (step
   1).
