"""Permission helpers for the timetracking cog.

Consolidates the old ``hasPerms`` logic and fixes the env-var mismatch where
some call sites read ``TIMECARD_ADMIN_ROLES`` while the setting is actually
``TIMECARD_ADMIN_ROLE``.
"""

from __future__ import annotations

import os

import discord
from discord.ext import commands

# Default roles (by env-var name) that are allowed to operate a clock.
CLOCK_ROLES = ("TIMECARD_ADMIN_ROLE", "TIMECARD_TIMECLOCK_ROLE_ID")


def _resolve_role_id(role) -> int | None:
    """Accept either a literal role id (int) or an env-var name (str)."""
    if isinstance(role, int):
        return role
    if isinstance(role, str):
        value = os.getenv(role)
        if value:
            try:
                return int(value)
            except ValueError:
                return None
    return None


def has_perms(
    user: discord.Member | discord.User,
    intended_user: discord.User | None = None,
    accepted_roles=("TIMECARD_ADMIN_ROLE",),
) -> bool:
    """True if ``user`` may act on ``intended_user``'s clock.

    Grants access when: the user IS the intended user, the user is a guild
    administrator, or the user holds one of ``accepted_roles``.
    """
    if user is None:
        return False

    if intended_user is not None and user.id == intended_user.id:
        return True

    roles = getattr(user, "roles", [])
    if any(getattr(role.permissions, "administrator", False) for role in roles):
        return True

    for role in accepted_roles:
        role_id = _resolve_role_id(role)
        if role_id is not None and discord.utils.get(roles, id=role_id):
            return True

    return False


def is_timecard_admin_member(user) -> bool:
    """True if ``user`` may run timecard admin commands: a guild Administrator
    (owner, admin role, etc.) or a holder of ``TIMECARD_ADMIN_ROLE``."""
    perms = getattr(user, "guild_permissions", None)
    if perms is not None and perms.administrator:
        return True
    return has_perms(user, accepted_roles=("TIMECARD_ADMIN_ROLE",))


def is_timecard_admin():
    """Slash-command check granting guild Administrators and TIMECARD_ADMIN_ROLE
    holders. On failure py-cord raises CheckFailure -> the global handler replies
    'You don't have permission to use this command.'"""
    async def predicate(ctx):
        return is_timecard_admin_member(ctx.author)
    return commands.check(predicate)
