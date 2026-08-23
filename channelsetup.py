"""Startup resolution of the bot's managed Discord channels.

Each managed channel is resolved by, in order:
  1. its configured id (``BOT_LOG_ID`` etc., already overlaid with any
     ``TESTING_*`` value in test mode),
  2. an exact **name** match in the guild.

Then a mode-specific policy is applied:
  * **TEST** — if still missing, the channel is **created** (it's a throwaway
    test server). Multiple same-name matches prompt later (Phase 2); for now the
    first is used with a warning.
  * **PROD** — never create. A **required** channel (the general log + the
    timecard log) missing means the bot **halts** with a clear message. An
    optional one (admin, reports) missing **warns** and falls back to the
    timecard-log channel.

Resolved ids are written to the live env and persisted (``TESTING_*`` in test
mode, the base key in prod) so the rest of the bot reads them normally.
"""

from __future__ import annotations

import config
from botlog import log

# (base env key, exact channel name, required-for-operation)
MANAGED = (
    ("BOT_LOG_ID", "log", True),
    ("TIMECARD_LOG_ID", "timecard-log", True),
    ("TIMECARD_ADMIN_CHANNEL_ID", "timecard-admin", False),
    ("TIMECARD_REPORTS_CHANNEL_ID", "timecard-reports", False),
)


def _configured_id(base: str):
    import os
    raw = os.getenv(base)
    return int(raw) if raw and raw.isdigit() else None


async def _find_channel(guild, base: str, name: str):
    """Resolve one managed channel by configured id, then exact name. Returns a
    channel or None (no creation here)."""
    cid = _configured_id(base)
    if cid:
        ch = guild.get_channel(cid)
        if ch is not None:
            return ch
    matches = [c for c in guild.text_channels if c.name == name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # Phase 2 will prompt to disambiguate; for now use the first, loudly.
        log.warning("[Setup] %d channels named #%s; using the first (id %s). "
                    "Set %s explicitly to choose.", len(matches), name, matches[0].id, base)
        return matches[0]
    return None


async def resolve_channels(guild) -> bool:
    """Resolve (and in test mode create) the managed channels for ``guild`` and
    apply the prod/test policy. Returns False if the bot must halt (a required
    channel is missing in production)."""
    testing = config.is_testing()
    resolved: dict[str, object] = {}

    for base, name, _required in MANAGED:
        ch = await _find_channel(guild, base, name)
        if ch is None and testing:
            try:
                ch = await guild.create_text_channel(name)
                log.warning("[Setup] Created missing test channel #%s (%s).", name, ch.id)
            except Exception as e:  # noqa: BLE001
                log.error("[Setup] Could not create test channel #%s: %s", name, e)
        if ch is not None:
            config.persist_channel_id(base, ch.id)
        resolved[base] = ch

    # Production: required channels must exist, or we can't operate safely.
    missing_required = [name for base, name, req in MANAGED if req and resolved[base] is None]
    if missing_required and not testing:
        log.critical(
            "[Setup] Missing REQUIRED channel(s): %s. Create channel(s) with those "
            "names in the guild, or set their ids in .env, then restart. Not starting.",
            ", ".join(missing_required),
        )
        return False

    # Optional channels fall back to the timecard-log channel when unset.
    tlog = resolved.get("TIMECARD_LOG_ID")
    if tlog is not None:
        import os
        for base, name, req in MANAGED:
            if not req and resolved[base] is None:
                os.environ[base] = str(tlog.id)
                log.warning("[Setup] #%s not configured; defaulting %s to #%s.",
                            name, base, tlog.name)
    return True
