"""Runtime configuration: .env bootstrap, webhook token, and IP allowlist.

Centralizes the bits of config that the bot generates or mutates at runtime and
persists back to the ``.env`` file:

* **First-run bootstrap** — if there is no ``.env``, create one from
  ``.env.example`` so the bot can start.
* **Webhook token** — a shared secret the inbound webhook checks. Auto-generated
  on first startup if missing; regenerable via a slash command. The token value
  is never sent to Discord.
* **IP allowlist** — optional defense-in-depth pre-filter for the webhook. It is
  refreshed with the Odoo host's resolved IP(s) every time the bot calls Odoo,
  and persisted to ``.env``. Toggle on/off via a slash command.

All writes go through python-dotenv's ``set_key`` under a lock and also update
``os.environ`` so changes take effect immediately without a restart.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import threading
from datetime import datetime

from dotenv import dotenv_values, set_key, unset_key

from botlog import log

ENV_PATH = os.path.join(os.getcwd(), ".env")
EXAMPLE_PATH = os.path.join(os.getcwd(), ".env.example")

TOKEN_KEY = "WEBHOOK_TOKEN"
ALLOWLIST_KEY = "WEBHOOK_IP_ALLOWLIST"
ALLOWLIST_ENABLED_KEY = "WEBHOOK_IP_ALLOWLIST_ENABLED"
ENV_VERSION_KEY = "ENV_VERSION"

# Bump when adding an env migration below. New *additions* are picked up
# automatically from .env.example; migrations here handle renames/removals.
ENV_TARGET_VERSION = 1

_write_lock = threading.Lock()


def _set_env(key: str, value: str):
    """Persist key=value to .env and to the live process environment."""
    with _write_lock:
        if not os.path.exists(ENV_PATH):
            open(ENV_PATH, "a").close()
        set_key(ENV_PATH, key, value, quote_mode="never")
        os.environ[key] = value


def _remove_env(key: str):
    """Delete a key from .env and the live environment."""
    with _write_lock:
        if os.path.exists(ENV_PATH):
            unset_key(ENV_PATH, key)
    os.environ.pop(key, None)


def _rename_env(old: str, new: str):
    """Move a key's value from an old name to a new one (preserving the value)."""
    value = os.getenv(old)
    if value is not None:
        _set_env(new, value)
    _remove_env(old)


# Ordered env migrations. Each version lists rename/remove operations to run.
# Key ADDITIONS are handled automatically by syncing from .env.example, so they
# don't need an entry here.
ENV_MIGRATIONS = {
    # v1: HMAC signing was replaced by the auto-generated WEBHOOK_TOKEN.
    1: [lambda: _remove_env("ODOO_WEBHOOK_SECRET")],
}


def _backup_env() -> str | None:
    if not os.path.exists(ENV_PATH):
        return None
    dest = f"{ENV_PATH}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(ENV_PATH, dest)
    log.info("[Config] Backed up .env to %s", dest)
    return dest


def migrate_env():
    """Upgrade the .env to the current version, like the database does.

    Applies rename/remove migrations, then adds any new keys present in
    .env.example (preserving the user's existing values), and stamps
    ENV_VERSION. Backs the file up first. No-op when already current.
    """
    try:
        current = int(os.getenv(ENV_VERSION_KEY, "0") or 0)
    except ValueError:
        current = 0
    if current >= ENV_TARGET_VERSION:
        return

    _backup_env()

    # 1. Renames / removals for each version step.
    for version in range(current + 1, ENV_TARGET_VERSION + 1):
        for op in ENV_MIGRATIONS.get(version, []):
            op()

    # 2. Additions: any key in the example that's missing gets its default.
    if os.path.exists(EXAMPLE_PATH):
        example = dotenv_values(EXAMPLE_PATH)
        existing = dotenv_values(ENV_PATH) if os.path.exists(ENV_PATH) else {}
        for key, default in example.items():
            if key == ENV_VERSION_KEY:
                continue
            if key not in existing:
                _set_env(key, default or "")
                log.info("[Config] Added new .env setting: %s", key)

    _set_env(ENV_VERSION_KEY, str(ENV_TARGET_VERSION))
    log.warning("[Config] Upgraded .env from v%s to v%s (backup saved).", current, ENV_TARGET_VERSION)


# ---- first-run bootstrap ---------------------------------------------------

def ensure_env_file() -> bool:
    """Create .env from .env.example if it doesn't exist. Returns True if created."""
    if os.path.exists(ENV_PATH):
        return False
    if os.path.exists(EXAMPLE_PATH):
        shutil.copy(EXAMPLE_PATH, ENV_PATH)
        log.warning("[Config] No .env found — created one from .env.example. Fill in your settings.")
    else:
        open(ENV_PATH, "a").close()
        log.warning("[Config] No .env or .env.example found — created an empty .env.")
    return True


# ---- webhook token ---------------------------------------------------------

def get_webhook_token() -> str | None:
    return os.getenv(TOKEN_KEY) or None


def ensure_webhook_token() -> str:
    """Generate + persist a token on first startup if one isn't set."""
    token = get_webhook_token()
    if not token:
        token = secrets.token_urlsafe(32)
        _set_env(TOKEN_KEY, token)
        log.warning("[Config] Generated a new %s and saved it to .env.", TOKEN_KEY)
    return token


def regenerate_webhook_token() -> None:
    """Rotate the webhook token. The new value is NOT returned/logged to Discord."""
    _set_env(TOKEN_KEY, secrets.token_urlsafe(32))
    log.warning("[Config] %s was regenerated. Update your Odoo automation URLs.", TOKEN_KEY)


# ---- IP allowlist ----------------------------------------------------------

def ip_allowlist_enabled() -> bool:
    return os.getenv(ALLOWLIST_ENABLED_KEY, "false").strip().lower() in ("1", "true", "yes")


def set_ip_allowlist_enabled(enabled: bool):
    _set_env(ALLOWLIST_ENABLED_KEY, "true" if enabled else "false")


def get_ip_allowlist() -> set[str]:
    raw = os.getenv(ALLOWLIST_KEY, "")
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def record_odoo_ips(odoo_url: str):
    """Resolve the Odoo host to its IP(s) and merge into the allowlist.

    Called on every outbound Odoo call so the allowlist tracks the IPs we
    actually reach out to. Only writes when the set changes (rare).
    """
    if not odoo_url:
        return
    try:
        from urllib.parse import urlparse

        host = urlparse(odoo_url).hostname
        if not host:
            return
        _, _, ips = socket.gethostbyname_ex(host)
    except Exception as e:  # noqa: BLE001 - best effort; never break an API call
        log.debug("[Config] Could not resolve Odoo host for allowlist: %s", e)
        return
    current = get_ip_allowlist()
    updated = current | set(ips)
    if updated != current:
        _set_env(ALLOWLIST_KEY, ",".join(sorted(updated)))
        log.info("[Config] Webhook IP allowlist refreshed: %s", ",".join(sorted(updated)))
