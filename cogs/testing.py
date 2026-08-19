"""Native testing helpers (loaded only when TESTING=true).

Run a SEPARATE bot application/token pointed at a test guild + test Odoo DB.
These commands let that test bot clone your real data into a clean, isolated
environment:

* ``/testimport`` — attach an exported prod db (``/timecardexportdb``); it's
  migrated, **sanitized** (prod channel/message ids + all Odoo ids + sync queues
  stripped), and swapped in as the test database.
* ``/testreset``  — wipe the test db to a bare baseline.
* ``/testsetup``  — create the managed channels (if missing) and (re)build clock
  messages for every employee who is also a member of the test guild.
* ``/testteardown`` — delete the clocks + bot-created channels.

Every command is double-guarded: ``TESTING`` must be true AND the command must
run in ``TESTING_GUILD_ID`` — so none of this can ever fire against production.
The test bot also uses a separate ``timetracker.test`` db file.
"""

import os
import shutil
import tempfile

import discord
from discord.ext import commands

import config
from botlog import log
from cogs.timetracking.db import TARGET_VERSION, Database, db_dir, db_filename
from cogs.timetracking.modals import Confirm

# env var -> default channel name the test bot manages
MANAGED_CHANNELS = {
    "BOT_LOG_ID": "bot-logs",
    "TIMECARD_LOG_ID": "timecard-logs",
    "TIMECARD_ADMIN_CHANNEL_ID": "timecard-approvals",
    "TIMECARD_REPORTS_CHANNEL_ID": "timecard-reports",
    "TESTING_CLOCK_CHANNEL_ID": "time-clocks",
}
MANAGED_LIST_KEY = "TESTING_MANAGED_CHANNELS"

SANITIZE_SQL = [
    "UPDATE employee   SET clockChannelId = NULL, clockMessageId = NULL, odooId = NULL",
    "UPDATE punch_clock SET checkChannelId = NULL, checkMessageId = NULL, odooId = NULL",
    "UPDATE customer    SET odooId = NULL",
    "UPDATE work_time   SET odooId = NULL, odooTaskId = NULL, odooProjectId = NULL",
    "DELETE FROM odoo_outbox",
    "DELETE FROM odoo_inbox",
]


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
    @commands.has_permissions(administrator=True)
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
            sdb = await Database(tmp).setup()  # migrate the snapshot up to current schema
            employees = (await sdb.fetchone("SELECT count(*) c FROM employee"))["c"]
            punches = (await sdb.fetchone("SELECT count(*) c FROM punch_clock"))["c"]
            for stmt in SANITIZE_SQL:
                await sdb.execute(stmt)
            await sdb.close()
        except Exception as e:  # noqa: BLE001
            log.exception("[Test] Import failed: %s", e)
            return await ctx.respond(f"Import failed (is it a valid timetracker db?): {e}", ephemeral=True)

        target = os.path.join(db_dir(os.getcwd()), db_filename(TARGET_VERSION, "timetracker.test"))
        await tt.close_db()
        os.makedirs(db_dir(os.getcwd()), exist_ok=True)
        for suffix in ("", "-wal", "-shm", "-journal"):  # clear the old test db + sidecars
            if os.path.exists(target + suffix):
                os.remove(target + suffix)
        shutil.move(tmp, target)
        await tt._ensure_db()
        log.warning("[Test] Imported prod snapshot into %s by %s.", os.path.basename(target), ctx.author)
        await ctx.respond(
            f"✅ Imported + sanitized: **{employees}** employees, **{punches}** punches. "
            "Prod channel/message ids, Odoo ids, and sync queues were stripped. "
            "Run `/testsetup` to build clocks.",
            ephemeral=True,
        )

    # ---- reset to a bare baseline ------------------------------------------

    @discord.slash_command(name="testreset", description="[TEST] Wipe the test database to a bare baseline.")
    @commands.has_permissions(administrator=True)
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

    # ---- setup (channels + clocks) -----------------------------------------

    @discord.slash_command(name="testsetup", description="[TEST] Create managed channels + build clocks for present employees.")
    @commands.has_permissions(administrator=True)
    async def testsetup(self, ctx: discord.ApplicationContext):
        block = self._blocked(ctx)
        if block:
            return await ctx.respond(block, ephemeral=True)
        tt = self._tt()
        if tt is None:
            return await ctx.respond("Timetracking cog isn't loaded.", ephemeral=True)
        await ctx.defer(ephemeral=True)
        guild = ctx.guild
        db = await tt._ensure_db()

        # 1. Ensure the managed channels exist; persist ids; track what we created.
        managed = {c for c in os.getenv(MANAGED_LIST_KEY, "").split(",") if c}
        created_names = []
        for env_key, name in MANAGED_CHANNELS.items():
            cid = os.getenv(env_key)
            channel = guild.get_channel(int(cid)) if cid and cid.isdigit() else None
            if channel is None:
                channel = await guild.create_text_channel(name)
                config.set_env(env_key, str(channel.id))
                managed.add(str(channel.id))
                created_names.append(name)
        config.set_env(MANAGED_LIST_KEY, ",".join(sorted(managed)))

        # 2. Build clocks for employees who are members of the test guild.
        clock_channel = guild.get_channel(int(os.getenv("TESTING_CLOCK_CHANNEL_ID")))
        made, skipped = 0, []
        for row in await db.fetchall("SELECT id, name FROM employee"):
            if guild.get_member(row["id"]) is None:
                skipped.append(row["name"])
                continue
            try:
                await tt.make_clock(row["id"], clock_channel)
                made += 1
            except Exception as e:  # noqa: BLE001
                log.warning("[Test] Could not build clock for %s: %s", row["name"], e)
                skipped.append(row["name"])

        skip_note = f"\nSkipped (not in this guild): {', '.join(skipped)}" if skipped else ""
        created_note = f"\nCreated channels: {', '.join(created_names)}" if created_names else ""
        await ctx.respond(
            f"✅ Built **{made}** clocks in #{clock_channel.name}.{created_note}{skip_note}",
            ephemeral=True,
        )

    # ---- teardown ----------------------------------------------------------

    @discord.slash_command(name="testteardown", description="[TEST] Delete clocks + bot-created channels.")
    @commands.has_permissions(administrator=True)
    async def testteardown(self, ctx: discord.ApplicationContext):
        block = self._blocked(ctx)
        if block:
            return await ctx.respond(block, ephemeral=True)
        tt = self._tt()
        if tt is None:
            return await ctx.respond("Timetracking cog isn't loaded.", ephemeral=True)
        confirm = Confirm(user=ctx.user, timeout=60)
        await ctx.respond("⚠️ Delete all test clocks and the channels this bot created?",
                          view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value:
            return await ctx.followup.send("Cancelled.", ephemeral=True)
        guild = ctx.guild
        db = await tt._ensure_db()

        # delete clock messages
        for row in await db.fetchall(
            "SELECT clockChannelId, clockMessageId FROM employee "
            "WHERE clockChannelId IS NOT NULL AND clockMessageId IS NOT NULL"
        ):
            try:
                msg = await tt.obtain_message(row["clockChannelId"], row["clockMessageId"])
                await msg.delete()
            except Exception:  # noqa: BLE001
                pass
        await db.execute("UPDATE employee SET clockChannelId = NULL, clockMessageId = NULL")

        # delete only the channels this bot created
        managed = {c for c in os.getenv(MANAGED_LIST_KEY, "").split(",") if c}
        deleted = 0
        for env_key in MANAGED_CHANNELS:
            cid = os.getenv(env_key)
            if cid and cid in managed:
                channel = guild.get_channel(int(cid))
                if channel is not None:
                    try:
                        await channel.delete()
                        deleted += 1
                    except Exception:  # noqa: BLE001
                        pass
                config.set_env(env_key, "")
        config.set_env(MANAGED_LIST_KEY, "")
        log.warning("[Test] Teardown by %s: removed clocks + %s channels.", ctx.author, deleted)
        await ctx.followup.send(f"✅ Teardown complete: clocks removed, {deleted} channels deleted.", ephemeral=True)


def setup(bot):
    bot.add_cog(Testing(bot))
