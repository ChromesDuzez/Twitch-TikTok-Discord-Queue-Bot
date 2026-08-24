"""Native testing helpers (loaded only when TESTING=true).

Run a SEPARATE bot application/token pointed at a test guild + test Odoo DB.
These commands manage the isolated test environment:

* ``/testimport`` — attach an exported prod db (``/timecardexportdb``); it's
  migrated, **sanitized** (prod channel/message ids + all Odoo ids + sync queues
  stripped), swapped in as the test database, and a clock is built for every
  employee who had one in prod.
* ``/testreset``  — wipe the test db to a bare baseline.
* ``/testsetup``  — (re)build the ``timecards`` category and a clock channel for
  every employee in the test db that doesn't already have one.
* ``/testteardown`` — delete the per-employee clock channels + the ``timecards``
  category and clear the clock pointers.

The managed log/admin/report channels are resolved/created at startup
(``channelsetup``), not here. Every command is double-guarded: ``TESTING`` must be
true AND the command must run in ``TESTING_GUILD_ID`` — so none of this can ever
fire against production.
"""

import os
import tempfile

import discord
from discord.ext import commands

import config
import testboot
from botlog import log
from cogs.timetracking.db import Database
from cogs.timetracking.modals import Confirm
from cogs.timetracking.perms import is_timecard_admin


class Testing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- guards ------------------------------------------------------------

    def _blocked(self, ctx) -> str | None:
        if not config.is_testing():
            return "TESTING is not enabled on this bot."
        tg = config.testing_guild_id()
        if ctx.guild is None or (tg and ctx.guild.id != tg):
            return "This command can only be run in the configured test guild."
        return None

    def _tt(self):
        return self.bot.get_cog("TimeTracking")

    # ---- import (clone + sanitize a prod snapshot) -------------------------

    @discord.slash_command(name="testimport", description="[TEST] Import + sanitize an exported prod database.")
    @is_timecard_admin()
    async def testimport(
        self, ctx: discord.ApplicationContext,
        snapshot: discord.Option(discord.Attachment, description="Exported prod timetracker .db file"),  # type: ignore
    ):
        block = self._blocked(ctx)
        if block:
            return await ctx.respond(block, ephemeral=True)
        tt = self._tt()
        if tt is None:
            return await ctx.respond("Timetracking cog isn't loaded (ENABLE_TIMETRACKING=true?).", ephemeral=True)
        await ctx.defer(ephemeral=True)

        tmp = os.path.join(tempfile.gettempdir(), f"testimport_{snapshot.id}.db")
        await snapshot.save(tmp)
        try:
            active, employees, punches = await testboot.import_source_into_test(tt, tmp)
        except Exception as e:  # noqa: BLE001
            log.exception("[Test] Import failed: %s", e)
            return await ctx.respond(f"Import failed (is it a valid timetracker db?): {e}", ephemeral=True)
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(tmp + suffix):
                    try:
                        os.remove(tmp + suffix)
                    except OSError:
                        pass

        made, skipped = await testboot.build_employee_clocks(tt, ctx.guild, active)
        log.warning("[Test] Imported prod snapshot by %s.", ctx.author)
        skip_note = f" Skipped clocks: {', '.join(skipped)}." if skipped else ""
        await ctx.respond(
            f"✅ Imported + sanitized: **{employees}** employees, **{punches}** punches "
            f"(Odoo/channel ids + sync queues stripped). Built **{made}** clock(s) under "
            f"the `timecards` category.{skip_note}",
            ephemeral=True,
        )

    # ---- reset to a bare baseline ------------------------------------------

    @discord.slash_command(name="testreset", description="[TEST] Wipe the test database to a bare baseline.")
    @is_timecard_admin()
    async def testreset(self, ctx: discord.ApplicationContext):
        block = self._blocked(ctx)
        if block:
            return await ctx.respond(block, ephemeral=True)
        tt = self._tt()
        if tt is None:
            return await ctx.respond("Timetracking cog isn't loaded.", ephemeral=True)
        confirm = Confirm(user=ctx.user, timeout=60)
        await ctx.respond("⚠️ This wipes the **test** database to a clean baseline. Proceed?",
                          view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value:
            return await ctx.followup.send("Cancelled.", ephemeral=True)
        await tt.wipe_db()
        log.warning("[Test] Test database wiped to baseline by %s.", ctx.author)
        await ctx.followup.send("✅ Test database wiped to a bare baseline.", ephemeral=True)

    # ---- setup (build the per-employee clocks) -----------------------------

    @discord.slash_command(name="testsetup", description="[TEST] Build a clock channel for every employee without one.")
    @is_timecard_admin()
    async def testsetup(self, ctx: discord.ApplicationContext):
        block = self._blocked(ctx)
        if block:
            return await ctx.respond(block, ephemeral=True)
        tt = self._tt()
        if tt is None:
            return await ctx.respond("Timetracking cog isn't loaded.", ephemeral=True)
        await ctx.defer(ephemeral=True)
        db = await tt._ensure_db()
        ids = [r["id"] for r in await db.fetchall("SELECT id FROM employee WHERE archived = 0 OR archived IS NULL")]
        made, skipped = await testboot.build_employee_clocks(tt, ctx.guild, ids, skip_existing=True)
        skip_note = f"\nSkipped: {', '.join(skipped)}" if skipped else ""
        await ctx.respond(
            f"✅ Built **{made}** clock channel(s) under `timecards` "
            f"({len(ids) - made - len(skipped)} already had one).{skip_note}",
            ephemeral=True,
        )

    # ---- teardown ----------------------------------------------------------

    @discord.slash_command(name="testteardown", description="[TEST] Delete the per-employee clock channels + timecards category.")
    @is_timecard_admin()
    async def testteardown(self, ctx: discord.ApplicationContext):
        block = self._blocked(ctx)
        if block:
            return await ctx.respond(block, ephemeral=True)
        tt = self._tt()
        if tt is None:
            return await ctx.respond("Timetracking cog isn't loaded.", ephemeral=True)
        confirm = Confirm(user=ctx.user, timeout=60)
        await ctx.respond("⚠️ Delete all per-employee clock channels and the `timecards` category?",
                          view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value:
            return await ctx.followup.send("Cancelled.", ephemeral=True)
        guild = ctx.guild
        db = await tt._ensure_db()

        # Delete each employee's clock channel (the whole channel, not just the message).
        deleted = 0
        for row in await db.fetchall(
            "SELECT DISTINCT clockChannelId FROM employee WHERE clockChannelId IS NOT NULL"
        ):
            channel = guild.get_channel(int(row["clockChannelId"]))
            if channel is not None:
                try:
                    await channel.delete()
                    deleted += 1
                except Exception:  # noqa: BLE001
                    pass
        await db.execute("UPDATE employee SET clockChannelId = NULL, clockMessageId = NULL")

        # Delete the timecards category (now empty) and forget its id.
        cat_id = os.getenv("TESTING_TIMECARD_CATEGORY_ID")
        category = guild.get_channel(int(cat_id)) if cat_id and cat_id.isdigit() else None
        if category is not None:
            try:
                await category.delete()
            except Exception:  # noqa: BLE001
                pass
        config.set_env("TESTING_TIMECARD_CATEGORY_ID", "")

        log.warning("[Test] Teardown by %s: removed %d clock channel(s) + the category.", ctx.author, deleted)
        await ctx.followup.send(
            f"✅ Teardown complete: {deleted} clock channel(s) + the `timecards` category removed.",
            ephemeral=True,
        )


def setup(bot):
    bot.add_cog(Testing(bot))
