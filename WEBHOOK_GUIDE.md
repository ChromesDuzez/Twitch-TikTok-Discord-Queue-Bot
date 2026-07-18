# Webhook Integration Guide

## Overview

The bot exposes HTTP endpoints that let **Odoo notify the bot that a record
changed**. Odoo POSTs a tiny pointer (just the record `_id`); the bot then
**pulls that record from Odoo** over the authenticated API and reconciles it
into its authoritative SQLite store, refreshing any affected Discord message.

SQLite stays the source of truth. Outbound sync (bot → Odoo) is handled
separately by the sync worker and needs no webhook.

## Why this is safe without HMAC signing

The payload never carries data the bot trusts — it's a pointer the bot
re-fetches authoritatively. So:

- **The model comes from the URL, not the body.** There's one endpoint per
  model. The only thing read from the payload is an integer `_id`. A
  malicious/garbage `_model` value can't do anything because it's never used.
- **A forged request can't inject data** — worst case it makes the bot do a
  redundant Odoo read that reconciles to truth (a no-op). Replay is harmless for
  the same reason.
- The residual risk is just *abuse/DoS*, which a shared **token** handles. HMAC
  signing (and the Odoo-side code to produce it) is unnecessary here.

## Authentication

### Token (required)

The bot checks a shared secret token, in constant time. Provide it either way:

- URL query: `...?token=<WEBHOOK_TOKEN>`  ← works with Odoo's no-code webhook
- Header: `X-Webhook-Token: <WEBHOOK_TOKEN>`

`WEBHOOK_TOKEN` is **auto-generated on first startup** and written to `.env` if
blank — you don't create it by hand. Rotate it anytime with the
`/regenwebhooktoken` slash command (the new value is **not** shown in Discord;
read it from the server's `.env`, then update your Odoo URLs).

### IP allowlist (optional, off by default)

A coarse pre-filter. When enabled (`/webhookallowlist enable`), the bot only
accepts webhooks from IPs in `WEBHOOK_IP_ALLOWLIST`, which is auto-refreshed
with the Odoo host's resolved IP(s) every time the bot calls Odoo. It uses the
real client IP behind Cloudflare (`CF-Connecting-IP`).

> ⚠️ Caveat: with Cloudflare-fronted Odoo hosting, the IP you *connect to* (the
> frontend) may differ from the IP Odoo *sends from* (its egress). If enabling
> the allowlist blocks legit webhooks, that mismatch is why — just disable it
> (the token is the real auth) or add the egress IP to `WEBHOOK_IP_ALLOWLIST`.

## Endpoints (one per model)

```
POST /webhook/odoo/res.partner?token=<TOKEN>
POST /webhook/odoo/hr.attendance?token=<TOKEN>
POST /webhook/odoo/account.analytic.line?token=<TOKEN>
```

Headers: `Content-Type: application/json`.
Body (Odoo's native webhook sends exactly this):

```json
{ "_id": 123 }
```

`_action` and `write_uid` are accepted if present but optional. The bot returns
`200` as soon as the pointer is queued; the pull + reconcile happen
asynchronously (usually within seconds).

## Exposing the endpoint (Cloudflare Zero Trust tunnel — recommended)

You do **not** need to port-forward. Run the bot with a **Cloudflare Zero Trust
tunnel** (`cloudflared`), which dials out to Cloudflare and publishes a public
`https://` hostname that forwards to the bot's local `WEBHOOK_PORT`:

- No inbound ports opened on your network/firewall.
- **HTTPS is automatic** (Cloudflare terminates TLS), so the `?token=` in the
  URL is encrypted in transit.
- Cloudflare sets `CF-Connecting-IP` to the real origin, which is what the IP
  allowlist checks.

Quick setup: in the Cloudflare Zero Trust dashboard, create a **Tunnel**, add a
**Public Hostname** (e.g. `hooks.yourdomain.com`) with service
`http://localhost:8080` (your `WEBHOOK_PORT`), and run the provided `cloudflared`
connector on the bot's host. Your Odoo URLs then look like
`https://hooks.yourdomain.com/webhook/odoo/hr.attendance?token=<TOKEN>`.

> The webhook auth still applies end-to-end: the token is required regardless of
> the tunnel. Optionally, Cloudflare Access policies can add another layer in
> front of the endpoint.

## Setting it up in Odoo (Enterprise 17+, incl. 19.0+e — no code)

Odoo's native webhook automation posts the pointer for you; there is nothing to
write or maintain on the Odoo side.

For **each** model (`hr.attendance`, `account.analytic.line`, `res.partner`):

1. Enable developer mode: **Settings → Developer Tools → Activate the developer mode**.
2. **Settings → Technical → Automation Rules → New**.
3. **Model**: e.g. *Attendance*. **Trigger**: *On Save* (or On Creation / On Update).
4. **Actions To Do** → add **“Send Webhook Notification”**.
5. **URL**: the matching endpoint above, including `?token=<your WEBHOOK_TOKEN>`.
6. Save.

That's it — Odoo sends `{"_id": <record id>}` (with `_model`) to that URL on the
trigger; the bot authenticates the token, pulls the record, and reconciles.

## Response codes

| Code | Meaning |
| --- | --- |
| `200` | Pointer accepted and queued |
| `400` | Missing/invalid integer `_id`, or wrong Content-Type |
| `401` | Missing or wrong token |
| `403` | IP allowlist enabled and the source IP isn't allowed |
| `503` | Token not configured, or TimeTracking cog not loaded |

## Testing (curl)

```bash
curl -X POST "https://your-domain.com/webhook/odoo/hr.attendance?token=YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"_id": 123}'
```

## Logging

Console lines are prefixed `[Webhook]`. Rejections log the source IP and reason
(bad token, IP not allowed). Successful timecard reconciliation is logged to the
timecard log channel; webhook/transport issues go to the general log channel.
