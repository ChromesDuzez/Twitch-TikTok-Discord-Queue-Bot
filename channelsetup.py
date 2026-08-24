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

import asyncio
import sys

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
        return await _disambiguate(base, name, matches)
    return None


async def _disambiguate(base: str, name: str, matches):
    """Ask (console, TTY only) which channel to use when several share a name.
    Runs the blocking input() in an executor so the event loop keeps ticking, and
    falls back to the first match when there's no interactive console."""
    if not sys.stdin.isatty():
        log.warning("[Setup] %d channels named #%s and no console to pick; using the first "
                    "(id %s). Set %s to choose.", len(matches), name, matches[0].id, base)
        return matches[0]
    listing = "\n".join(f"  [{i + 1}] #{c.name} (id {c.id})" for i, c in enumerate(matches))
    prompt = (f"[Setup] Multiple channels named #{name}:\n{listing}\n"
              f"Which one should the bot use? [1-{len(matches)}]: ")
    try:
        ans = await asyncio.get_event_loop().run_in_executor(None, input, prompt)
        idx = int(ans.strip()) - 1
        if 0 <= idx < len(matches):
            log.info("[Setup] Using #%s id %s for the duplicate name.", name, matches[idx].id)
            return matches[idx]
        log.warning("[Setup] Choice out of range; using the first #%s.", name)
    except Exception as e:  # noqa: BLE001
        log.warning("[Setup] Couldn't read a choice (%s); using the first #%s.", e, name)
    return matches[0]


async def resolve_channels(guild) -> bool:
    """Resolve (and in test mode create) the managed channels for ``guild`` and
    apply the prod/test policy. Returns False if the bot must halt (a required
    channel is missing in production)."""
    testing = config.is_testing()
    resolved: dict[str, object] = {}

    # Pass 1: resolve the channels that already exist (by id, then exact name).
    for base, name, _required in MANAGED:
        resolved[base] = await _find_channel(guild, base, name)

    # New test channels join the category the existing managed channels live in
    # (e.g. an "Administration" category) instead of being dumped at the top level.
    category = None
    for base, _name, _req in MANAGED:
        ch = resolved[base]
        if ch is not None and getattr(ch, "category", None) is not None:
            category = ch.category
            break

    # Pass 2: create any still-missing channels (test mode only), under that category.
    for base, name, _required in MANAGED:
        if resolved[base] is None and testing:
            try:
                resolved[base] = await guild.create_text_channel(name, category=category)
                log.warning("[Setup] Created missing test channel #%s (%s)%s.", name,
                            resolved[base].id, f" under '{category.name}'" if category else "")
            except Exception as e:  # noqa: BLE001
                log.error("[Setup] Could not create test channel #%s: %s", name, e)

    # Persist every resolved id (never raises; degrades to session-only on failure).
    for base, _name, _req in MANAGED:
        if resolved[base] is not None:
            config.persist_channel_id(base, resolved[base].id)

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
