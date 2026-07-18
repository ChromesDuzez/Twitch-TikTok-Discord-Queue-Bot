"""Inbound webhook server for Odoo -> Discord updates.

This replaces the previous "verify Odoo connectivity" gate, which was not real
authentication: it only proved that *the bot* could reach Odoo, not that the
*request* came from Odoo, so anyone able to reach the port could drive message
edits.

Instead we require an **HMAC-SHA256 signature** over the raw request body using
a shared secret (``ODOO_WEBHOOK_SECRET``), plus a timestamp to bound replay.
Odoo signs the payload with the same secret when it POSTs. Requests without a
valid signature are rejected with 401.

Payload validation is limited to types/shape (SQL is fully parameterized in the
cog, so the old character whitelist -- which rejected legitimate names with
apostrophes, etc. -- is gone).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time

from aiohttp import web

# Reject requests whose timestamp is older/newer than this (seconds).
REPLAY_WINDOW = 300


def _valid_signature(secret: str, timestamp: str, raw_body: bytes, provided: str) -> bool:
    """Constant-time check of ``HMAC(secret, "<timestamp>.<body>")``."""
    if not provided:
        return False
    signed = f"{timestamp}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    # Accept an optional "sha256=" prefix for convenience.
    provided = provided.split("=", 1)[1] if provided.startswith("sha256=") else provided
    return hmac.compare_digest(expected, provided)


def _validate_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a JSON object")
    channel_id = payload.get("channel_id")
    message_id = payload.get("message_id")
    if channel_id is None or message_id is None:
        raise ValueError("Missing required fields: channel_id, message_id")
    int(channel_id)  # type checks
    int(message_id)
    action = payload.get("action", "sync_database")
    allowed = {"update_content", "approve_punch", "update_clock_view", "sync_database"}
    if action not in allowed:
        raise ValueError(f"Unknown action: {action}")
    if payload.get("punch_id") is not None:
        int(payload["punch_id"])
    if payload.get("data") is not None and not isinstance(payload["data"], dict):
        raise ValueError("'data' must be an object")
    return payload


def make_app(bot) -> web.Application:
    secret = os.getenv("ODOO_WEBHOOK_SECRET")

    async def handle_webhook(request: web.Request):
        client_ip = request.remote
        raw = await request.read()

        # --- authentication ---
        if not secret:
            print("[Webhook] Rejected: ODOO_WEBHOOK_SECRET is not set on the bot.")
            return web.Response(status=503, text="Webhook not configured")
        timestamp = request.headers.get("X-Odoo-Timestamp", "")
        signature = request.headers.get("X-Odoo-Signature", "")
        if not timestamp.isdigit() or abs(time.time() - int(timestamp)) > REPLAY_WINDOW:
            print(f"[Webhook] Rejected: bad/stale timestamp from {client_ip}")
            return web.Response(status=401, text="Invalid or expired timestamp")
        if not _valid_signature(secret, timestamp, raw, signature):
            print(f"[Webhook] Rejected: bad signature from {client_ip}")
            return web.Response(status=401, text="Invalid signature")

        # --- content type + parse ---
        if "application/json" not in request.headers.get("Content-Type", ""):
            return web.Response(status=400, text="Content-Type must be application/json")
        try:
            payload = _validate_payload(json.loads(raw.decode() or "{}"))
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")
        except ValueError as e:
            return web.Response(status=400, text=f"Validation error: {e}")

        cog = bot.get_cog("TimeTracking")
        if cog is None:
            return web.Response(status=503, text="TimeTracking cog not loaded")
        try:
            await cog.handle_odoo_webhook(payload)
        except Exception as e:  # noqa: BLE001
            print(f"[Webhook] Handler error from {client_ip}: {e}")
            return web.Response(status=500, text="Internal server error")
        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_post("/webhook/odoo-timetracking", handle_webhook)
    app.router.add_post("/webhook/timetracking", handle_webhook)
    return app


async def run_webserver(bot):
    """Start the aiohttp webhook server."""
    app = make_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("WEBHOOK_PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Webhook server started on port {port}")
