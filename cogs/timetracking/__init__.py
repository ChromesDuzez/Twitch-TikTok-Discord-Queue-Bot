"""Timetracking cog package.

Discord punch-clock system backed by an authoritative SQLite store, with
best-effort two-way Odoo sync. ``load_extension('cogs.timetracking')`` calls
:func:`setup`.
"""

from .cog import TimeTracking


def setup(bot):
    bot.add_cog(TimeTracking(bot))
