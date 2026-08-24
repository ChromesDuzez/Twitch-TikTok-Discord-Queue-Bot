"""TimeTracking cog: slash commands, view re-attachment, inbound webhook apply.

Behaviour matches the original cog; the internals now use the async db layer,
render clock messages from DB state (never from embed text), and enqueue Odoo
sync jobs after local commits.
"""

import asyncio
import os
from datetime import datetime

import discord
from discord.ext import commands

import config
from botlog import log, timecard_log
from .db import (
    Database, RELEASE_VERSION, TARGET_VERSION, archive_stale_dbs, backup_database,
    db_archive_dir, db_dir, move_db_file, resolve_db_path,
)
from .modals import Confirm
from .odoo import inbox, sync
from .odoo.client import OdooClient
from .perms import has_perms, is_timecard_admin
from .reports import (
    autofill_incomplete_date,
    generate_timecard_report,
    get_closest_saturdays,
    get_day_of_week,
    is_saturday,
)
from .views import (
    ApprovePunch, DeleteApproval, DeletePunchFlow, TimecardWeekView,
    build_timecard_embed, delete_punch_cascade, delete_worktime_local,
    reassign_worktime, render_clock,
)

WORKTYPES = ["Construction", "Service", "Office"]

# Admin management commands: their results are shown publicly in the timecard
# admin/log channels and ephemerally (decluttered, + mirrored to the log channel)
# elsewhere. Self-service (mytimecard, refreshclock) and report/DM commands are
# intentionally excluded.
_MANAGEMENT_COMMANDS = {
    "addpunch", "editpunch", "deletepunch",
    "addworktime", "editworktime", "deleteworktime", "reassignworktime",
    "viewtimecard",
    "synccustomers", "linkcustomer", "unlinkcustomer", "unlinkedcustomers",
    "addcustomer", "editcustomer", "mergecustomers", "archivecustomer", "unarchivecustomer",
    "deletecustomer", "purgeimportedcontacts", "configureprojects", "configureroles", "configurecategories",
    "addemployee", "linkemployee", "unlinkemployee", "archiveemployee", "unarchiveemployee",
    "createclock", "deleteclock",
}


def _parse_punch_time(raw: str) -> str:
    """Accept 'YYYY-MM-DD HH:MM' or '...:SS' -> normalized 'YYYY-MM-DD HH:MM:SS'."""
    from datetime import datetime
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"'{raw}' isn't a valid date/time — use `YYYY-MM-DD HH:MM` (e.g. 2026-08-19 08:00).")


def _hours_to_minutes(hours: float) -> int:
    """Hours -> minutes rounded to the quarter hour (the schema requires % 15 == 0)."""
    minutes = int(round((hours or 0) * 4) / 4 * 60)
    if minutes < 0 or minutes > 1440:
        raise ValueError("Hours must be between 0 and 24.")
    return minutes


def _choice(name, value, fallback="(unnamed)"):
    """Build an OptionChoice with a Discord-legal name (1..100 chars). Odoo/DB
    rows occasionally have a blank display name — an empty choice name 400s the
    whole autocomplete ('Must be between 1 and 100 in length')."""
    label = (str(name).strip() if name is not None else "") or fallback
    return discord.OptionChoice(name=label[:100], value=value)


def _opt_int(raw) -> int | None:
    """Parse an id chosen from an autocomplete (a string value) to int, or None
    if blank/unparseable. Id-based options are string-typed so users can type a
    name to filter (Discord only lets you type digits into an integer option)."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


class TimeTracking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_path: str | None = None  # resolved (version-stamped) on first use
        self.db: Database | None = None
        self.db_upgraded = False  # True once an older/legacy db was migrated this run
        self.client = OdooClient(
            os.getenv("ODOO_URL"), os.getenv("ODOO_DB"),
            os.getenv("ODOO_USERNAME"), os.getenv("ODOO_API_KEY"),
        )
        self.sync: sync.SyncWorker | None = None
        self.inbox: inbox.InboxWorker | None = None
        self._lock = asyncio.Lock()
        self._odoo_employees: list | None = None  # cached hr.employee list for autocomplete
        self._odoo_customers: list | None = None  # cached res.partner (customer) list for autocomplete

    # ---- lifecycle ---------------------------------------------------------

    async def _open_db(self) -> Database:
        """Resolve, back up, upgrade, and open the DB (no background workers).

        Sets ``self.db_upgraded`` when an older/legacy db was migrated this run, so
        startup can pause for review before the bot goes live. Idempotent.
        """
        async with self._lock:
            if self.db is None:
                base = os.getcwd()
                # A test bot uses a separate db file so it can never touch prod's.
                prefix = "timetracker.test" if config.is_testing() else "timetracker"
                target, source = resolve_db_path(base, prefix)
                # A source at the SAME version is just being relocated into
                # database/ (no backup, no review); a different/legacy version is a
                # real upgrade -> back it up first and flag for the startup review.
                relocating = source is not None and os.path.basename(source) == os.path.basename(target)
                self.db_upgraded = source is not None and not relocating
                if source is not None:
                    if relocating:
                        log.info(f"[DB] Relocating {os.path.basename(source)} into {db_dir(base)}/.")
                    else:
                        backup_database(source, db_archive_dir(base))
                        log.info(
                            f"[DB] Upgrading {os.path.basename(source)} -> "
                            f"{os.path.basename(target)} (schema v{TARGET_VERSION}, release {RELEASE_VERSION})."
                        )
                    move_db_file(source, target)
                self.db_path = target
                self.db = await Database(target).setup(
                    company_name=os.getenv("COMPANY_NAME"),
                    debug=bool(os.getenv("DEBUGGING")),
                )
                # Sweep any leftover old versions / backups for this prefix into
                # database/archive/ so only the live file remains in view.
                archive_stale_dbs(base, prefix, target)
        return self.db

    async def _ensure_db(self) -> Database:
        """Open the DB (if needed) and start the Odoo sync/inbox workers."""
        await self._open_db()
        async with self._lock:
            if self.sync is None:
                self.sync = sync.SyncWorker(self.db, self.client)
                self.sync.start()
                self.inbox = inbox.InboxWorker(self)
                self.inbox.start()
        return self.db

    async def enqueue_inbound(self, model: str, odoo_id: int, action=None, write_uid=None):
        """Entry point for the webhook: queue an inbound Odoo change to reconcile."""
        await self._ensure_db()
        await inbox.enqueue_inbound(self.db, model, odoo_id, action, write_uid)

    async def enqueue_inbound_delete(self, model: str, odoo_id: int):
        """Entry point for the webhook: queue an Odoo-side deletion."""
        await self._ensure_db()
        await inbox.enqueue_inbound_delete(self.db, model, odoo_id)

    async def post_delete_approval(self, model: str, odoo_id: int, local_kind: str, local_id: int):
        """Post an admin approval for an Odoo-side deletion (Discord is truth)."""
        db = await self._ensure_db()
        # Build a human summary of what would be removed.
        if local_kind == "punch":
            p = await db.fetchone(
                "SELECT e.name, pc.punchInTime, pc.punchOutTime FROM punch_clock pc "
                "JOIN employee e ON pc.employeeID = e.id WHERE pc.id = ?", (local_id,))
            n = (await db.fetchone("SELECT count(*) c FROM work_time WHERE punchID = ?", (local_id,)))["c"]
            who = p["name"] if p else str(local_id)
            summary = (f"🗑️ Odoo deleted a **shift** for **{who}** "
                       f"({p['punchInTime'] if p else '?'} → {p['punchOutTime'] if p and p['punchOutTime'] else 'open'}, "
                       f"{n} worktime). Approve deleting it here, or reject to restore it in Odoo.")
        else:
            w = await db.fetchone(
                "SELECT wt.punchType, wt.timeSpent, c.name, e.name AS emp FROM work_time wt "
                "JOIN punch_clock pc ON wt.punchID = pc.id JOIN employee e ON pc.employeeID = e.id "
                "LEFT JOIN customer c ON wt.customerID = c.id WHERE wt.id = ?", (local_id,))
            summary = (f"🗑️ Odoo deleted a **timesheet** on **{w['emp'] if w else '?'}**'s shift "
                       f"({w['punchType'] if w else '?'}, {(w['timeSpent'] or 0)/60 if w else 0}h, "
                       f"{w['name'] if w and w['name'] else 'n/a'}). Approve removing it here, or reject to restore it in Odoo.")

        pending_id = await db.execute(
            "INSERT INTO pending_action (action, model, odoo_id, local_kind, local_id, created_at) "
            "VALUES ('delete', ?, ?, ?, ?, ?)",
            (model, odoo_id, local_kind, local_id, sync.now_local_str()),
        )
        channel = self.bot.get_channel(int(os.getenv("TIMECARD_ADMIN_CHANNEL_ID")))
        view = DeleteApproval(self, pending_id, model, odoo_id, local_kind, local_id)
        msg = await channel.send(content=summary, view=view)
        await db.execute(
            "UPDATE pending_action SET channel_id = ?, message_id = ? WHERE id = ?",
            (msg.channel.id, msg.id, pending_id),
        )

    async def clear_pending_action(self, pending_id: int):
        await self.db.execute("DELETE FROM pending_action WHERE id = ?", (pending_id,))

    async def obtain_message(self, channel_id: int, message_id: int) -> discord.Message:
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        return await channel.fetch_message(message_id)

    async def close_db(self):
        """Stop the workers and close the db connection (leaves the file)."""
        async with self._lock:
            if self.sync is not None:
                await self.sync.stop()
                self.sync = None
            if self.inbox is not None:
                await self.inbox.stop()
                self.inbox = None
            if self.db is not None:
                await self.db.close()
            self.db = None

    async def reload_db(self):
        """Close + reopen the db and restart the workers (after swapping the file)."""
        await self.close_db()
        await self._ensure_db()

    async def wipe_db(self):
        """Delete the current db file and recreate a bare baseline. Test-only."""
        await self.close_db()
        if self.db_path and os.path.exists(self.db_path):
            os.remove(self.db_path)
        # Also clear WAL sidecar files so nothing stale is reopened.
        for suffix in ("-wal", "-shm"):
            side = f"{self.db_path}{suffix}" if self.db_path else None
            if side and os.path.exists(side):
                os.remove(side)
        await self._ensure_db()

    async def make_clock(self, employee_id: int, channel) -> discord.Message:
        """Create + persist a fresh clock message for an employee in a channel.

        Shared by /createclock and the testing setup so both build clocks the
        same way. Renders from DB state (clocked-in or not).
        """
        db = await self._ensure_db()
        user_obj = await self.bot.fetch_user(employee_id)
        embed = discord.Embed(title="You are currently NOT clocked in.", color=discord.Colour.brand_red())
        embed.add_field(
            name="Wondering how to clock in?",
            value="Click the green clock-in button and watch the field turn green. "
            "To clock out, hit the red clock-out button. Simple as that!",
        )
        embed.set_footer(text=f"User: {user_obj.name}")
        message = await channel.send(embed=embed)
        await db.execute(
            "UPDATE employee SET clockChannelId = ?, clockMessageId = ? WHERE id = ?",
            (message.channel.id, message.id, employee_id),
        )
        await render_clock(self, message, employee_id)
        return message

    def _admin_visible_channels(self) -> set[int]:
        """Channels where admin/management command results are shown to everyone
        (not ephemeral): the timecard admin channel and the timecard log channel."""
        ids = set()
        for var in ("TIMECARD_ADMIN_CHANNEL_ID", "TIMECARD_LOG_ID"):
            v = os.getenv(var)
            if v and v.isdigit():
                ids.add(int(v))
        return ids

    def _eph(self, ctx) -> bool:
        """Whether an admin command should reply ephemerally. False in the
        timecard admin/log channels (show everyone); True elsewhere (declutter)."""
        return ctx.channel_id not in self._admin_visible_channels()

    async def cog_after_invoke(self, ctx):
        """When an admin management command runs *outside* the timecard admin/log
        channels its reply is ephemeral, so mirror a one-line record to the
        timecard log channel — the per-command logs carry the detailed result."""
        try:
            cmd = getattr(ctx, "command", None)
            if cmd and cmd.name in _MANAGEMENT_COMMANDS and self._eph(ctx):
                where = f"#{ctx.channel}" if getattr(ctx, "channel", None) else "a DM"
                timecard_log.info(f"[Cmd] {ctx.author} ran /{cmd.qualified_name} in {where}.")
        except Exception:  # noqa: BLE001 - logging must never break a command
            pass

    async def _refresh_clock(self, employee_id: int):
        """Re-render an employee's clock message if they have one (best-effort)."""
        row = await self.db.fetchone(
            "SELECT clockChannelId, clockMessageId FROM employee WHERE id = ?", (employee_id,)
        )
        if not row or not row["clockChannelId"] or not row["clockMessageId"]:
            return
        try:
            msg = await self.obtain_message(row["clockChannelId"], row["clockMessageId"])
            await render_clock(self, msg, employee_id)
        except Exception:  # noqa: BLE001
            log.warning("[Clock] Could not refresh clock for employee %s", employee_id)

    async def set_employee_archived(self, employee_id: int, archived: bool) -> bool:
        """Archive or reactivate an employee. Used by the Odoo hr.employee reconcile
        so terminating a temp worker in Odoo removes their Discord clock.

        Archiving deletes their clock message and nulls the clock pointers so they
        can no longer punch in -- but keeps the employee row and all their punches /
        worktime for payroll and reports. Reactivating just clears the flag; an admin
        re-creates the clock with /createclock. Returns True if state changed.
        """
        db = await self._ensure_db()
        row = await db.fetchone(
            "SELECT name, archived, clockChannelId, clockMessageId FROM employee WHERE id = ?",
            (employee_id,),
        )
        if row is None or bool(row["archived"]) == archived:
            return False
        who = f"{row['name']} (employee {employee_id})"
        if archived:
            # Remove the clock message, move their channel to the Disabled category
            # (keep the channel id so reactivation can move it back), keep history.
            if row["clockMessageId"]:
                await self._delete_clock_message(row["clockChannelId"], row["clockMessageId"])
            moved = await self._move_clock_channel(employee_id, row["clockChannelId"], "TIMECARD_DISABLED_CATEGORY_ID")
            await db.execute(
                "UPDATE employee SET archived = 1, clockMessageId = NULL WHERE id = ?", (employee_id,))
            timecard_log.info(
                f"[Employee] Archived {who}; clock removed{' + channel moved to Disabled' if moved else ''}, history kept.")
        else:
            await db.execute("UPDATE employee SET archived = 0 WHERE id = ?", (employee_id,))
            # Move the channel back to the Timecards category and rebuild the clock in it.
            await self._move_clock_channel(employee_id, row["clockChannelId"], "TIMECARD_CATEGORY_ID")
            rebuilt = await self._rebuild_clock_channel(employee_id, row["clockChannelId"])
            timecard_log.info(
                f"[Employee] Reactivated {who}; "
                f"{'channel moved back + clock rebuilt' if rebuilt else 'recreate their clock with /createclock'}.")
        return True

    def _category_from_env(self, key: str):
        """The configured category channel for a TIMECARD_*_CATEGORY_ID key, or None."""
        cid = os.getenv(key)
        return self.bot.get_channel(int(cid)) if cid and cid.isdigit() else None

    async def _move_clock_channel(self, employee_id: int, channel_id, category_key: str) -> bool:
        """Move an employee's **dedicated** clock channel into a configured category.
        No-op (returns False) when the channel is shared with another employee, is
        missing, or the target category isn't configured."""
        if not channel_id:
            return False
        shared = await self.db.fetchone(
            "SELECT 1 FROM employee WHERE clockChannelId = ? AND id != ? LIMIT 1", (channel_id, employee_id))
        if shared:
            log.debug("[Employee] Clock channel %s is shared; not moving it.", channel_id)
            return False
        category = self._category_from_env(category_key)
        channel = self.bot.get_channel(int(channel_id)) if category is not None else None
        if channel is None:
            return False
        try:
            await channel.edit(category=category)
            log.info("[Employee] Moved #%s to '%s'.", channel.name, category.name)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("[Employee] Couldn't move clock channel %s: %s", channel_id, e)
            return False

    async def _rebuild_clock_channel(self, employee_id: int, channel_id) -> bool:
        """Rebuild an employee's clock message in their existing channel (used on
        reactivation). Returns False if the channel is gone."""
        if not channel_id:
            return False
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            return False
        try:
            await self.make_clock(employee_id, channel)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("[Employee] Couldn't rebuild clock for employee %s: %s", employee_id, e)
            return False

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_db()
        db = self.db
        log.info("[Clock] Re-initializing clock views for each employee...")
        rows = await db.fetchall(
            "SELECT id, clockChannelId, clockMessageId FROM employee "
            "WHERE clockChannelId IS NOT NULL AND clockMessageId IS NOT NULL"
        )
        for r in rows:
            try:
                msg = await self.obtain_message(r["clockChannelId"], r["clockMessageId"])
                await render_clock(self, msg, r["id"])
            except discord.NotFound:
                log.warning(f"[Clock] Message for employee {r['id']} not found; skipping.")
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Clock] Failed to restore clock for {r['id']}: {e}")
        log.info("[Clock] Finished re-initializing clock views.")

        log.info("[Approval] Re-initializing approval message views...")
        rows = await db.fetchall(
            "SELECT id, checkChannelId, checkMessageId FROM punch_clock "
            "WHERE checkChannelId IS NOT NULL AND checkMessageId IS NOT NULL"
        )
        for r in rows:
            try:
                msg = await self.obtain_message(r["checkChannelId"], r["checkMessageId"])
                await msg.edit(view=await ApprovePunch.create(self, r["id"], msg))
            except discord.NotFound:
                log.warning(f"[Approval] Message for punch {r['id']} not found; skipping.")
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Approval] Failed to restore approval for punch {r['id']}: {e}")
        log.info("[Approval] Finished re-initializing approval message views.")

        # Re-attach pending delete-approval views (Odoo-side deletions awaiting a decision).
        for r in await db.fetchall(
            "SELECT * FROM pending_action WHERE action = 'delete' "
            "AND channel_id IS NOT NULL AND message_id IS NOT NULL"
        ):
            try:
                msg = await self.obtain_message(r["channel_id"], r["message_id"])
                await msg.edit(view=DeleteApproval.from_row(self, r))
            except discord.NotFound:
                await db.execute("DELETE FROM pending_action WHERE id = ?", (r["id"],))
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Delete] Failed to restore delete-approval {r['id']}: {e}")

    # ---- customer commands -------------------------------------------------

    @discord.slash_command(name="addcustomer", description="Add a new Customer to the customer table.")
    @is_timecard_admin()
    async def addcustomer(
        self, ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Full Name of Customer"),  # type: ignore
    ):
        db = await self._ensure_db()
        existing = await db.fetchall("SELECT id FROM customer WHERE name = ?", (name,))
        if existing:
            await ctx.respond(f"'{name}' already exists in the customer table.", ephemeral=self._eph(ctx))
            return
        customer_id = await db.execute("INSERT INTO customer (name) VALUES (?)", (name,))
        await sync.enqueue(db, "customer", customer_id, "create")
        await ctx.respond(f"Successfully inserted {name} into the customer table.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="editcustomer", description="Edit an existing Customer in the customer table.")
    @is_timecard_admin()
    async def editcustomer(
        self, ctx: discord.ApplicationContext,
        newname: discord.Option(str, description="New name for Customer"),  # type: ignore
        id: discord.Option(int, default=None, description="Id of Customer"),  # type: ignore
        name: discord.Option(str, default=None, description="Name of Customer"),  # type: ignore
    ):
        db = await self._ensure_db()
        if id is None and name is None:
            await ctx.respond("You must provide either id or name.", ephemeral=self._eph(ctx))
            return
        if id is not None:
            row = await db.fetchone("SELECT id, name FROM customer WHERE id = ?", (id,))
        else:
            rows = await db.fetchall("SELECT id, name FROM customer WHERE name = ?", (name,))
            if len(rows) != 1:
                await ctx.respond(f"Search for '{name}' returned {len(rows)} results; be more specific.", ephemeral=self._eph(ctx))
                return
            row = rows[0]
        if row is None:
            await ctx.respond("Could not find that customer.", ephemeral=self._eph(ctx))
            return
        await db.execute("UPDATE customer SET name = ? WHERE id = ?", (newname, row["id"]))
        await ctx.respond(f"Updated customer {row['id']} ({row['name']}) to {newname}.", ephemeral=self._eph(ctx))

    async def resolve_customer(self, name: str) -> int:
        """Find (or create) a local customer row by name; return its id.

        Used when a worktime is tagged with an Odoo task/project so reports can
        still group by customer whether or not Odoo is reachable. Customers that
        come from Odoo already exist there, so no outbox row is enqueued.
        """
        name = (name or "").strip()
        if not name:
            return 0
        row = await self.db.fetchone("SELECT id FROM customer WHERE name = ?", (name,))
        if row:
            return row["id"]
        return await self.db.execute("INSERT INTO customer (name) VALUES (?)", (name,))

    # ---- employee commands -------------------------------------------------

    async def employee_type_autocomplete(self, ctx: discord.AutocompleteContext):
        if self.db is None:
            return []
        rows = await self.db.fetchall("SELECT id, name FROM employee_type")
        return [discord.OptionChoice(name=r["name"], value=r["id"]) for r in rows]

    async def odoo_employee_autocomplete(self, ctx: discord.AutocompleteContext):
        """Suggest **unlinked** Odoo hr.employee records — an Odoo employee already
        linked to a local employee is hidden (linking is one-to-one). The Odoo list
        is cached per run; the linked set is read fresh so it updates as links change."""
        if not self.client.loaded:
            return []
        if self._odoo_employees is None:
            try:
                self._odoo_employees = await self.client.get_employee_list() or []
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Odoo] employee autocomplete fetch failed: {e}")
                return []
        linked = set()
        if self.db is not None:
            linked = {r["odooId"] for r in
                      await self.db.fetchall("SELECT odooId FROM employee WHERE odooId IS NOT NULL")}
        term = str(ctx.value or "").lower()
        matches = [e for e in self._odoo_employees
                   if e["id"] not in linked and term in str(e.get("display_name") or "").lower()]

        def label(e):
            nm = e.get("display_name") or "Employee"
            arch = " · archived" if not e.get("active", True) else ""
            return f"{nm} (Odoo #{e['id']}{arch})"  # id shown so same-named employees are distinguishable
        return [_choice(label(e), str(e["id"]), f"Employee #{e['id']}") for e in matches[:25]]

    async def linked_employee_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local employees currently linked to an Odoo employee (for /unlinkemployee).
        The choice value is a mention so it parses like a manually-typed user."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name, odooId FROM employee WHERE odooId IS NOT NULL ORDER BY name"
        )
        term = str(ctx.value or "").lower()
        out = []
        for r in rows:
            if term in str(r["name"] or "").lower() or term in str(r["odooId"]):
                out.append(_choice(f"{r['name']} (Odoo #{r['odooId']})", f"<@{r['id']}>"))
        return out[:25]

    async def unlinked_employee_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local employees not yet linked to an Odoo employee (for /linkemployee).
        The choice value is a mention so it parses like a manually-typed user."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name FROM employee WHERE odooId IS NULL ORDER BY name"
        )
        term = str(ctx.value or "").lower()
        out = [_choice(r["name"], f"<@{r['id']}>")
               for r in rows if term in str(r["name"] or "").lower()]
        return out[:25]

    async def archived_employee_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local employees currently archived (for /unarchiveemployee)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall("SELECT id, name FROM employee WHERE archived = 1 ORDER BY name")
        term = str(ctx.value or "").lower()
        return [_choice(r["name"], f"<@{r['id']}>")
                for r in rows if term in str(r["name"] or "").lower()][:25]

    async def unlinked_customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local customers not yet linked to an Odoo partner (value = local id)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name FROM customer WHERE odooId IS NULL "
            "AND (archived IS NULL OR archived = 0) ORDER BY name"
        )
        term = str(ctx.value or "").lower()
        return [_choice(f"{r['name']} (#{r['id']})", str(r["id"]))
                for r in rows if term in str(r["name"] or "").lower()][:25]

    async def linked_customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local customers currently linked to an Odoo partner (value = local id)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name, odooId FROM customer WHERE odooId IS NOT NULL ORDER BY name"
        )
        term = str(ctx.value or "").lower()
        return [_choice(f"{r['name']} (#{r['id']} · Odoo #{r['odooId']})", str(r["id"]))
                for r in rows if term in str(r["name"] or "").lower() or term in str(r["odooId"])][:25]

    async def odoo_customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """Suggest **unlinked** Odoo customers (res.partner). Cached per run; the
        linked set is read fresh so it updates as links change."""
        if not self.client.loaded:
            return []
        if self._odoo_customers is None:
            try:
                self._odoo_customers = await self.client.get_customer_list() or []
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Odoo] customer autocomplete fetch failed: {e}")
                return []
        linked = set()
        if self.db is not None:
            linked = {r["odooId"] for r in
                      await self.db.fetchall("SELECT odooId FROM customer WHERE odooId IS NOT NULL")}
        term = str(ctx.value or "").lower()
        matches = [c for c in self._odoo_customers
                   if c["id"] not in linked and term in str(c.get("display_name") or "").lower()]
        # Show the Odoo id so same-named partners (two "Steve Long"s) are distinguishable.
        return [_choice(f"{c.get('display_name') or 'Partner'} (Odoo #{c['id']})", str(c["id"]), f"Partner #{c['id']}")
                for c in matches[:25]]

    async def employee_autocomplete(self, ctx: discord.AutocompleteContext):
        """All active employees (value = a mention, so it parses like a typed user)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name FROM employee WHERE archived = 0 OR archived IS NULL ORDER BY name"
        )
        term = str(ctx.value or "").lower()
        return [_choice(r["name"], f"<@{r['id']}>")
                for r in rows if term in str(r["name"] or "").lower()][:25]

    async def punch_autocomplete(self, ctx: discord.AutocompleteContext):
        """Recent punches, shown as 'Name · MM-DD HH:MM→out (#id)' (value = punch id
        as a string, so the option is text-typed and filterable by employee name)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT pc.id, e.name, pc.punchInTime, pc.punchOutTime FROM punch_clock pc "
            "JOIN employee e ON pc.employeeID = e.id ORDER BY pc.id DESC LIMIT 300"
        )
        term = str(ctx.value or "").lower()
        out = []
        for r in rows:
            pin = (r["punchInTime"] or "")[5:16] or "?"
            pout = (r["punchOutTime"] or "")[5:16] or "open"
            label = f"{r['name']} · {pin}→{pout} (#{r['id']})"
            if term in label.lower():
                out.append(discord.OptionChoice(name=label[:100], value=str(r["id"])))
            if len(out) >= 25:
                break
        return out

    async def customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """All active customers by name (value = local customer id)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name FROM customer WHERE archived = 0 OR archived IS NULL ORDER BY name"
        )
        term = str(ctx.value or "").lower()
        return [_choice(r["name"], str(r["id"]))
                for r in rows if term in str(r["name"] or "").lower()][:25]

    async def merge_customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """Every customer with its local id, Odoo id, and archived state — so the two
        same-named duplicates in a merge are distinguishable."""
        if self.db is None:
            return []
        rows = await self.db.fetchall("SELECT id, name, odooId, archived FROM customer ORDER BY name")
        term = str(ctx.value or "").lower()
        out = []
        for r in rows:
            odoo = f" · Odoo #{r['odooId']}" if r["odooId"] else ""
            arch = " · archived" if r["archived"] else ""
            label = f"{r['name']} (#{r['id']}{odoo}{arch})"
            if term in str(r["name"] or "").lower() or term in str(r["id"]):
                out.append(_choice(label, str(r["id"])))
            if len(out) >= 25:
                break
        return out

    async def archived_customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """Archived customers (for /unarchivecustomer), with id + Odoo id."""
        if self.db is None:
            return []
        rows = await self.db.fetchall("SELECT id, name, odooId FROM customer WHERE archived = 1 ORDER BY name")
        term = str(ctx.value or "").lower()
        out = []
        for r in rows:
            odoo = f" · Odoo #{r['odooId']}" if r["odooId"] else ""
            if term in str(r["name"] or "").lower() or term in str(r["id"]):
                out.append(_choice(f"{r['name']} (#{r['id']}{odoo})", str(r["id"])))
            if len(out) >= 25:
                break
        return out

    async def worktime_autocomplete(self, ctx: discord.AutocompleteContext):
        """Recent worktime entries, shown as 'Name · Type Nh Customer (#id)'."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT wt.id, e.name AS ename, wt.punchType, wt.timeSpent, c.name AS cname "
            "FROM work_time wt JOIN punch_clock pc ON wt.punchID = pc.id "
            "JOIN employee e ON pc.employeeID = e.id LEFT JOIN customer c ON wt.customerID = c.id "
            "ORDER BY wt.id DESC LIMIT 300"
        )
        term = str(ctx.value or "").lower()
        out = []
        for r in rows:
            label = f"{r['ename']} · {r['punchType']} {r['timeSpent'] / 60:g}h" \
                    f"{(' ' + r['cname']) if r['cname'] else ''} (#{r['id']})"
            if term in label.lower():
                out.append(discord.OptionChoice(name=label[:100], value=str(r["id"])))
            if len(out) >= 25:
                break
        return out

    async def task_autocomplete(self, ctx: discord.AutocompleteContext):
        """Odoo tasks for the chosen customer, ranked by planned-start proximity
        to the punch time (used to link a manually-added worktime to Odoo)."""
        if self.db is None or not self.client.loaded:
            return []
        opts = ctx.options or {}
        customer_id = _opt_int(opts.get("customer"))
        punch_in = None
        wt_id = _opt_int(opts.get("worktime"))  # /editworktime: derive customer + punch from the entry
        if wt_id:
            wt = await self.db.fetchone(
                "SELECT wt.customerID, pc.punchInTime FROM work_time wt "
                "JOIN punch_clock pc ON wt.punchID = pc.id WHERE wt.id = ?", (wt_id,)
            )
            if wt:
                customer_id = customer_id or wt["customerID"]
                punch_in = wt["punchInTime"]
        punch_id = _opt_int(opts.get("punch"))  # /addworktime
        if punch_id and punch_in is None:
            prow = await self.db.fetchone("SELECT punchInTime FROM punch_clock WHERE id = ?", (punch_id,))
            punch_in = prow["punchInTime"] if prow else None
        if not customer_id:
            return []
        crow = await self.db.fetchone("SELECT odooId FROM customer WHERE id = ?", (customer_id,))
        if not crow or not crow["odooId"]:
            return []  # customer isn't linked to Odoo -> no tasks to link
        try:
            tasks = await self.client.search_tasks_for_partner(crow["odooId"], str(ctx.value or ""))
        except Exception as e:  # noqa: BLE001
            log.warning(f"[Odoo] task autocomplete fetch failed: {e}")
            return []
        ref = None
        if punch_in:
            try:
                ref = datetime.strptime(sync.local_str_to_utc_str(punch_in), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                ref = None

        def proximity(t):
            pd = t.get("planned_date_begin")
            if not pd or ref is None:
                return (1, 0.0)  # undated / no reference -> rank after the dated ones
            try:
                dt = datetime.strptime(str(pd)[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return (1, 0.0)
            return (0, abs((dt - ref).total_seconds()))

        tasks.sort(key=proximity)
        out = []
        for t in tasks[:25]:
            pd = str(t.get("planned_date_begin") or "")[:10]
            label = f"{t.get('display_name') or ''}{(' · ' + pd) if pd else ''}"
            out.append(_choice(label, str(t["id"]), f"Task #{t['id']}"))
        return out

    async def project_autocomplete(self, ctx: discord.AutocompleteContext):
        """Active Odoo projects (value = project id as a string)."""
        if not self.client.loaded:
            return []
        try:
            rows = await self.client.get_project_list(str(ctx.value or ""))
        except Exception as e:  # noqa: BLE001
            log.warning(f"[Odoo] project autocomplete fetch failed: {e}")
            return []
        return [_choice(r.get("display_name"), str(r["id"]), f"Project #{r['id']}")
                for r in (rows or [])][:25]

    @discord.slash_command(name="addemployee", description="Add a new Employee to the system.")
    @is_timecard_admin()
    async def addemployee(
        self, ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Full Name of Employee"),  # type: ignore
        phonenumber: discord.Option(str, description="Phone Number of Employee"),  # type: ignore
        addressline1: discord.Option(str, description="Address Line 1 of Employee"),  # type: ignore
        city: discord.Option(str, description="Address City of Employee"),  # type: ignore
        state: discord.Option(str, description="Address State of Employee"),  # type: ignore
        zip: discord.Option(str, description="Address Zip of Employee"),  # type: ignore
        addressline2: discord.Option(str, default="", description="Address Line 2"),  # type: ignore
        user: discord.Option(str, default=None, description="Attach a different user"),  # type: ignore
        payrate: discord.Option(float, default=16.00, description="Payrate"),  # type: ignore
        employeetype: discord.Option(int, default=2, description="Employee Type", autocomplete=employee_type_autocomplete),  # type: ignore
        odoo_employee: discord.Option(int, default=None, description="Link to an Odoo employee (for sync)", autocomplete=odoo_employee_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        emp_id = ctx.author.id
        if user is not None:
            try:
                emp_id = int(user.strip()[2:-1])
            except (ValueError, IndexError):
                await ctx.respond("User override was improperly formatted.", ephemeral=self._eph(ctx))
                return

        existing = await db.fetchall("SELECT id FROM employee WHERE id = ?", (emp_id,))
        if existing:
            await ctx.respond(f"{name} (<@{emp_id}>) already exists in the database.", ephemeral=self._eph(ctx))
            return
        if odoo_employee is not None:
            clash = await db.fetchone("SELECT id, name FROM employee WHERE odooId = ?", (odoo_employee,))
            if clash is not None:
                await ctx.respond(
                    f"Odoo employee {odoo_employee} is already linked to {clash['name']} (<@{clash['id']}>). "
                    f"Unlink them first with /unlinkemployee.", ephemeral=self._eph(ctx),
                )
                return
        try:
            await db.execute(
                "INSERT INTO employee (id, name, phoneNumber, addressLine1, addressLine2, "
                "addressCity, addressState, addressZip, payrate, employeeTypeID, odooId) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (emp_id, name, phonenumber, addressline1, addressline2, city, state, zip,
                 payrate, employeetype, odoo_employee),
            )
            await ctx.respond(f"Added new employee {name} (<@{emp_id}>).", ephemeral=self._eph(ctx))
        except Exception as e:  # noqa: BLE001
            await ctx.respond(f"Error adding employee {name}: {e}", ephemeral=self._eph(ctx))

    @discord.slash_command(name="linkemployee", description="Link an existing employee to their Odoo hr.employee record.")
    @is_timecard_admin()
    async def linkemployee(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The employee to link", autocomplete=unlinked_employee_autocomplete),  # type: ignore
        odoo_employee: discord.Option(str, description="Odoo employee", autocomplete=odoo_employee_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        await ctx.defer(ephemeral=self._eph(ctx))  # Odoo read + possible clock removal below
        odoo_employee = _opt_int(odoo_employee)
        if odoo_employee is None:
            await ctx.respond("Pick an Odoo employee from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        try:
            emp_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name FROM employee WHERE id = ?", (emp_id,))
        if row is None:
            await ctx.respond(f"{user} is not in the employee database. Add them with /addemployee first.", ephemeral=self._eph(ctx))
            return
        # Linking is one-to-one: refuse an Odoo employee already linked elsewhere.
        clash = await db.fetchone(
            "SELECT id, name FROM employee WHERE odooId = ? AND id != ?", (odoo_employee, emp_id)
        )
        if clash is not None:
            await ctx.respond(
                f"Odoo employee {odoo_employee} is already linked to {clash['name']} (<@{clash['id']}>). "
                f"Unlink them first with /unlinkemployee.", ephemeral=self._eph(ctx),
            )
            return
        await db.execute("UPDATE employee SET odooId = ? WHERE id = ?", (odoo_employee, emp_id))
        timecard_log.info(f"[Employee] {ctx.author} linked employee {emp_id} to Odoo employee {odoo_employee}.")
        # If the Odoo employee is archived (terminated), archive them here too — keeps
        # their history but hides them from active lists and removes their clock.
        note = ""
        try:
            rec = await self.client.read_record("hr.employee", odoo_employee, ["active"], include_archived=True)
            if rec is not None and not rec.get("active", True):
                if await self.set_employee_archived(emp_id, True):
                    note = " They're archived in Odoo, so I archived them here too (history kept, hidden from active lists)."
        except Exception as e:  # noqa: BLE001
            log.warning(f"[Employee] Couldn't check Odoo archive state for {odoo_employee}: {e}")
        await ctx.respond(f"Linked {row['name']} (<@{emp_id}>) to Odoo employee {odoo_employee}.{note}", ephemeral=self._eph(ctx))

    @discord.slash_command(name="archiveemployee", description="Archive an employee: hide from active lists + remove their clock, keep history.")
    @is_timecard_admin()
    async def archiveemployee(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The employee to archive", autocomplete=employee_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        # When Odoo is the source of truth for employees, archive there (it syncs
        # here via the hr.employee webhook) rather than diverging the two.
        if self.client.loaded:
            await ctx.respond(
                "Odoo is configured, so employees are archived **in Odoo** — archive them there and "
                "it syncs here automatically (make sure they're linked with `/linkemployee`).",
                ephemeral=self._eph(ctx))
            return
        await ctx.defer(ephemeral=self._eph(ctx))  # removing the clock message is a Discord call
        try:
            emp_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name, archived FROM employee WHERE id = ?", (emp_id,))
        if row is None:
            await ctx.respond(f"{user} is not in the employee database.", ephemeral=self._eph(ctx))
            return
        if row["archived"]:
            await ctx.respond(f"{row['name']} (<@{emp_id}>) is already archived.", ephemeral=self._eph(ctx))
            return
        await self.set_employee_archived(emp_id, True)
        timecard_log.info(f"[Employee] {ctx.author} archived {row['name']} (employee {emp_id}).")
        await ctx.respond(
            f"Archived **{row['name']}** (<@{emp_id}>): clock removed, history kept, and they're "
            f"hidden from active lists. Reactivate with `/unarchiveemployee`.",
            ephemeral=self._eph(ctx),
        )

    @discord.slash_command(name="unarchiveemployee", description="Reactivate an archived employee (then rebuild their clock with /createclock).")
    @is_timecard_admin()
    async def unarchiveemployee(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The archived employee to reactivate", autocomplete=archived_employee_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        if self.client.loaded:
            await ctx.respond(
                "Odoo is configured, so employees are reactivated **in Odoo** (unarchive them there and "
                "it syncs here). Then rebuild their clock with `/createclock`.",
                ephemeral=self._eph(ctx))
            return
        try:
            emp_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name, archived FROM employee WHERE id = ?", (emp_id,))
        if row is None:
            await ctx.respond(f"{user} is not in the employee database.", ephemeral=self._eph(ctx))
            return
        if not row["archived"]:
            await ctx.respond(f"{row['name']} (<@{emp_id}>) isn't archived.", ephemeral=self._eph(ctx))
            return
        await self.set_employee_archived(emp_id, False)
        timecard_log.info(f"[Employee] {ctx.author} reactivated {row['name']} (employee {emp_id}).")
        await ctx.respond(
            f"Reactivated **{row['name']}** (<@{emp_id}>). Their channel is moved back to Timecards and "
            f"the clock rebuilt (or run `/createclock` if the channel was removed).",
            ephemeral=self._eph(ctx),
        )

    @discord.slash_command(name="unlinkemployee", description="Remove an employee's link to their Odoo hr.employee record.")
    @is_timecard_admin()
    async def unlinkemployee(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The linked employee to unlink", autocomplete=linked_employee_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            emp_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name, odooId FROM employee WHERE id = ?", (emp_id,))
        if row is None:
            await ctx.respond(f"{user} is not in the employee database.", ephemeral=self._eph(ctx))
            return
        if row["odooId"] is None:
            await ctx.respond(f"{row['name']} (<@{emp_id}>) isn't linked to an Odoo employee.", ephemeral=self._eph(ctx))
            return
        await db.execute("UPDATE employee SET odooId = NULL WHERE id = ?", (emp_id,))
        timecard_log.info(f"[Employee] {ctx.author} unlinked employee {emp_id} from Odoo employee {row['odooId']}.")
        await ctx.respond(
            f"Unlinked {row['name']} (<@{emp_id}>) from Odoo employee {row['odooId']}. "
            f"That Odoo employee is now available to link again; new punches won't sync until re-linked.",
            ephemeral=self._eph(ctx),
        )

    # ---- customer <-> Odoo linking -----------------------------------------

    @discord.slash_command(name="synccustomers", description="Auto-link local customers to Odoo partners by exact name.")
    @is_timecard_admin()
    async def synccustomers(self, ctx: discord.ApplicationContext):
        if not self.client.loaded:
            await ctx.respond("Odoo isn't configured, so there's nothing to link to.", ephemeral=self._eph(ctx))
            return
        await ctx.defer(ephemeral=self._eph(ctx))
        db = await self._ensure_db()
        try:
            odoo_customers = await self.client.get_customer_list() or []
        except Exception as e:  # noqa: BLE001
            log.exception("[Customer] synccustomers: Odoo fetch failed")
            await ctx.followup.send(f"Couldn't fetch Odoo customers: {e}", ephemeral=self._eph(ctx))
            return
        # Case-insensitive Odoo name -> [partner ids]. One Odoo call, match in memory.
        by_name: dict[str, list[int]] = {}
        id_to_name = {}
        for c in odoo_customers:
            by_name.setdefault((c["display_name"] or "").strip().lower(), []).append(c["id"])
            id_to_name[c["id"]] = c["display_name"] or f"#{c['id']}"
        already = {r["odooId"] for r in await db.fetchall("SELECT odooId FROM customer WHERE odooId IS NOT NULL")}
        unlinked = await db.fetchall(
            "SELECT id, name FROM customer WHERE odooId IS NULL AND (archived IS NULL OR archived = 0)"
        )
        linked = nomatch = via_history = 0
        ambiguous_details = []  # (local_name, [(partner_id, partner_name), ...])
        history = None  # {former_name_lower: [partner_id]}, lazy-loaded on first no-match
        for c in unlinked:
            key = (c["name"] or "").strip().lower()
            candidates = [i for i in by_name.get(key, []) if i not in already]
            if len(candidates) == 1:
                await db.execute("UPDATE customer SET odooId = ? WHERE id = ?", (candidates[0], c["id"]))
                already.add(candidates[0]); linked += 1
                continue
            if len(candidates) > 1:
                ambiguous_details.append((c["name"], [(i, id_to_name.get(i, f"#{i}")) for i in candidates]))
                continue
            # No current-name match — try former names from the Odoo chatter, so a
            # customer renamed on one side still links (fetched once, then cached).
            if history is None:
                history = await self.client.get_partner_name_history() or {}
            hist = [i for i in history.get(key, []) if i not in already]
            if len(hist) == 1:
                await db.execute("UPDATE customer SET odooId = ? WHERE id = ?", (hist[0], c["id"]))
                already.add(hist[0]); via_history += 1
            else:
                nomatch += 1
        ambiguous = len(ambiguous_details)
        timecard_log.info(
            f"[Customer] {ctx.author} synccustomers: {linked} linked, {via_history} via rename-history, "
            f"{ambiguous} ambiguous, {nomatch} no-match."
        )
        # Record each ambiguous customer + the partners it matched (so it's kept even
        # if the reply below truncates a long list).
        for local_name, matches in ambiguous_details:
            timecard_log.info(f"[Customer]   ambiguous: '{local_name}' matches "
                              + ", ".join(f"#{i} {nm}" for i, nm in matches))

        via = f", **{via_history}** by a previous (renamed) Odoo name" if via_history else ""
        body = (f"Auto-link complete: **{linked}** linked by exact name{via}. "
                f"**{ambiguous + nomatch}** still unlinked — **{ambiguous}** ambiguous "
                f"(the name matches more than one Odoo partner, so it can't pick one) and "
                f"**{nomatch}** no match. Link those with `/linkcustomer` (see `/unlinkedcustomers`).")
        if ambiguous_details:
            body += "\n\n__Ambiguous — each matched >1 Odoo partner:__"
            for local_name, matches in ambiguous_details:
                line = f"\n• **{local_name}** → " + ", ".join(f"#{i} {nm}" for i, nm in matches)
                if len(body) + len(line) > 1900:  # Discord message cap; rest is in the log channel
                    body += f"\n…and more — see the full list in the log channel."
                    break
                body += line
        await ctx.followup.send(body, ephemeral=self._eph(ctx))

    @discord.slash_command(name="linkcustomer", description="Link a local customer to their Odoo partner.")
    @is_timecard_admin()
    async def linkcustomer(
        self, ctx: discord.ApplicationContext,
        customer: discord.Option(str, description="Local customer", autocomplete=unlinked_customer_autocomplete),  # type: ignore
        odoo_partner: discord.Option(str, description="Odoo customer", autocomplete=odoo_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        customer, odoo_partner = _opt_int(customer), _opt_int(odoo_partner)
        if customer is None or odoo_partner is None:
            await ctx.respond("Pick both a local customer and an Odoo partner from the autocomplete lists.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT id, name FROM customer WHERE id = ?", (customer,))
        if row is None:
            await ctx.respond("That customer isn't in the database.", ephemeral=self._eph(ctx))
            return
        clash = await db.fetchone("SELECT name FROM customer WHERE odooId = ? AND id != ?", (odoo_partner, customer))
        if clash is not None:
            await ctx.respond(
                f"Odoo partner {odoo_partner} is already linked to '{clash['name']}'. "
                f"Unlink it first with /unlinkcustomer.", ephemeral=self._eph(ctx),
            )
            return
        await db.execute("UPDATE customer SET odooId = ? WHERE id = ?", (odoo_partner, customer))
        timecard_log.info(f"[Customer] {ctx.author} linked customer {customer} ({row['name']}) to Odoo partner {odoo_partner}.")
        await ctx.respond(f"Linked '{row['name']}' to Odoo partner {odoo_partner}.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="unlinkcustomer", description="Remove a customer's link to their Odoo partner.")
    @is_timecard_admin()
    async def unlinkcustomer(
        self, ctx: discord.ApplicationContext,
        customer: discord.Option(str, description="Linked customer", autocomplete=linked_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        customer = _opt_int(customer)
        if customer is None:
            await ctx.respond("Pick a customer from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name, odooId FROM customer WHERE id = ?", (customer,))
        if row is None:
            await ctx.respond("That customer isn't in the database.", ephemeral=self._eph(ctx))
            return
        if row["odooId"] is None:
            await ctx.respond(f"'{row['name']}' isn't linked to an Odoo partner.", ephemeral=self._eph(ctx))
            return
        await db.execute("UPDATE customer SET odooId = NULL WHERE id = ?", (customer,))
        timecard_log.info(f"[Customer] {ctx.author} unlinked customer {customer} ({row['name']}) from Odoo partner {row['odooId']}.")
        await ctx.respond(f"Unlinked '{row['name']}' from Odoo partner {row['odooId']}.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="unlinkedcustomers", description="List local customers not yet linked to an Odoo partner.")
    @is_timecard_admin()
    async def unlinkedcustomers(self, ctx: discord.ApplicationContext):
        db = await self._ensure_db()
        rows = await db.fetchall(
            "SELECT name FROM customer WHERE odooId IS NULL AND (archived IS NULL OR archived = 0) ORDER BY name"
        )
        if not rows:
            await ctx.respond("All customers are linked to Odoo. \U0001f389", ephemeral=self._eph(ctx))
            return
        names = [r["name"] for r in rows]
        preview = "\n".join(f"• {n}" for n in names[:40])
        more = f"\n…and {len(names) - 40} more." if len(names) > 40 else ""
        await ctx.respond(f"**{len(names)}** unlinked customer(s):\n{preview}{more}\n\nLink them with /linkcustomer.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="mergecustomers", description="Merge two duplicate customers into one (moves worktime, keeps history).")
    @is_timecard_admin()
    async def mergecustomers(
        self, ctx: discord.ApplicationContext,
        keep: discord.Option(str, description="Customer to KEEP", autocomplete=merge_customer_autocomplete),  # type: ignore
        remove: discord.Option(str, description="Customer to REMOVE (merged into keep)", autocomplete=merge_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        keep_id, remove_id = _opt_int(keep), _opt_int(remove)
        if keep_id is None or remove_id is None:
            await ctx.respond("Pick both customers from the autocomplete lists.", ephemeral=self._eph(ctx))
            return
        if keep_id == remove_id:
            await ctx.respond("Those are the same customer — pick two different ones.", ephemeral=self._eph(ctx))
            return
        if remove_id == 0:
            await ctx.respond("You can't remove the default/company customer.", ephemeral=self._eph(ctx))
            return
        krow = await db.fetchone("SELECT id, name, odooId FROM customer WHERE id = ?", (keep_id,))
        rrow = await db.fetchone("SELECT id, name, odooId FROM customer WHERE id = ?", (remove_id,))
        if krow is None or rrow is None:
            await ctx.respond("One of those customers doesn't exist.", ephemeral=self._eph(ctx))
            return

        n = (await db.fetchone("SELECT count(*) c FROM work_time WHERE customerID = ?", (remove_id,)))["c"]
        adopt_odoo = krow["odooId"] is None and rrow["odooId"] is not None
        adopt_note = f"adopt its Odoo link (#{rrow['odooId']})" if adopt_odoo else "no extra fields to adopt"
        confirm = Confirm(user=ctx.user, timeout=60)
        await ctx.respond(
            f"Merge **{rrow['name']}** (#{remove_id}"
            f"{f' · Odoo #{rrow['odooId']}' if rrow['odooId'] else ''}) **into** "
            f"**{krow['name']}** (#{keep_id}{f' · Odoo #{krow['odooId']}' if krow['odooId'] else ''})?\n"
            f"• move **{n}** worktime entr{'y' if n == 1 else 'ies'} to #{keep_id}\n"
            f"• {adopt_note}\n"
            f"• then delete customer #{remove_id}.",
            view=confirm, ephemeral=self._eph(ctx),
        )
        await confirm.wait()
        if not confirm.value:
            await ctx.followup.send("Cancelled — nothing was merged.", ephemeral=self._eph(ctx))
            return

        # Move worktime off the loser FIRST (work_time.customerID has a FK), adopt any
        # missing field, cancel its pending Odoo push, then delete it.
        await db.execute("UPDATE work_time SET customerID = ? WHERE customerID = ?", (keep_id, remove_id))
        if adopt_odoo:
            await db.execute("UPDATE customer SET odooId = ? WHERE id = ?", (rrow["odooId"], keep_id))
        await db.execute(
            "UPDATE odoo_outbox SET status = 'skipped' WHERE status = 'pending' "
            "AND entity_type = 'customer' AND entity_id = ?", (remove_id,))
        await db.execute("DELETE FROM customer WHERE id = ?", (remove_id,))
        timecard_log.info(
            f"[Customer] {ctx.author} merged customer {remove_id} ('{rrow['name']}') into "
            f"{keep_id} ('{krow['name']}'): {n} worktime moved"
            f"{f', adopted Odoo #{rrow['odooId']}' if adopt_odoo else ''}.")
        await ctx.followup.send(
            f"Merged **{rrow['name']}** into **{krow['name']}** (#{keep_id}): moved **{n}** worktime "
            f"entr{'y' if n == 1 else 'ies'}{f' and adopted the Odoo link #{rrow['odooId']}' if adopt_odoo else ''}.",
            ephemeral=self._eph(ctx),
        )

    @discord.slash_command(name="archivecustomer", description="Archive (soft-delete) a customer: hide from lists, keep all history.")
    @is_timecard_admin()
    async def archivecustomer(
        self, ctx: discord.ApplicationContext,
        customer: discord.Option(str, description="Customer to archive", autocomplete=merge_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        cid = _opt_int(customer)
        if cid is None:
            await ctx.respond("Pick a customer from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        if cid == 0:
            await ctx.respond("You can't archive the default/company customer.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name, archived FROM customer WHERE id = ?", (cid,))
        if row is None:
            await ctx.respond("That customer isn't in the database.", ephemeral=self._eph(ctx))
            return
        if row["archived"]:
            await ctx.respond(f"'{row['name']}' (#{cid}) is already archived.", ephemeral=self._eph(ctx))
            return
        await db.execute("UPDATE customer SET archived = 1 WHERE id = ?", (cid,))
        timecard_log.info(f"[Customer] {ctx.author} archived customer {cid} ('{row['name']}').")
        await ctx.respond(
            f"Archived **{row['name']}** (#{cid}): hidden from active lists, all history kept. "
            f"Restore with `/unarchivecustomer`.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="unarchivecustomer", description="Restore an archived customer.")
    @is_timecard_admin()
    async def unarchivecustomer(
        self, ctx: discord.ApplicationContext,
        customer: discord.Option(str, description="Archived customer to restore", autocomplete=archived_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        cid = _opt_int(customer)
        if cid is None:
            await ctx.respond("Pick a customer from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name, archived FROM customer WHERE id = ?", (cid,))
        if row is None:
            await ctx.respond("That customer isn't in the database.", ephemeral=self._eph(ctx))
            return
        if not row["archived"]:
            await ctx.respond(f"'{row['name']}' (#{cid}) isn't archived.", ephemeral=self._eph(ctx))
            return
        await db.execute("UPDATE customer SET archived = 0 WHERE id = ?", (cid,))
        timecard_log.info(f"[Customer] {ctx.author} restored customer {cid} ('{row['name']}').")
        await ctx.respond(f"Restored **{row['name']}** (#{cid}).", ephemeral=self._eph(ctx))

    @discord.slash_command(name="deletecustomer", description="Delete a customer (or archive it if it has worktime history).")
    @is_timecard_admin()
    async def deletecustomer(
        self, ctx: discord.ApplicationContext,
        customer: discord.Option(str, description="Customer to delete", autocomplete=merge_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        cid = _opt_int(customer)
        if cid is None:
            await ctx.respond("Pick a customer from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        if cid == 0:
            await ctx.respond("You can't delete the default/company customer.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name, odooId, archived FROM customer WHERE id = ?", (cid,))
        if row is None:
            await ctx.respond("That customer isn't in the database.", ephemeral=self._eph(ctx))
            return
        n = (await db.fetchone("SELECT count(*) c FROM work_time WHERE customerID = ?", (cid,)))["c"]

        if n > 0:
            # Has history -> can't hard-delete (would lose worktime). Offer to archive.
            if row["archived"]:
                await ctx.respond(
                    f"'{row['name']}' (#{cid}) has **{n}** worktime entr{'y' if n == 1 else 'ies'}, so it can't be "
                    f"deleted without losing history — and it's already archived. Nothing to do.",
                    ephemeral=self._eph(ctx))
                return
            confirm = Confirm(user=ctx.user, timeout=60)
            await ctx.respond(
                f"**{row['name']}** (#{cid}) has **{n}** worktime entr{'y' if n == 1 else 'ies'} — deleting would "
                f"lose that history. **Archive** it instead (hide from lists, keep history)?",
                view=confirm, ephemeral=self._eph(ctx))
            await confirm.wait()
            if not confirm.value:
                await ctx.followup.send("Cancelled — nothing changed.", ephemeral=self._eph(ctx))
                return
            await db.execute("UPDATE customer SET archived = 1 WHERE id = ?", (cid,))
            timecard_log.info(f"[Customer] {ctx.author} archived customer {cid} ('{row['name']}') (had {n} worktime; delete declined).")
            await ctx.followup.send(f"Archived **{row['name']}** (#{cid}) instead — history kept.", ephemeral=self._eph(ctx))
            return

        # No worktime -> safe to hard-delete after confirmation.
        confirm = Confirm(user=ctx.user, timeout=60)
        await ctx.respond(
            f"Permanently delete **{row['name']}** (#{cid}"
            f"{f' · Odoo #{row['odooId']}' if row['odooId'] else ''})? It has no worktime, so nothing is lost.",
            view=confirm, ephemeral=self._eph(ctx))
        await confirm.wait()
        if not confirm.value:
            await ctx.followup.send("Cancelled — nothing was deleted.", ephemeral=self._eph(ctx))
            return
        await db.execute(
            "UPDATE odoo_outbox SET status = 'skipped' WHERE status = 'pending' "
            "AND entity_type = 'customer' AND entity_id = ?", (cid,))
        await db.execute("DELETE FROM customer WHERE id = ?", (cid,))
        timecard_log.info(
            f"[Customer] {ctx.author} DELETED customer {cid} ('{row['name']}'"
            f"{f', Odoo #{row['odooId']}' if row['odooId'] else ''}) — re-add with /addcustomer if needed.")
        await ctx.followup.send(f"Deleted **{row['name']}** (#{cid}).", ephemeral=self._eph(ctx))

    @discord.slash_command(name="purgeimportedcontacts", description="Clean up Odoo contacts wrongly imported as customers (employees/non-customers).")
    @is_timecard_admin()
    async def purgeimportedcontacts(self, ctx: discord.ApplicationContext):
        if not self.client.loaded:
            await ctx.respond("Odoo isn't configured, so there's nothing to check against.", ephemeral=self._eph(ctx))
            return
        await ctx.defer(ephemeral=self._eph(ctx))
        db = await self._ensure_db()
        try:
            valid_ids = await self.client.get_customer_partner_ids()
        except Exception as e:  # noqa: BLE001
            log.exception("[Customer] purgeimportedcontacts: Odoo fetch failed")
            await ctx.followup.send(f"Couldn't fetch the customer list from Odoo: {e}", ephemeral=self._eph(ctx))
            return
        linked = await db.fetchall("SELECT id, name, odooId, archived FROM customer WHERE odooId IS NOT NULL AND id != 0")
        to_delete, to_archive = [], []
        for c in linked:
            if c["odooId"] in valid_ids:
                continue  # a real customer in Odoo — keep
            n = (await db.fetchone("SELECT count(*) c FROM work_time WHERE customerID = ?", (c["id"],)))["c"]
            if n == 0:
                to_delete.append(c)
            elif not c["archived"]:
                to_archive.append((c, n))
        if not to_delete and not to_archive:
            await ctx.followup.send("No imported employee/non-customer contacts found. \U0001f389", ephemeral=self._eph(ctx))
            return

        lines = [f"🗑️ delete **{c['name']}** (#{c['id']} · Odoo #{c['odooId']})" for c in to_delete[:20]]
        lines += [f"📦 archive **{c['name']}** (#{c['id']}, {n} worktime)" for c, n in to_archive[:20]]
        extra = (len(to_delete) - 20 if len(to_delete) > 20 else 0) + (len(to_archive) - 20 if len(to_archive) > 20 else 0)
        summary = (f"Found **{len(to_delete)}** to delete + **{len(to_archive)}** to archive "
                   f"(non-customer partners in Odoo):\n" + "\n".join(lines)
                   + (f"\n…and {extra} more." if extra > 0 else "")
                   + "\n\nEach removal is logged to the timecard log channel for recovery. Proceed?")
        confirm = Confirm(user=ctx.user, timeout=120)
        await ctx.followup.send(summary[:1990], view=confirm, ephemeral=self._eph(ctx))
        await confirm.wait()
        if not confirm.value:
            await ctx.followup.send("Cancelled — nothing was removed.", ephemeral=self._eph(ctx))
            return

        for c in to_delete:
            await db.execute(
                "UPDATE odoo_outbox SET status = 'skipped' WHERE status = 'pending' "
                "AND entity_type = 'customer' AND entity_id = ?", (c["id"],))
            await db.execute("DELETE FROM customer WHERE id = ?", (c["id"],))
            timecard_log.info(
                f"[Customer] PURGE deleted '{c['name']}' (#{c['id']}, Odoo #{c['odooId']}) — "
                f"re-add with /addcustomer + /linkcustomer if needed.")
        for c, n in to_archive:
            await db.execute("UPDATE customer SET archived = 1 WHERE id = ?", (c["id"],))
            timecard_log.info(
                f"[Customer] PURGE archived '{c['name']}' (#{c['id']}, Odoo #{c['odooId']}, {n} worktime — kept).")
        timecard_log.info(f"[Customer] {ctx.author} purged imported contacts: {len(to_delete)} deleted, {len(to_archive)} archived.")
        await ctx.followup.send(
            f"Purge complete: **{len(to_delete)}** deleted, **{len(to_archive)}** archived (had worktime). "
            f"Each is listed in the log channel — re-add or `/unarchivecustomer` to bring one back.",
            ephemeral=self._eph(ctx))

    # ---- Odoo project configuration ----------------------------------------

    async def _project_config_text(self) -> str:
        """Render the current Field Service / Office project config (id + name)."""
        lines = []
        for label, key in (("Field Service", "ODOO_FIELD_SERVICE_PROJECT_ID"),
                            ("Office", "ODOO_OFFICE_PROJECT_ID")):
            pid = _opt_int(os.getenv(key))
            name = ""
            if pid and self.client.loaded:
                try:
                    rec = await self.client.read_record("project.project", pid, ["display_name"])
                    name = f" — {rec['display_name']}" if rec else " — ⚠️ not found in Odoo"
                except Exception:  # noqa: BLE001
                    name = ""
            lines.append(f"• **{label}**: {pid if pid else '_not set_'}{name}")
        return "\n".join(lines)

    @discord.slash_command(name="configureprojects", description="Set the Odoo Field Service / Office project ids used to categorize worktime.")
    @is_timecard_admin()
    async def configureprojects(
        self, ctx: discord.ApplicationContext,
        field_service: discord.Option(str, default=None, description="Odoo project for Field Service (Service) work", autocomplete=project_autocomplete),  # type: ignore
        office: discord.Option(str, default=None, description="Odoo project for Office work", autocomplete=project_autocomplete),  # type: ignore
    ):
        await self._ensure_db()
        # Validating the projects + reading their names below makes several Odoo
        # calls; defer first so we acknowledge within Discord's 3s window (else
        # the interaction expires -> 404 Unknown interaction). The ephemeral flag
        # carries through to the eventual reply.
        await ctx.defer(ephemeral=self._eph(ctx))
        changed = []
        for value, key, label in ((field_service, "ODOO_FIELD_SERVICE_PROJECT_ID", "Field Service"),
                                  (office, "ODOO_OFFICE_PROJECT_ID", "Office")):
            if value is None:
                continue
            pid = _opt_int(value)
            if pid is None:
                await ctx.respond(f"'{value}' isn't a valid project id for {label}.", ephemeral=self._eph(ctx))
                return
            if self.client.loaded:  # validate the project exists when Odoo is online
                try:
                    rec = await self.client.read_record("project.project", pid, ["id"])
                except Exception as e:  # noqa: BLE001
                    await ctx.respond(f"Couldn't reach Odoo to verify the {label} project: {e}", ephemeral=self._eph(ctx))
                    return
                if rec is None:
                    await ctx.respond(f"No Odoo project has id {pid} (for {label}). Pick one from the list.", ephemeral=self._eph(ctx))
                    return
            # Persist to .env AND the live process env (takes effect immediately,
            # no restart) -- same mechanism as the webhook token / IP allowlist.
            config.set_env(key, str(pid))
            changed.append(f"{label} → {pid}")
        if changed:
            timecard_log.info(f"[Config] {ctx.author} set Odoo project ids: {', '.join(changed)}.")
        cfg = await self._project_config_text()
        header = ("Updated: " + "; ".join(changed) + ".\n\n") if changed else "Current Odoo project configuration:\n\n"
        await ctx.respond(header + cfg, ephemeral=self._eph(ctx))

    def _role_config_text(self, guild) -> str:
        """Render the current timecard admin / timeclock role assignments."""
        lines = []
        for label, key in (("Timecard Admin", "TIMECARD_ADMIN_ROLE"),
                            ("Timeclock (shop)", "TIMECARD_TIMECLOCK_ROLE_ID")):
            rid = _opt_int(os.getenv(key))
            role = guild.get_role(rid) if (rid and guild) else None
            if role:
                shown = f"@{role.name} (id {role.id})"
            elif rid:
                shown = f"id {rid} — ⚠️ no such role in this server"
            else:
                shown = "_not set_"
            lines.append(f"• **{label}**: {shown}")
        return "\n".join(lines)

    @discord.slash_command(name="configureroles", description="Set which Discord roles are the timecard-admin and shop timeclock roles.")
    @is_timecard_admin()
    async def configureroles(
        self, ctx: discord.ApplicationContext,
        admin_role: discord.Option(discord.Role, default=None, description="Role that can run all timecard admin commands"),  # type: ignore
        timeclock_role: discord.Option(discord.Role, default=None, description="Shop role that can clock anyone in/out without approval"),  # type: ignore
    ):
        await self._ensure_db()
        changed = []
        for role, key, label in ((admin_role, "TIMECARD_ADMIN_ROLE", "Timecard Admin"),
                                 (timeclock_role, "TIMECARD_TIMECLOCK_ROLE_ID", "Timeclock (shop)")):
            if role is None:
                continue
            # Persist the role id to .env + the live env (immediate, no restart) --
            # same mechanism as /configureprojects.
            config.set_env(key, str(role.id))
            changed.append(f"{label} → @{role.name}")
        if changed:
            timecard_log.info(f"[Config] {ctx.author} set timecard roles: {', '.join(changed)}.")
        cfg = self._role_config_text(ctx.guild)
        header = ("Updated: " + "; ".join(changed) + ".\n\n") if changed else "Current timecard role configuration:\n\n"
        await ctx.respond(header + cfg, ephemeral=self._eph(ctx))

    @discord.slash_command(name="configurecategories", description="Set the Timecards + Disabled Timecards categories (archived clocks move to Disabled).")
    @is_timecard_admin()
    async def configurecategories(
        self, ctx: discord.ApplicationContext,
        timecards: discord.Option(discord.CategoryChannel, default=None, description="Category for active per-employee clock channels"),  # type: ignore
        disabled: discord.Option(discord.CategoryChannel, default=None, description="Category archived clocks move into"),  # type: ignore
    ):
        await self._ensure_db()
        changed = []
        for cat, key, label in ((timecards, "TIMECARD_CATEGORY_ID", "Timecards"),
                                (disabled, "TIMECARD_DISABLED_CATEGORY_ID", "Disabled Timecards")):
            if cat is None:
                continue
            config.persist_channel_id(key, cat.id)  # TESTING_* in test mode, base key in prod
            changed.append(f"{label} → {cat.name}")
        if changed:
            timecard_log.info(f"[Config] {ctx.author} set timecard categories: {', '.join(changed)}.")
        lines = []
        for label, key in (("Timecards", "TIMECARD_CATEGORY_ID"), ("Disabled Timecards", "TIMECARD_DISABLED_CATEGORY_ID")):
            cat = self._category_from_env(key)
            lines.append(f"• **{label}**: {cat.name if cat else '_not set_'}")
        header = ("Updated: " + "; ".join(changed) + ".\n\n") if changed else "Current timecard categories:\n\n"
        await ctx.respond(header + "\n".join(lines), ephemeral=self._eph(ctx))

    # ---- punch management (after-the-fact fixes, no DB browser needed) ------

    @discord.slash_command(name="addpunch", description="Manually add a punch for an employee (e.g. a missed clock-in).")
    @is_timecard_admin()
    async def addpunch(
        self, ctx: discord.ApplicationContext,
        employee: discord.Option(str, description="The employee", autocomplete=employee_autocomplete),  # type: ignore
        clock_in: discord.Option(str, description="Clock-in time [YYYY-MM-DD HH:MM]"),  # type: ignore
        clock_out: discord.Option(str, default=None, description="Clock-out time [YYYY-MM-DD HH:MM] (leave blank for an open punch)"),  # type: ignore
        approved: discord.Option(bool, default=True, description="Mark it approved (default yes)"),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            emp_id = int(employee[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{employee}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone("SELECT name FROM employee WHERE id = ?", (emp_id,))
        if row is None:
            await ctx.respond(f"{employee} is not in the employee database. Add them with /addemployee first.", ephemeral=self._eph(ctx))
            return
        try:
            pin = _parse_punch_time(clock_in)
            pout = _parse_punch_time(clock_out) if clock_out else None
        except ValueError as e:
            await ctx.respond(str(e), ephemeral=self._eph(ctx))
            return
        if pout and pout < pin:
            await ctx.respond("Clock-out is before clock-in.", ephemeral=self._eph(ctx))
            return
        appr = 1 if approved else 0
        punch_id = await db.execute(
            "INSERT INTO punch_clock (employeeID, punchInTime, punchInApproval, punchOutTime, punchOutApproval) "
            "VALUES (?, ?, ?, ?, ?)",
            (emp_id, pin, appr, pout, appr),
        )
        await sync.enqueue(db, "punch", punch_id, "in")
        if pout:
            await sync.enqueue(db, "punch", punch_id, "out")
        await self._refresh_clock(emp_id)
        timecard_log.info(f"[Punch] {ctx.author} added punch {punch_id} for employee {emp_id} ({pin} -> {pout or 'open'}).")
        await ctx.respond(f"Added punch #{punch_id} for {row['name']}: `{pin}` → `{pout or 'open'}`.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="editpunch", description="Fix a punch's clock-in/out time or approval.")
    @is_timecard_admin()
    async def editpunch(
        self, ctx: discord.ApplicationContext,
        punch: discord.Option(str, description="The punch to edit", autocomplete=punch_autocomplete),  # type: ignore
        clock_in: discord.Option(str, default=None, description="New clock-in time [YYYY-MM-DD HH:MM]"),  # type: ignore
        clock_out: discord.Option(str, default=None, description="New clock-out time [YYYY-MM-DD HH:MM]"),  # type: ignore
        approved: discord.Option(bool, default=None, description="Set approval (leave blank to keep as-is)"),  # type: ignore
    ):
        db = await self._ensure_db()
        punch = _opt_int(punch)
        if punch is None:
            await ctx.respond("Pick a punch from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone(
            "SELECT employeeID, punchInTime, punchOutTime, punchInApproval, punchOutApproval "
            "FROM punch_clock WHERE id = ?", (punch,)
        )
        if row is None:
            await ctx.respond("That punch doesn't exist.", ephemeral=self._eph(ctx))
            return
        sets, params, changes = [], [], []  # changes = human "field: old → new" for the log
        try:
            if clock_in is not None:
                nv = _parse_punch_time(clock_in)
                sets.append("punchInTime = ?"); params.append(nv)
                changes.append(f"clock-in {row['punchInTime'] or '—'} → {nv}")
            if clock_out is not None:
                nv = _parse_punch_time(clock_out)
                sets.append("punchOutTime = ?"); params.append(nv)
                changes.append(f"clock-out {row['punchOutTime'] or 'open'} → {nv}")
        except ValueError as e:
            await ctx.respond(str(e), ephemeral=self._eph(ctx))
            return
        if approved is not None:
            v = 1 if approved else 0
            sets += ["punchInApproval = ?", "punchOutApproval = ?"]; params += [v, v]
            was = "approved" if (row["punchInApproval"] and row["punchOutApproval"]) else "pending"
            changes.append(f"approval {was} → {'approved' if approved else 'unapproved'}")
        if not sets:
            await ctx.respond("Nothing to change — provide a new clock-in, clock-out, or approval.", ephemeral=self._eph(ctx))
            return
        await db.execute(f"UPDATE punch_clock SET {', '.join(sets)} WHERE id = ?", (*params, punch))
        await sync.enqueue(db, "punch", punch, "edit")
        await self._refresh_clock(row["employeeID"])
        erow = await db.fetchone("SELECT name FROM employee WHERE id = ?", (row["employeeID"],))
        ename = erow["name"] if erow else f"employee {row['employeeID']}"
        timecard_log.info(f"[Punch] {ctx.author} edited punch {punch} for {ename}: {'; '.join(changes)}.")
        await ctx.respond(f"Updated punch #{punch}.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="deletepunch", description="Delete a punch — review each linked worktime first.")
    @is_timecard_admin()
    async def deletepunch(
        self, ctx: discord.ApplicationContext,
        punch: discord.Option(str, description="The punch to delete", autocomplete=punch_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        punch = _opt_int(punch)
        if punch is None:
            await ctx.respond("Pick a punch from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone(
            "SELECT pc.employeeID, e.name AS ename, pc.punchInTime, pc.punchOutTime FROM punch_clock pc "
            "JOIN employee e ON pc.employeeID = e.id WHERE pc.id = ?", (punch,)
        )
        if row is None:
            await ctx.respond("That punch doesn't exist.", ephemeral=self._eph(ctx))
            return
        header = (f"Delete punch #{punch} for **{row['ename']}** "
                  f"(`{row['punchInTime']}` → `{row['punchOutTime'] or 'open'}`)")
        worktimes = await db.fetchall(
            "SELECT wt.id, wt.punchType, wt.timeSpent, c.name AS cname FROM work_time wt "
            "LEFT JOIN customer c ON wt.customerID = c.id WHERE wt.punchID = ? ORDER BY wt.id", (punch,)
        )

        # No worktime: a plain confirm is enough.
        if not worktimes:
            confirm = Confirm(user=ctx.user, timeout=60)
            await ctx.respond(f"{header}?", view=confirm, ephemeral=self._eph(ctx))
            await confirm.wait()
            if not confirm.value:
                await ctx.followup.send("Cancelled.", ephemeral=self._eph(ctx))
                return
            emp = await delete_punch_cascade(self, punch, to_odoo=True)
            timecard_log.info(f"[Punch] {ctx.author} deleted punch {punch} via /deletepunch.")
            if emp:
                await self._refresh_clock(emp)
            await ctx.followup.send(f"Deleted punch #{punch}.", ephemeral=self._eph(ctx))
            return

        # Candidate shifts to reassign onto: the same employee's other punches.
        candidates = await db.fetchall(
            "SELECT id, punchInTime, punchOutTime FROM punch_clock WHERE employeeID = ? "
            "AND id != ? ORDER BY id DESC LIMIT 24", (row["employeeID"], punch)
        )

        # Discord shows at most 4 per-worktime dropdowns (row 5 is the buttons).
        # For a punch with more, keep it to a delete-all / cancel confirm and
        # point the admin at /reassignworktime for any they want to keep.
        if len(worktimes) > 4:
            confirm = Confirm(user=ctx.user, timeout=60)
            await ctx.respond(
                f"{header} and its **{len(worktimes)}** worktime entries? "
                f"(To keep any, cancel and move them first with `/reassignworktime`.)",
                view=confirm, ephemeral=self._eph(ctx),
            )
            await confirm.wait()
            if not confirm.value:
                await ctx.followup.send("Cancelled.", ephemeral=self._eph(ctx))
                return
            emp = await delete_punch_cascade(self, punch, to_odoo=True)
            timecard_log.info(f"[Punch] {ctx.author} deleted punch {punch} (+{len(worktimes)} worktime) via /deletepunch.")
            if emp:
                await self._refresh_clock(emp)
            await ctx.followup.send(f"Deleted punch #{punch}.", ephemeral=self._eph(ctx))
            return

        flow = DeletePunchFlow(self, punch, worktimes, candidates, ctx.user.id)
        note = "" if candidates else "\n(No other shifts for this employee, so reassignment isn't available.)"
        await ctx.respond(
            f"{header} has **{len(worktimes)}** worktime "
            f"entr{'y' if len(worktimes) == 1 else 'ies'}. For each, choose **delete with "
            f"the punch** or **reassign to another shift**, then Confirm.{note}",
            view=flow, ephemeral=self._eph(ctx),
        )
        timecard_log.info(f"[Punch] {ctx.author} opened delete review for punch {punch} ({len(worktimes)} worktime).")

    @discord.slash_command(name="reassignworktime", description="Move a worktime to a different punch/shift (keeps Odoo's shift link correct).")
    @is_timecard_admin()
    async def reassignworktime(
        self, ctx: discord.ApplicationContext,
        worktime: discord.Option(str, description="The worktime to move", autocomplete=worktime_autocomplete),  # type: ignore
        punch: discord.Option(str, description="The punch/shift to move it onto", autocomplete=punch_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        worktime, punch = _opt_int(worktime), _opt_int(punch)
        if worktime is None or punch is None:
            await ctx.respond("Pick both the worktime and the target punch from the autocomplete lists.", ephemeral=self._eph(ctx))
            return
        if await db.fetchone("SELECT 1 FROM work_time WHERE id = ?", (worktime,)) is None:
            await ctx.respond("That worktime doesn't exist.", ephemeral=self._eph(ctx))
            return
        if await db.fetchone("SELECT 1 FROM punch_clock WHERE id = ?", (punch,)) is None:
            await ctx.respond("That target punch doesn't exist.", ephemeral=self._eph(ctx))
            return
        old_emp, new_emp = await reassign_worktime(self, worktime, punch, to_odoo=True)
        if new_emp is None:
            await ctx.respond("Couldn't reassign — it's already on that punch.", ephemeral=self._eph(ctx))
            return
        for e in {old_emp, new_emp} - {None}:
            await self._refresh_clock(e)
        timecard_log.info(f"[Work] {ctx.author} reassigned worktime {worktime} to punch {punch}.")
        note = " (Odoo shift link updated)" if self.client.loaded else ""
        await ctx.respond(f"Moved worktime #{worktime} onto punch #{punch}.{note}", ephemeral=self._eph(ctx))

    # ---- worktime management -----------------------------------------------

    @discord.slash_command(name="addworktime", description="Add a worktime entry to a punch.")
    @is_timecard_admin()
    async def addworktime(
        self, ctx: discord.ApplicationContext,
        punch: discord.Option(str, description="The punch/shift", autocomplete=punch_autocomplete),  # type: ignore
        worktype: discord.Option(str, description="Type of work", choices=WORKTYPES),  # type: ignore
        hours: discord.Option(float, description="Hours on the quarter hour (e.g. 1.5)"),  # type: ignore
        customer: discord.Option(str, default=None, description="Customer (required for Construction/Service)", autocomplete=customer_autocomplete),  # type: ignore
        task: discord.Option(str, default=None, description="Odoo task to link (customer tasks, nearest planned start first)", autocomplete=task_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        # An Odoo task lookup + clock refresh can exceed Discord's 3s window; defer first.
        await ctx.defer(ephemeral=self._eph(ctx))
        punch, customer, task = _opt_int(punch), _opt_int(customer), _opt_int(task)
        if punch is None:
            await ctx.respond("Pick a punch from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        prow = await db.fetchone("SELECT employeeID, punchInTime FROM punch_clock WHERE id = ?", (punch,))
        if prow is None:
            await ctx.respond("That punch doesn't exist.", ephemeral=self._eph(ctx))
            return
        try:
            minutes = _hours_to_minutes(hours)
        except ValueError as e:
            await ctx.respond(str(e), ephemeral=self._eph(ctx))
            return
        cust_id = 0
        if worktype in ("Construction", "Service"):
            if not customer:
                await ctx.respond(f"{worktype} work needs a customer.", ephemeral=self._eph(ctx))
                return
            if await db.fetchone("SELECT 1 FROM customer WHERE id = ?", (customer,)) is None:
                await ctx.respond("That customer doesn't exist.", ephemeral=self._eph(ctx))
                return
            cust_id = customer
        # An Odoo task makes the entry syncable: resolve its project so the
        # background worker can post the timesheet (with the shift link).
        odoo_task_id = odoo_project_id = None
        warn = ""
        if task and self.client.loaded:
            try:
                odoo_project_id = await self.client.get_task_project(task)
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Odoo] task project lookup failed: {e}")
            if odoo_project_id:
                odoo_task_id = task
            else:
                warn = " (couldn't resolve the Odoo task — saved locally only)"
        started = prow["punchInTime"] or sync.now_local_str()
        wt_id = await db.execute(
            "INSERT INTO work_time (punchID, customerID, punchType, timeSpent, timeStarted, odooTaskId, odooProjectId) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (punch, cust_id, worktype, minutes, started, odoo_task_id, odoo_project_id),
        )
        if odoo_project_id:
            await sync.enqueue(db, "worktime", wt_id, "create")
        await self._refresh_clock(prow["employeeID"])
        timecard_log.info(f"[Work] {ctx.author} added worktime {wt_id} to punch {punch} ({worktype}, {minutes}min).")
        if odoo_project_id:
            note = " — queued to Odoo."
        elif self.client.loaded:
            note = warn or " (local only — link an Odoo task to push it, or add it in Odoo directly)"
        else:
            note = ""
        await ctx.respond(f"Added {worktype} worktime #{wt_id} ({hours:g}h) to punch #{punch}.{note}", ephemeral=self._eph(ctx))

    @discord.slash_command(name="editworktime", description="Edit a worktime entry's type, hours, or customer.")
    @is_timecard_admin()
    async def editworktime(
        self, ctx: discord.ApplicationContext,
        worktime: discord.Option(str, description="The worktime to edit", autocomplete=worktime_autocomplete),  # type: ignore
        worktype: discord.Option(str, default=None, description="New type", choices=WORKTYPES),  # type: ignore
        hours: discord.Option(float, default=None, description="New hours (quarter-hour)"),  # type: ignore
        customer: discord.Option(str, default=None, description="New customer", autocomplete=customer_autocomplete),  # type: ignore
        task: discord.Option(str, default=None, description="Odoo task to (re)link", autocomplete=task_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        # An Odoo task lookup + clock refresh can exceed Discord's 3s window; defer first.
        await ctx.defer(ephemeral=self._eph(ctx))
        worktime, customer, task = _opt_int(worktime), _opt_int(customer), _opt_int(task)
        if worktime is None:
            await ctx.respond("Pick a worktime from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        wt = await db.fetchone(
            "SELECT punchID, odooId, punchType, timeSpent, customerID FROM work_time WHERE id = ?", (worktime,)
        )
        if wt is None:
            await ctx.respond("That worktime doesn't exist.", ephemeral=self._eph(ctx))
            return
        sets, params, changes = [], [], []  # changes = human "field: old → new" for the log
        if worktype is not None:
            sets.append("punchType = ?"); params.append(worktype)
            changes.append(f"type {wt['punchType']} → {worktype}")
        if hours is not None:
            try:
                mins = _hours_to_minutes(hours)
            except ValueError as e:
                await ctx.respond(str(e), ephemeral=self._eph(ctx))
                return
            sets.append("timeSpent = ?"); params.append(mins)
            changes.append(f"hours {(wt['timeSpent'] or 0) / 60:g}h → {hours:g}h")
        if customer is not None:
            newc = await db.fetchone("SELECT name FROM customer WHERE id = ?", (customer,))
            if newc is None:
                await ctx.respond("That customer doesn't exist.", ephemeral=self._eph(ctx))
                return
            oldc = await db.fetchone("SELECT name FROM customer WHERE id = ?", (wt["customerID"],))
            sets.append("customerID = ?"); params.append(customer)
            changes.append(f"customer {oldc['name'] if oldc else wt['customerID']} → {newc['name']}")
        if task is not None and self.client.loaded:
            try:
                pid = await self.client.get_task_project(task)
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Odoo] task project lookup failed: {e}")
                pid = None
            if not pid:
                await ctx.respond("Couldn't resolve that Odoo task's project.", ephemeral=self._eph(ctx))
                return
            sets.append("odooTaskId = ?"); params.append(task)
            sets.append("odooProjectId = ?"); params.append(pid)
            changes.append(f"Odoo task → {task}")
        if not sets:
            await ctx.respond("Nothing to change — provide a new type, hours, customer, or task.", ephemeral=self._eph(ctx))
            return
        await db.execute(f"UPDATE work_time SET {', '.join(sets)} WHERE id = ?", (*params, worktime))
        push = self.client.loaded and (wt["odooId"] or task is not None)
        if push:
            await sync.enqueue(db, "worktime", worktime, "edit")
        prow = await db.fetchone(
            "SELECT pc.employeeID, e.name FROM punch_clock pc JOIN employee e ON pc.employeeID = e.id "
            "WHERE pc.id = ?", (wt["punchID"],))
        if prow:
            await self._refresh_clock(prow["employeeID"])
        ename = prow["name"] if prow else "?"
        timecard_log.info(f"[Work] {ctx.author} edited worktime {worktime} for {ename}: {'; '.join(changes)}.")
        note = " — change queued to Odoo." if push else ""
        await ctx.respond(f"Updated worktime #{worktime}.{note}", ephemeral=self._eph(ctx))

    @discord.slash_command(name="deleteworktime", description="Delete a single worktime entry.")
    @is_timecard_admin()
    async def deleteworktime(
        self, ctx: discord.ApplicationContext,
        worktime: discord.Option(str, description="The worktime to delete", autocomplete=worktime_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        worktime = _opt_int(worktime)
        if worktime is None:
            await ctx.respond("Pick a worktime from the autocomplete list.", ephemeral=self._eph(ctx))
            return
        if await db.fetchone("SELECT 1 FROM work_time WHERE id = ?", (worktime,)) is None:
            await ctx.respond("That worktime doesn't exist.", ephemeral=self._eph(ctx))
            return
        emp = await delete_worktime_local(self, worktime, to_odoo=True)
        if emp:
            await self._refresh_clock(emp)
        timecard_log.info(f"[Work] {ctx.author} deleted worktime {worktime} via /deleteworktime.")
        await ctx.respond(f"Deleted worktime #{worktime}.", ephemeral=self._eph(ctx))

    # ---- clock commands ----------------------------------------------------

    @discord.slash_command(name="createclock", description="Create a time clock embed for a user.")
    @is_timecard_admin()
    async def createclock(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The user to create a time clock for."),  # type: ignore
        channel: discord.Option(str, default=None, description="A different channel for the clock."),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            employee_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return

        row = await db.fetchone(
            "SELECT clockChannelId, clockMessageId, archived FROM employee WHERE id = ?", (employee_id,)
        )
        if row is None:
            await ctx.respond(f"{user} is not in the employee database. Add them with /addemployee first.", ephemeral=self._eph(ctx))
            return
        if row["archived"]:
            await ctx.respond(
                f"{user} is archived (terminated). Reactivate them in Odoo (unarchive the employee) before creating a clock.",
                ephemeral=self._eph(ctx),
            )
            return
        if row["clockMessageId"] is not None:
            confirm = Confirm(user=ctx.user, timeout=180)
            await ctx.response.send_message(
                "This employee already has a time clock. Proceed and replace it?", view=confirm, ephemeral=self._eph(ctx)
            )
            await confirm.wait()
            if not confirm.value:
                await ctx.followup.send("Cancelled (or timed out).", ephemeral=self._eph(ctx))
                return
            await self._delete_clock_message(row["clockChannelId"], row["clockMessageId"])
            responded = True
        else:
            responded = False

        channel_obj = ctx.channel
        if channel is not None:
            try:
                channel_obj = self.bot.get_channel(int(channel[2:-1]))
            except (ValueError, IndexError):
                await ctx.respond(f"'{channel}' is not a valid channel mention.", ephemeral=self._eph(ctx))
                return

        await self.make_clock(employee_id, channel_obj)

        note = f"Clock created successfully for {user}."
        if responded:
            await ctx.followup.send(note, ephemeral=self._eph(ctx))
        else:
            await ctx.respond(note, ephemeral=self._eph(ctx))

    async def _delete_clock_message(self, channel_id, message_id):
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"[Clock] Could not delete old clock message: {e}")

    @discord.slash_command(name="deleteclock", description="Delete a clock embed for a user.")
    @is_timecard_admin()
    async def deleteclock(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The user whose time clock to delete"),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            employee_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return
        row = await db.fetchone(
            "SELECT clockChannelId, clockMessageId FROM employee WHERE id = ?", (employee_id,)
        )
        if row and row["clockMessageId"]:
            await self._delete_clock_message(row["clockChannelId"], row["clockMessageId"])
            await db.execute(
                "UPDATE employee SET clockChannelId = NULL, clockMessageId = NULL WHERE id = ?",
                (employee_id,),
            )
        await ctx.respond(f"Clock deleted for {user}.", ephemeral=self._eph(ctx))

    @discord.slash_command(name="refreshclock", description="Refresh your own time clock if the display looks stuck.")
    async def refreshclock(self, ctx: discord.ApplicationContext):
        db = await self._ensure_db()
        emp_id = ctx.author.id
        row = await db.fetchone(
            "SELECT clockChannelId, clockMessageId FROM employee WHERE id = ?", (emp_id,)
        )
        if row is None:
            await ctx.respond("You're not set up with a time clock yet — ask an admin to run /createclock.", ephemeral=True)
            return
        if row["clockChannelId"] and row["clockMessageId"]:
            try:
                msg = await self.obtain_message(row["clockChannelId"], row["clockMessageId"])
                await render_clock(self, msg, emp_id)
                await ctx.respond("Your time clock has been refreshed.", ephemeral=True)
                return
            except discord.NotFound:
                pass  # the message is gone -> re-create it below
            except Exception as e:  # noqa: BLE001
                log.exception("[Clock] refreshclock failed for %s", emp_id)
                await ctx.respond(f"Couldn't refresh your clock: {e}", ephemeral=True)
                return
        # No/missing clock message -> re-create it in its original channel.
        channel = self.bot.get_channel(row["clockChannelId"]) if row["clockChannelId"] else None
        await self.make_clock(emp_id, channel or ctx.channel)
        await ctx.respond("Your time clock message was missing, so I re-created it.", ephemeral=True)

    # ---- reports -----------------------------------------------------------

    async def week_ending_autocomplete(self, ctx: discord.AutocompleteContext):
        date_object = autofill_incomplete_date(str(ctx.value or "").lower())
        if not date_object:
            return []
        saturdays = get_closest_saturdays(date_object)
        filtered = [d for d in saturdays if str(ctx.value or "").lower() in d]
        return [discord.OptionChoice(d, value=d) for d in filtered[:25]]

    async def employee_group_autocomplete(self, ctx: discord.AutocompleteContext):
        if self.db is None:
            return []
        rows = await self.db.fetchall("SELECT name FROM employee_group")
        return [r["name"] for r in rows if str(ctx.value or "").lower() in r["name"].lower()]

    async def _gather_timecard(self, db, employee_ids, start, end):
        """Build (employees, punch_data, employee_data) for the given employees whose
        punch-in falls in [start, end). Shared by all report commands; legacy punches
        are INCLUDED (this is reporting, not sync)."""
        if not employee_ids:
            return [], {}, {}
        placeholders = ",".join("?" for _ in employee_ids)
        punches = await db.fetchall(
            f"""
            SELECT e.id, e.name, pc.id AS punch_id, pc.punchInTime, pc.punchOutTime,
                   pc.punchInApproval, pc.punchOutApproval, pc.ignoreLunchBreak
            FROM punch_clock pc
            JOIN employee e ON pc.employeeID = e.id
            WHERE pc.punchInTime BETWEEN ? AND ? AND e.id IN ({placeholders})
            ORDER BY e.name, pc.punchInTime
            """,
            tuple([str(start), str(end)] + list(employee_ids)),
        )
        punch_data: dict = {}
        employee_data: dict = {}
        for p in punches:
            name = p["name"]
            punch_tuple = (name, p["punch_id"], p["punchInTime"], p["punchOutTime"],
                           p["punchInApproval"], p["punchOutApproval"], p["ignoreLunchBreak"])
            work_rows = await db.fetchall(
                """
                SELECT wt.punchType, c.name, wt.timeSpent
                FROM work_time wt JOIN customer c ON wt.customerID = c.id
                WHERE wt.punchID = ? AND wt.detached = 0 ORDER BY wt.timeStarted
                """,
                (p["punch_id"],),
            )
            work_punches = [(w["punchType"], w["name"], w["timeSpent"]) for w in work_rows]
            if name not in punch_data:
                punch_data[name] = []
                emp = await db.fetchone(
                    "SELECT name, addressLine1, addressLine2, addressCity, addressState, "
                    "addressZip, phoneNumber FROM employee WHERE id = ?",
                    (p["id"],),
                )
                employee_data[name] = tuple(emp)
            punch_data[name].append((punch_tuple, work_punches))
        return list(punch_data.keys()), punch_data, employee_data

    @discord.slash_command(name="viewtimecard", description="View an employee's week of punches + worktime (with ids) to decide what to edit.")
    @is_timecard_admin()
    async def viewtimecard(
        self, ctx: discord.ApplicationContext,
        employee: discord.Option(str, description="The employee", autocomplete=employee_autocomplete),  # type: ignore
        week_end_date: discord.Option(str, default=None, description="Week-ending SATURDAY [YYYY-MM-DD]; defaults to the current week.", autocomplete=week_ending_autocomplete),  # type: ignore
    ):
        from datetime import datetime, timedelta
        db = await self._ensure_db()
        try:
            emp_id = int(employee[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{employee}' is not a valid user mention.", ephemeral=self._eph(ctx))
            return
        erow = await db.fetchone("SELECT name FROM employee WHERE id = ?", (emp_id,))
        if erow is None:
            await ctx.respond(f"{employee} isn't in the employee system.", ephemeral=self._eph(ctx))
            return
        if week_end_date:
            try:
                d = datetime.strptime(week_end_date, "%Y-%m-%d")
            except ValueError:
                await ctx.respond("Invalid date — use YYYY-MM-DD.", ephemeral=self._eph(ctx))
                return
        else:
            d = datetime.now()
        eow = d + timedelta(days=(5 - d.weekday()) % 7)  # snap to that week's ending Saturday
        embed = await build_timecard_embed(self, emp_id, erow["name"], eow)
        view = TimecardWeekView(self, emp_id, erow["name"], eow, ctx.user.id)
        await ctx.respond(embed=embed, view=view, ephemeral=self._eph(ctx))

    @discord.slash_command(name="timecardreport", description="Generate a weekly punch report given an end date.")
    @is_timecard_admin()
    async def timecardreport(
        self, ctx: discord.ApplicationContext,
        week_end_date: discord.Option(str, description="End of Week [SATURDAY, YYYY-MM-DD]", autocomplete=week_ending_autocomplete),  # type: ignore
        employee_group: discord.Option(str, description="Employee group to include", autocomplete=employee_group_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        await ctx.defer(ephemeral=True)
        try:
            from datetime import datetime, timedelta

            eow = datetime.strptime(week_end_date, "%Y-%m-%d")
            if not is_saturday(week_end_date):
                await ctx.respond(f"{week_end_date} is a {get_day_of_week(week_end_date)}, not a Saturday.", ephemeral=True)
                return
            week_start = eow - timedelta(days=6)
            week_end = eow + timedelta(days=1)

            group = await db.fetchone("SELECT id FROM employee_group WHERE name = ?", (employee_group,))
            if not group:
                await ctx.respond(f"Employee group '{employee_group}' not found.", ephemeral=True)
                return
            members = await db.fetchall("SELECT employeeID FROM group_member WHERE groupID = ?", (group["id"],))
            employee_ids = [m["employeeID"] for m in members]
            if not employee_ids:
                await ctx.respond(f"No employees in group '{employee_group}'.", ephemeral=True)
                return

            employees, punch_data, employee_data = await self._gather_timecard(
                db, employee_ids, week_start, week_end
            )
            if not employees:
                await ctx.respond(f"No punches for the week ending {week_end_date} in '{employee_group}'.", ephemeral=True)
                return

            safe_group = employee_group.strip().replace(" ", "_")
            file_path = f"reports/{safe_group}_Weekly_Report_{week_end_date}.xlsx"

            # openpyxl/xlsxwriter are blocking -> build off the event loop.
            await asyncio.to_thread(
                generate_timecard_report, file_path, employees, punch_data, employee_data, week_end_date
            )

            reports_channel = self.bot.get_channel(int(os.getenv("TIMECARD_REPORTS_CHANNEL_ID")))
            if reports_channel:
                await reports_channel.send(file=discord.File(file_path))
                timecard_log.info(f"[Report] {ctx.author} generated the weekly report for {week_end_date} ('{employee_group}') → reports channel.")
                await ctx.respond(f"Weekly report for {week_end_date} sent to the reports channel.", ephemeral=True)
            else:
                await ctx.respond("Report generated, but the reports channel was not found.", ephemeral=True)
        except ValueError:
            await ctx.respond("Invalid date format. Please use YYYY-MM-DD.", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            log.exception(f"[Report] error: {e}")
            await ctx.respond(f"An error occurred: {e}", ephemeral=True)

    @discord.slash_command(name="mytimecard", description="DM yourself your own timecard for a week.")
    async def mytimecard(
        self, ctx: discord.ApplicationContext,
        week_end_date: discord.Option(str, default=None, description="Week-ending SATURDAY [YYYY-MM-DD]; defaults to the most recent.", autocomplete=week_ending_autocomplete),  # type: ignore
    ):
        from datetime import datetime, timedelta
        db = await self._ensure_db()
        await ctx.defer(ephemeral=True)
        emp_id = ctx.author.id
        if await db.fetchone("SELECT id FROM employee WHERE id = ?", (emp_id,)) is None:
            await ctx.followup.send("You're not in the employee system.", ephemeral=True)
            return
        if not week_end_date:  # default to the most recent Saturday
            today = datetime.now()
            week_end_date = (today - timedelta(days=(today.weekday() - 5) % 7)).strftime("%Y-%m-%d")
        try:
            eow = datetime.strptime(week_end_date, "%Y-%m-%d")
        except ValueError:
            await ctx.followup.send("Invalid date — use YYYY-MM-DD (a Saturday).", ephemeral=True)
            return
        if not is_saturday(week_end_date):
            await ctx.followup.send(f"{week_end_date} is a {get_day_of_week(week_end_date)}, not a Saturday.", ephemeral=True)
            return
        week_start, week_end = eow - timedelta(days=6), eow + timedelta(days=1)
        employees, punch_data, employee_data = await self._gather_timecard(db, [emp_id], week_start, week_end)
        if not employees:
            await ctx.followup.send(f"You have no punches for the week ending {week_end_date}.", ephemeral=True)
            return
        os.makedirs("reports", exist_ok=True)
        file_path = f"reports/My_Timecard_{emp_id}_{week_end_date}.xlsx"
        await asyncio.to_thread(generate_timecard_report, file_path, employees, punch_data, employee_data, week_end_date)
        try:
            await ctx.author.send(content=f"Here's your timecard for the week ending {week_end_date}.",
                                  file=discord.File(file_path))
            timecard_log.info(f"[Report] {ctx.author} requested their own timecard for the week ending {week_end_date} (DM'd).")
            await ctx.followup.send("I've DM'd you your timecard. \U0001f4ec", ephemeral=True)
        except discord.Forbidden:  # DMs closed -> deliver ephemerally instead
            await ctx.followup.send(content="Your DMs are closed, so here it is (only you can see this):",
                                    file=discord.File(file_path), ephemeral=True)

    @discord.slash_command(name="timecardrange", description="Report over a custom date range (up to 1 year).")
    @is_timecard_admin()
    async def timecardrange(
        self, ctx: discord.ApplicationContext,
        start_date: discord.Option(str, description="Start date [YYYY-MM-DD]"),  # type: ignore
        end_date: discord.Option(str, description="End date [YYYY-MM-DD]"),  # type: ignore
        employee_group: discord.Option(str, default=None, description="Employee group (default: everyone)", autocomplete=employee_group_autocomplete),  # type: ignore
    ):
        from datetime import datetime, timedelta
        db = await self._ensure_db()
        await ctx.defer(ephemeral=True)
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
        except ValueError:
            await ctx.followup.send("Invalid date — use YYYY-MM-DD.", ephemeral=True)
            return
        if end < start:
            await ctx.followup.send("The end date is before the start date.", ephemeral=True)
            return
        if (end - start).days > 366:
            await ctx.followup.send("That range is over a year — please pick 1 year or less (Excel gets slow).", ephemeral=True)
            return
        if employee_group:
            group = await db.fetchone("SELECT id FROM employee_group WHERE name = ?", (employee_group,))
            if not group:
                await ctx.followup.send(f"Employee group '{employee_group}' not found.", ephemeral=True)
                return
            members = await db.fetchall("SELECT employeeID FROM group_member WHERE groupID = ?", (group["id"],))
            employee_ids = [m["employeeID"] for m in members]
            label_group = employee_group
        else:
            rows = await db.fetchall("SELECT id FROM employee WHERE archived = 0 OR archived IS NULL")
            employee_ids = [r["id"] for r in rows]
            label_group = "All"
        if not employee_ids:
            await ctx.followup.send("No employees to report on.", ephemeral=True)
            return
        employees, punch_data, employee_data = await self._gather_timecard(
            db, employee_ids, start, end + timedelta(days=1)  # inclusive of the end date
        )
        if not employees:
            await ctx.followup.send(f"No punches between {start_date} and {end_date}.", ephemeral=True)
            return
        period_label = f"{start_date} to {end_date}"
        os.makedirs("reports", exist_ok=True)
        safe = label_group.strip().replace(" ", "_")
        file_path = f"reports/{safe}_Range_{start_date}_to_{end_date}.xlsx"
        await asyncio.to_thread(generate_timecard_report, file_path, employees, punch_data, employee_data, period_label)
        chan_id = os.getenv("TIMECARD_REPORTS_CHANNEL_ID")
        reports_channel = self.bot.get_channel(int(chan_id)) if chan_id else None
        if reports_channel:
            await reports_channel.send(content=f"Timecard report: **{label_group}**, {period_label}.",
                                       file=discord.File(file_path))
            timecard_log.info(f"[Report] {ctx.author} generated a range report for {period_label} ('{label_group}') → reports channel.")
            await ctx.followup.send(f"Report for {period_label} sent to the reports channel.", ephemeral=True)
        else:
            await ctx.followup.send(content=f"Report for {period_label} (reports channel not set):",
                                    file=discord.File(file_path), ephemeral=True)

    @discord.slash_command(name="timecardexportdb", description="Send the timecard db file in chat.")
    async def timecardexportdb(self, ctx: discord.ApplicationContext):
        await self._ensure_db()
        if not has_perms(ctx.author, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await ctx.respond("You do not have permission to run this command.", ephemeral=True)
            return
        timecard_log.info(f"[Report] {ctx.author} exported the timecard database file.")
        await ctx.respond("Here you go!", file=discord.File(self.db_path), ephemeral=True)

    # Inbound Odoo changes are handled by the pull-based inbox worker
    # (cogs/timetracking/odoo/inbox.py), entered via enqueue_inbound() above.
