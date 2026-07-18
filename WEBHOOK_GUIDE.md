# Webhook Integration Guide

## Overview

The bot exposes an HTTP endpoint that lets **Odoo push updates into the bot**.
When something changes in Odoo (e.g. a punch is approved), Odoo POSTs a signed
JSON payload to the bot, which applies the change to its authoritative SQLite
store and then re-renders the affected Discord message.

SQLite remains the source of truth — the webhook is how Odoo-originated changes
flow *back* into the bot. Outbound sync (bot → Odoo) is handled separately by
the sync worker and needs no webhook.

## Security model (read this first)

Requests are authenticated with an **HMAC-SHA256 signature**, not by "checking
that the bot can reach Odoo" (which proved nothing about the request's origin).

Every request must include two headers:

| Header | Value |
| --- | --- |
| `X-Odoo-Timestamp` | Unix seconds when the request was signed |
| `X-Odoo-Signature` | `HMAC_SHA256(secret, "<timestamp>.<raw_body>")` as hex (optionally `sha256=` prefixed) |

The bot:
1. Rejects requests whose timestamp is missing or more than **5 minutes** off (replay protection).
2. Recomputes the HMAC over `"<timestamp>.<raw_body>"` using `ODOO_WEBHOOK_SECRET` and compares in constant time.
3. Only then parses and applies the payload.

If `ODOO_WEBHOOK_SECRET` is unset, the endpoint returns `503` and accepts nothing.

> SQL is fully parameterized in the bot, so there is **no character whitelist** —
> legitimate names with apostrophes, ampersands, etc. are accepted normally.

## Setup

### 1. Environment variables (`.env`)

```
WEBHOOK_PORT=8080
ODOO_WEBHOOK_SECRET=<a long random shared secret>
# Odoo API creds are only needed for OUTBOUND sync, not to accept webhooks:
ODOO_URL=https://your-odoo-instance.com
ODOO_DB=your_database_name
ODOO_USERNAME=your_username
ODOO_API_KEY=your_api_key
```

Generate a secret, e.g. `python -c "import secrets; print(secrets.token_hex(32))"`.
Use the **same** value in Odoo's signing code.

### 2. Exposing the endpoint

- Local testing: `ngrok http 8080`
- Production: reverse proxy over **HTTPS**. Your URL looks like
  `https://your-domain.com/webhook/timetracking`.

## Endpoint

`POST /webhook/timetracking` (alias: `/webhook/odoo-timetracking`)

Headers: `Content-Type: application/json`, `X-Odoo-Timestamp`, `X-Odoo-Signature`.

Payload:

```json
{
  "channel_id": 1234567890,
  "message_id": 9876543210,
  "punch_id": 5,
  "action": "approve_punch",
  "content": "optional message text",
  "data": {}
}
```

### Supported actions

| Action | Effect |
| --- | --- |
| `update_content` | Edit the target message's text (`content` required) |
| `approve_punch` | Write approvals from `data.punch_approval_status` to SQLite, then refresh the approval buttons (removes them when fully approved) |
| `sync_database` | Re-read the punch's approval state from SQLite and refresh the approval message |
| `update_clock_view` | Re-render an employee's clock message from DB state (`data.employee_id` required) |

`approve_punch` example `data`:

```json
{ "punch_approval_status": { "punchInApproval": true, "punchOutApproval": true } }
```

## Signing example (Python)

```python
import hashlib, hmac, json, time, requests

SECRET = "your_shared_secret"
url = "https://your-domain.com/webhook/timetracking"
body = json.dumps({
    "channel_id": 1234567890,
    "message_id": 9876543210,
    "punch_id": 5,
    "action": "approve_punch",
    "data": {"punch_approval_status": {"punchInApproval": True, "punchOutApproval": True}},
}).encode()

ts = str(int(time.time()))
sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

r = requests.post(url, data=body, headers={
    "Content-Type": "application/json",
    "X-Odoo-Timestamp": ts,
    "X-Odoo-Signature": sig,
})
print(r.status_code, r.text)
```

In Odoo, put the equivalent signing logic in the server action that POSTs to the
webhook (Automation → Automated Actions).

## Response codes

| Code | Meaning |
| --- | --- |
| `200` | Applied successfully |
| `400` | Invalid JSON, missing `channel_id`/`message_id`, bad types, or unknown action |
| `401` | Missing/expired timestamp or bad signature |
| `503` | `ODOO_WEBHOOK_SECRET` not configured, or TimeTracking cog not loaded |
| `500` | Unexpected handler error (check logs) |

## Logging

Console lines are prefixed `[Webhook]`. Watch for:
- `Webhook server started on port 8080`
- `Rejected: bad signature from <ip>` / `Rejected: bad/stale timestamp from <ip>`
- `Handler error from <ip>: ...`
```
