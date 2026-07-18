"""Inbound webhook server for Odoo -> Discord updates (pull-based).

Odoo notifies the bot that a record changed; the bot then **pulls that record
from Odoo** and reconciles it into its authoritative SQLite store. Because the
payload is only a pointer, this endpoint's job is narrow and its trust surface
is tiny:

* **The model comes from the URL, not the payload.** There is one route per
  supported model (``/webhook/odoo/<model>``), so the only thing read from the
  body is an integer ``_id``. A malicious/garbage ``_model`` field is impossible
  to exploit because it is never used.
* **Auth is a shared token**, checked in constant time. Odoo's native webhook
  automation (Enterprise 17+) posts to a URL with no custom code, so the token
  travels in the URL query (``?token=...``) or the ``X-Webhook-Token`` header.
  HMAC signing was dropped: it protected payload integrity, which is moot when
  the payload is a pointer the bot re-fetches authoritatively.
* **Optional IP allowlist** (off by default) acts as a cheap pre-filter, using
  the real client IP behind Cloudflare. It's refreshed from the Odoo host on
  each outbound call — see ``config.record_odoo_ips``.
"""

from __future__ import annotations

import hmac
import json

from aiohttp import web

import config
from botlog import log

# One route per model; the route determines the model, so the body only needs _id.
SUPPORTED_MODELS = ("res.partner", "hr.attendance", "account.analytic.line")


def _real_client_ip(request: web.Request) -> str | None:
    """Real origin IP, accounting for Cloudflare / reverse proxies."""
    cf = request.headers.get("CF-Connecting-IP")
    if cf:
        return cf.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote


def make_app(bot) -> web.Application:
    def make_handler(model: str):
        async def handle(request: web.Request):
            ip = _real_client_ip(request)

            # 1. Optional IP allowlist pre-filter (cheap load-shedding).
            if config.ip_allowlist_enabled():
                allow = config.get_ip_allowlist()
                if allow and ip not in allow:
                    log.warning("[Webhook] Rejected %s: IP not in allowlist.", ip)
                    return web.Response(status=403, text="Forbidden")

            # 2. Shared-token auth (constant-time).
            token = config.get_webhook_token()
            if not token:
                log.warning("[Webhook] Rejected: WEBHOOK_TOKEN not configured.")
                return web.Response(status=503, text="Webhook not configured")
            provided = request.query.get("token") or request.headers.get("X-Webhook-Token", "")
            if not provided or not hmac.compare_digest(provided, token):
                log.warning("[Webhook] Rejected %s: bad/missing token.", ip)
                return web.Response(status=401, text="Invalid token")

            # 3. Parse the pointer. The ONLY thing we trust from the body is an int id.
            if "application/json" not in request.headers.get("Content-Type", ""):
                return web.Response(status=400, text="Content-Type must be application/json")
            raw = (await request.read()).decode() or "{}"
            try:
                body = json.loads(raw)
                record_id = int(body.get("_id", body.get("id")))
            except (json.JSONDecodeError, TypeError, ValueError):
                log.warning("[Webhook] Rejected %s: missing/invalid integer _id.", ip)
                return web.Response(status=400, text="Missing or invalid integer _id")
            action = body.get("_action")
            write_uid = body.get("write_uid")
            write_uid = int(write_uid) if isinstance(write_uid, int) else None

            # 4. Enqueue the pull for this route's (trusted) model. Return fast.
            cog = bot.get_cog("TimeTracking")
            if cog is None:
                return web.Response(status=503, text="TimeTracking cog not loaded")
            try:
                await cog.enqueue_inbound(model, record_id, action, write_uid)
            except Exception as e:  # noqa: BLE001
                log.exception("[Webhook] Handler error from %s: %s", ip, e)
                return web.Response(status=500, text="Internal server error")
            return web.Response(status=200, text="ok")

        return handle

    app = web.Application()
    for model in SUPPORTED_MODELS:
        app.router.add_post(f"/webhook/odoo/{model}", make_handler(model))
    return app


async def run_webserver(bot):
    """Start the aiohttp webhook server."""
    import os

    app = make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("WEBHOOK_PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("[Webhook] Server started on port %s. Routes: %s",
             port, ", ".join(f"/webhook/odoo/{m}" for m in SUPPORTED_MODELS))
