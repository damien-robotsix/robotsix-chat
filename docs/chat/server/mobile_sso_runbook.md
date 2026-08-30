# Mobile SSO Operational Runbook

This runbook covers operational procedures for the mobile SSO authentication
endpoints (`GET /auth/login`, `GET /auth/callback`, `POST /chat/auth/mobile-token`).

## Overview

The mobile SSO flow allows the mobile app to authenticate users via tinyauth
and obtain short-lived bearer tokens for API access.  The flow is:

1. **Login initiation**: Mobile app opens `GET /auth/login?redirect_to=<deep-link>`
   in an external browser.
2. **Tinyauth authentication**: User authenticates at the tinyauth edge proxy.
3. **Callback**: Tinyauth redirects to `GET /auth/callback`, which redirects
   to the mobile app's deep-link.
4. **Token exchange**: Mobile app calls `POST /chat/auth/mobile-token` to
   exchange the tinyauth identity header for a short-lived bearer token.

## Configuration

Mobile SSO is controlled by the `mobile_auth` configuration block:

```json
{
  "mobile_auth": {
    "enabled": false,
    "tinyauth_url": "https://auth.example.com",
    "subject_header": "X-Forwarded-User",
    "session_header": "X-Forwarded-Session",
    "token_secret": "<hmac-secret>",
    "token_ttl_seconds": 3600,
    "allowed_redirect_domains": ["app.example.com"],
    "callback_base_url": "https://chat.example.com"
  }
}
```

### Key configuration fields

| Field | Description | Default |
|-------|-------------|---------|
| `enabled` | Master switch for mobile SSO endpoints | `false` |
| `tinyauth_url` | Base URL of the tinyauth instance | `""` |
| `subject_header` | HTTP header where tinyauth writes the authenticated user identity | `X-Forwarded-User` |
| `token_secret` | HMAC secret for signing bearer tokens | `""` |
| `token_ttl_seconds` | Bearer token lifetime in seconds | `3600` |
| `allowed_redirect_domains` | Allowlist for `redirect_to` parameter | `[]` |
| `callback_base_url` | Public base URL of this chat server | `""` |

## Disabling Mobile SSO

### Immediate disable (no redeploy)

Set `mobile_auth.enabled` to `false` in the configuration file and restart
the service:

```bash
# Edit the config file
vi /path/to/config.json

# Restart the service
systemctl restart robotsix-chat
# or
docker compose restart robotsix-chat
```

When disabled:
- `GET /auth/login` returns 404
- `GET /auth/callback` returns 404
- `POST /chat/auth/mobile-token` returns 404
- Existing bearer tokens remain valid until they expire

### Emergency disable (rotate token secret)

If you suspect token compromise, rotate the `token_secret` to invalidate
all existing tokens immediately:

1. Generate a new secret:
   ```bash
   openssl rand -hex 32
   ```

2. Update `mobile_auth.token_secret` in the config file.

3. Restart the service.

All existing bearer tokens will fail verification after restart.

## Monitoring

### Prometheus Metrics

The following metrics are exposed at `GET /metrics`:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `robotsix_chat_auth_login_requests_total` | Counter | `status` | Total GET /auth/login requests |
| `robotsix_chat_auth_callback_requests_total` | Counter | `status` | Total GET /auth/callback requests |
| `robotsix_chat_mobile_token_exchange_requests_total` | Counter | `status` | Total POST /chat/auth/mobile-token requests |
| `robotsix_chat_redirect_validation_rejections_total` | Counter | — | Total redirect-validation failures |
| `robotsix_chat_token_issuance_events_total` | Counter | — | Total bearer tokens issued |
| `robotsix_chat_token_verification_failures_total` | Counter | `reason` | Total bearer token verification failures |

### Key queries

**Token exchange success rate:**
```promql
rate(robotsix_chat_mobile_token_exchange_requests_total{status="200"}[5m])
/ rate(robotsix_chat_mobile_token_exchange_requests_total[5m])
```

**401 rate (token exchange):**
```promql
rate(robotsix_chat_mobile_token_exchange_requests_total{status="401"}[5m])
```

**Redirect rejection rate:**
```promql
rate(robotsix_chat_redirect_validation_rejections_total[5m])
```

## Alerting

Alert rules are defined in `deploy/alerts/mobile_sso.yml`.  Deploy this
file to your Prometheus server's rules directory.

### Alert: MobileSsoTokenExchange401Spike

**Condition:** 401 rate > 0.1/sec for 5 minutes

**Possible causes:**
- Attack (brute-force subject token guessing)
- Mass token expiry (tokens issued at the same time expiring together)
- Misconfigured edge proxy stripping the identity header

**Response:**
1. Check logs for patterns in the 401 responses (same IP? same user?).
2. If attack suspected: consider rate-limiting or temporarily disabling.
3. If mass expiry: check when tokens were issued and consider extending TTL.
4. If misconfiguration: verify tinyauth is setting the identity header.

### Alert: MobileSsoRedirectValidationRejections

**Condition:** Rejection rate > 0.05/sec for 5 minutes

**Possible causes:**
- Mobile app using an unlisted redirect domain
- Attack attempting open-redirect exploitation
- Configuration drift (domain removed from allowlist)

**Response:**
1. Check logs for the rejected `redirect_to` values.
2. If legitimate domain: add to `allowed_redirect_domains`.
3. If attack: investigate source and consider blocking.

### Alert: MobileSsoTokenVerificationFailures

**Condition:** Verification failure rate > 0.1/sec for 5 minutes

**Possible causes:**
- Token tampering
- Clock skew between servers
- Token secret rotated without coordinating all instances

**Response:**
1. Check logs for the failure reason (expired, tampered, malformed).
2. If clock skew: verify NTP synchronization.
3. If secret mismatch: ensure all instances use the same `token_secret`.

### Alert: MobileSsoEndpointsDisabled

**Condition:** No auth_login_requests metrics for 10 minutes

**Possible causes:**
- Intentional disable (`enabled: false`)
- Configuration error
- Service not receiving traffic

**Response:**
1. Verify `mobile_auth.enabled` in config.
2. If intentional: no action needed.
3. If unintentional: check configuration and restart.

## Audit Logging

All auth-sensitive operations are logged with structured fields for
audit trail purposes.  Logs are emitted via structlog in JSON format
when `structured_logging` is enabled.

### Logged events

| Event | Log level | Fields |
|-------|-----------|--------|
| `auth_login_redirect` | INFO | `redirect_to`, `tinyauth_url` |
| `redirect_validation_rejection` | WARNING | `redirect_to`, `allowed_domains` |
| `auth_callback_redirect` | INFO | `subject`, `redirect_to` |
| `token_issuance` | INFO | `subject`, `ttl` |
| `token_exchange_failure` | WARNING | `reason` |

### Querying audit logs

**All token issuances for a user:**
```json
{"event": "token_issuance", "subject": "alice"}
```

**All redirect rejections:**
```json
{"event": "redirect_validation_rejection"}
```

**All token exchange failures:**
```json
{"event": "token_exchange_failure"}
```

## Token Lifecycle

### Subject tokens

The current implementation does not use persistent subject tokens.
The tinyauth identity header is used directly for token exchange.
This means:

- **No server-side token storage**: Tokens are HMAC-signed and stateless.
- **No individual revocation**: Cannot revoke a single user's tokens
  without rotating the shared secret.
- **Revocation by secret rotation**: Rotating `token_secret` invalidates
  ALL existing tokens immediately.

### Bearer tokens

Bearer tokens are HMAC-signed with the following structure:
```
<subject>|<expiry_timestamp>|<hmac_signature>
```

- **Lifetime**: Controlled by `token_ttl_seconds` (default: 3600 seconds / 1 hour).
- **Verification**: Server verifies HMAC signature and checks expiry.
- **No refresh**: Mobile app must re-exchange when token expires.

## Incident Response

### Suspected token compromise

1. **Immediate**: Rotate `token_secret` in config and restart.
2. **Investigate**: Check audit logs for unusual token issuance patterns.
3. **Notify**: Inform affected users they need to re-authenticate.

### Suspected attack

1. **Rate limiting**: Consider adding rate limiting to auth endpoints.
2. **IP blocking**: Block suspicious IPs at the edge proxy.
3. **Temporary disable**: Set `enabled: false` if attack is severe.

### Edge proxy misconfiguration

1. **Verify headers**: Ensure tinyauth sets `X-Forwarded-User` correctly.
2. **Check bypass**: Ensure `/chat/auth/mobile-token` is accessible
   without browser SSO (the app calls it programmatically).
3. **Test flow**: Verify the complete login flow on a test device.

## Related Documentation

- [Mobile SSO Configuration](../../configuration.md#mobile-auth-sso)
- [Architecture: Authentication](../../architecture.md)
- [API Reference: Auth Endpoints](../../api/mobile-sso.md)
