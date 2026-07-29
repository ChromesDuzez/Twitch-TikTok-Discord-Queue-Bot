"""TimeTracking cog: slash commands, view re-attachment, inbound webhook apply.

Behaviour matches the original cog; the internals now use the async db layer,
render clock messages from DB state (never from embed text), and enqueue Odoo
sync jobs after local commits.
"""

import asyncio
import os

import discord
from discord.ext import commands

import config
from botlog import log, timecard_log
from .db import Database, RELEASE_VERSION, TARGET_VERSION, backup_database, resolve_db_path
from .modals import Confirm
from .odoo import inbox, sync
from .odoo.client import OdooClient
from .perms import has_perms
from .reports import (
    autofill_incomplete_date,
    generate_timecard_report,
    get_closest_saturdays,
    get_day_of_week,
    is_saturday,
)
from .views import ApprovePunch, DeleteApproval, render_clock


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
                # A test bot uses a separate db file so it can never touch prod's.
                prefix = "timetracker.test" if config.is_testing() else "timetracker"
                target, source = resolve_db_path(os.getcwd(), prefix)
                self.db_upgraded = source is not None
                if source is not None:
                    # Upgrading an older/legacy db: back it up, then rename it to
                    # the current version-stamped name so migrations bring it up.
                    backup_database(source)
                    log.info(
                        f"[DB] Upgrading {os.path.basename(source)} -> "
                        f"{os.path.basename(target)} (schema v{TARGET_VERSION}, release {RELEASE_VERSION})."
                    )
                    os.rename(source, target)
                self.db_path = target
                self.db = await Database(target).setup(
                    company_name=os.getenv("COMPANY_NAME"),
                    debug=bool(os.getenv("DEBUGGING")),
                )
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
            "SELECT archived, clockChannelId, clockMessageId FROM employee WHERE id = ?",
            (employee_id,),
        )
        if row is None or bool(row["archived"]) == archived:
            return False
        if archived:
            if row["clockMessageId"]:
                await self._delete_clock_message(row["clockChannelId"], row["clockMessageId"])
            await db.execute(
                "UPDATE employee SET archived = 1, clockChannelId = NULL, clockMessageId = NULL WHERE id = ?",
                (employee_id,),
            )
            timecard_log.info(f"[Employee] Archived employee {employee_id}; clock removed, history kept.")
        else:
            await db.execute("UPDATE employee SET archived = 0 WHERE id = ?", (employee_id,))
            timecard_log.info(f"[Employee] Reactivated employee {employee_id}; recreate their clock with /createclock.")
        return True

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
    @commands.has_permissions(administrator=True)
    async def addcustomer(
        self, ctx: discord.ApplicationContext,
        name: discord.Option(str, description="Full Name of Customer"),  # type: ignore
    ):
        db = await self._ensure_db()
        existing = await db.fetchall("SELECT id FROM customer WHERE name = ?", (name,))
        if existing:
            await ctx.respond(f"'{name}' already exists in the customer table.", ephemeral=True)
            return
        customer_id = await db.execute("INSERT INTO customer (name) VALUES (?)", (name,))
        await sync.enqueue(db, "customer", customer_id, "create")
        await ctx.respond(f"Successfully inserted {name} into the customer table.", ephemeral=True)

    @discord.slash_command(name="editcustomer", description="Edit an existing Customer in the customer table.")
    @commands.has_permissions(administrator=True)
    async def editcustomer(
        self, ctx: discord.ApplicationContext,
        newname: discord.Option(str, description="New name for Customer"),  # type: ignore
        id: discord.Option(int, default=None, description="Id of Customer"),  # type: ignore
        name: discord.Option(str, default=None, description="Name of Customer"),  # type: ignore
    ):
        db = await self._ensure_db()
        if id is None and name is None:
            await ctx.respond("You must provide either id or name.", ephemeral=True)
            return
        if id is not None:
            row = await db.fetchone("SELECT id, name FROM customer WHERE id = ?", (id,))
        else:
            rows = await db.fetchall("SELECT id, name FROM customer WHERE name = ?", (name,))
            if len(rows) != 1:
                await ctx.respond(f"Search for '{name}' returned {len(rows)} results; be more specific.", ephemeral=True)
                return
            row = rows[0]
        if row is None:
            await ctx.respond("Could not find that customer.", ephemeral=True)
            return
        await db.execute("UPDATE customer SET name = ? WHERE id = ?", (newname, row["id"]))
        await ctx.respond(f"Updated customer {row['id']} ({row['name']}) to {newname}.", ephemeral=True)

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
        term = ctx.value.lower()
        matches = [e for e in self._odoo_employees
                   if e["id"] not in linked and term in e["display_name"].lower()]
        return [discord.OptionChoice(name=e["display_name"], value=e["id"]) for e in matches[:25]]

    async def linked_employee_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local employees currently linked to an Odoo employee (for /unlinkemployee).
        The choice value is a mention so it parses like a manually-typed user."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name, odooId FROM employee WHERE odooId IS NOT NULL ORDER BY name"
        )
        term = ctx.value.lower()
        out = []
        for r in rows:
            if term in r["name"].lower() or term in str(r["odooId"]):
                out.append(discord.OptionChoice(name=f"{r['name']} (Odoo #{r['odooId']})"[:100],
                                                value=f"<@{r['id']}>"))
        return out[:25]

    async def unlinked_employee_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local employees not yet linked to an Odoo employee (for /linkemployee).
        The choice value is a mention so it parses like a manually-typed user."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name FROM employee WHERE odooId IS NULL ORDER BY name"
        )
        term = ctx.value.lower()
        out = [discord.OptionChoice(name=r["name"][:100], value=f"<@{r['id']}>")
               for r in rows if term in r["name"].lower()]
        return out[:25]

    async def unlinked_customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local customers not yet linked to an Odoo partner (value = local id)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name FROM customer WHERE odooId IS NULL "
            "AND (archived IS NULL OR archived = 0) ORDER BY name"
        )
        term = ctx.value.lower()
        return [discord.OptionChoice(name=r["name"][:100], value=r["id"])
                for r in rows if term in r["name"].lower()][:25]

    async def linked_customer_autocomplete(self, ctx: discord.AutocompleteContext):
        """Local customers currently linked to an Odoo partner (value = local id)."""
        if self.db is None:
            return []
        rows = await self.db.fetchall(
            "SELECT id, name, odooId FROM customer WHERE odooId IS NOT NULL ORDER BY name"
        )
        term = ctx.value.lower()
        return [discord.OptionChoice(name=f"{r['name']} (Odoo #{r['odooId']})"[:100], value=r["id"])
                for r in rows if term in r["name"].lower() or term in str(r["odooId"])][:25]

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
        term = ctx.value.lower()
        matches = [c for c in self._odoo_customers
                   if c["id"] not in linked and term in c["display_name"].lower()]
        return [discord.OptionChoice(name=c["display_name"][:100], value=c["id"]) for c in matches[:25]]

    @discord.slash_command(name="addemployee", description="Add a new Employee to the system.")
    @commands.has_permissions(administrator=True)
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
                await ctx.respond("User override was improperly formatted.", ephemeral=True)
                return

        existing = await db.fetchall("SELECT id FROM employee WHERE id = ?", (emp_id,))
        if existing:
            await ctx.respond(f"{name} (<@{emp_id}>) already exists in the database.", ephemeral=True)
            return
        if odoo_employee is not None:
            clash = await db.fetchone("SELECT id, name FROM employee WHERE odooId = ?", (odoo_employee,))
            if clash is not None:
                await ctx.respond(
                    f"Odoo employee {odoo_employee} is already linked to {clash['name']} (<@{clash['id']}>). "
                    f"Unlink them first with /unlinkemployee.", ephemeral=True,
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
            await ctx.respond(f"Added new employee {name} (<@{emp_id}>).")
        except Exception as e:  # noqa: BLE001
            await ctx.respond(f"Error adding employee {name}: {e}", ephemeral=True)

    @addemployee.error
    async def addemployee_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.respond("You do not have permission to use this command.", ephemeral=True)

    @discord.slash_command(name="linkemployee", description="Link an existing employee to their Odoo hr.employee record.")
    @commands.has_permissions(administrator=True)
    async def linkemployee(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The employee to link", autocomplete=unlinked_employee_autocomplete),  # type: ignore
        odoo_employee: discord.Option(int, description="Odoo employee", autocomplete=odoo_employee_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            emp_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=True)
            return
        row = await db.fetchone("SELECT name FROM employee WHERE id = ?", (emp_id,))
        if row is None:
            await ctx.respond(f"{user} is not in the employee database. Add them with /addemployee first.", ephemeral=True)
            return
        # Linking is one-to-one: refuse an Odoo employee already linked elsewhere.
        clash = await db.fetchone(
            "SELECT id, name FROM employee WHERE odooId = ? AND id != ?", (odoo_employee, emp_id)
        )
        if clash is not None:
            await ctx.respond(
                f"Odoo employee {odoo_employee} is already linked to {clash['name']} (<@{clash['id']}>). "
                f"Unlink them first with /unlinkemployee.", ephemeral=True,
            )
            return
        await db.execute("UPDATE employee SET odooId = ? WHERE id = ?", (odoo_employee, emp_id))
        timecard_log.info(f"[Employee] {ctx.author} linked employee {emp_id} to Odoo employee {odoo_employee}.")
        await ctx.respond(f"Linked {row['name']} (<@{emp_id}>) to Odoo employee {odoo_employee}.", ephemeral=True)

    @discord.slash_command(name="unlinkemployee", description="Remove an employee's link to their Odoo hr.employee record.")
    @commands.has_permissions(administrator=True)
    async def unlinkemployee(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The linked employee to unlink", autocomplete=linked_employee_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            emp_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=True)
            return
        row = await db.fetchone("SELECT name, odooId FROM employee WHERE id = ?", (emp_id,))
        if row is None:
            await ctx.respond(f"{user} is not in the employee database.", ephemeral=True)
            return
        if row["odooId"] is None:
            await ctx.respond(f"{row['name']} (<@{emp_id}>) isn't linked to an Odoo employee.", ephemeral=True)
            return
        await db.execute("UPDATE employee SET odooId = NULL WHERE id = ?", (emp_id,))
        timecard_log.info(f"[Employee] {ctx.author} unlinked employee {emp_id} from Odoo employee {row['odooId']}.")
        await ctx.respond(
            f"Unlinked {row['name']} (<@{emp_id}>) from Odoo employee {row['odooId']}. "
            f"That Odoo employee is now available to link again; new punches won't sync until re-linked.",
            ephemeral=True,
        )

    # ---- customer <-> Odoo linking -----------------------------------------

    @discord.slash_command(name="synccustomers", description="Auto-link local customers to Odoo partners by exact name.")
    @commands.has_permissions(administrator=True)
    async def synccustomers(self, ctx: discord.ApplicationContext):
        if not self.client.loaded:
            await ctx.respond("Odoo isn't configured, so there's nothing to link to.", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        db = await self._ensure_db()
        try:
            odoo_customers = await self.client.get_customer_list() or []
        except Exception as e:  # noqa: BLE001
            log.exception("[Customer] synccustomers: Odoo fetch failed")
            await ctx.followup.send(f"Couldn't fetch Odoo customers: {e}", ephemeral=True)
            return
        # Case-insensitive Odoo name -> [partner ids]. One Odoo call, match in memory.
        by_name: dict[str, list[int]] = {}
        for c in odoo_customers:
            by_name.setdefault((c["display_name"] or "").strip().lower(), []).append(c["id"])
        already = {r["odooId"] for r in await db.fetchall("SELECT odooId FROM customer WHERE odooId IS NOT NULL")}
        unlinked = await db.fetchall(
            "SELECT id, name FROM customer WHERE odooId IS NULL AND (archived IS NULL OR archived = 0)"
        )
        linked = ambiguous = nomatch = 0
        for c in unlinked:
            candidates = [i for i in by_name.get((c["name"] or "").strip().lower(), []) if i not in already]
            if len(candidates) == 1:
                await db.execute("UPDATE customer SET odooId = ? WHERE id = ?", (candidates[0], c["id"]))
                already.add(candidates[0]); linked += 1
            elif len(candidates) > 1:
                ambiguous += 1
            else:
                nomatch += 1
        timecard_log.info(f"[Customer] {ctx.author} synccustomers: {linked} linked, {ambiguous} ambiguous, {nomatch} no-match.")
        await ctx.followup.send(
            f"Auto-link complete: **{linked}** linked by exact name. "
            f"**{ambiguous + nomatch}** still unlinked ({ambiguous} ambiguous, {nomatch} no match) — "
            f"link those with `/linkcustomer` (see `/unlinkedcustomers`).",
            ephemeral=True,
        )

    @discord.slash_command(name="linkcustomer", description="Link a local customer to their Odoo partner.")
    @commands.has_permissions(administrator=True)
    async def linkcustomer(
        self, ctx: discord.ApplicationContext,
        customer: discord.Option(int, description="Local customer", autocomplete=unlinked_customer_autocomplete),  # type: ignore
        odoo_partner: discord.Option(int, description="Odoo customer", autocomplete=odoo_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        row = await db.fetchone("SELECT id, name FROM customer WHERE id = ?", (customer,))
        if row is None:
            await ctx.respond("That customer isn't in the database.", ephemeral=True)
            return
        clash = await db.fetchone("SELECT name FROM customer WHERE odooId = ? AND id != ?", (odoo_partner, customer))
        if clash is not None:
            await ctx.respond(
                f"Odoo partner {odoo_partner} is already linked to '{clash['name']}'. "
                f"Unlink it first with /unlinkcustomer.", ephemeral=True,
            )
            return
        await db.execute("UPDATE customer SET odooId = ? WHERE id = ?", (odoo_partner, customer))
        timecard_log.info(f"[Customer] {ctx.author} linked customer {customer} ({row['name']}) to Odoo partner {odoo_partner}.")
        await ctx.respond(f"Linked '{row['name']}' to Odoo partner {odoo_partner}.", ephemeral=True)

    @discord.slash_command(name="unlinkcustomer", description="Remove a customer's link to their Odoo partner.")
    @commands.has_permissions(administrator=True)
    async def unlinkcustomer(
        self, ctx: discord.ApplicationContext,
        customer: discord.Option(int, description="Linked customer", autocomplete=linked_customer_autocomplete),  # type: ignore
    ):
        db = await self._ensure_db()
        row = await db.fetchone("SELECT name, odooId FROM customer WHERE id = ?", (customer,))
        if row is None:
            await ctx.respond("That customer isn't in the database.", ephemeral=True)
            return
        if row["odooId"] is None:
            await ctx.respond(f"'{row['name']}' isn't linked to an Odoo partner.", ephemeral=True)
            return
        await db.execute("UPDATE customer SET odooId = NULL WHERE id = ?", (customer,))
        timecard_log.info(f"[Customer] {ctx.author} unlinked customer {customer} ({row['name']}) from Odoo partner {row['odooId']}.")
        await ctx.respond(f"Unlinked '{row['name']}' from Odoo partner {row['odooId']}.", ephemeral=True)

    @discord.slash_command(name="unlinkedcustomers", description="List local customers not yet linked to an Odoo partner.")
    @commands.has_permissions(administrator=True)
    async def unlinkedcustomers(self, ctx: discord.ApplicationContext):
        db = await self._ensure_db()
        rows = await db.fetchall(
            "SELECT name FROM customer WHERE odooId IS NULL AND (archived IS NULL OR archived = 0) ORDER BY name"
        )
        if not rows:
            await ctx.respond("All customers are linked to Odoo. \U0001f389", ephemeral=True)
            return
        names = [r["name"] for r in rows]
        preview = "\n".join(f"• {n}" for n in names[:40])
        more = f"\n…and {len(names) - 40} more." if len(names) > 40 else ""
        await ctx.respond(f"**{len(names)}** unlinked customer(s):\n{preview}{more}\n\nLink them with /linkcustomer.", ephemeral=True)

    # ---- clock commands ----------------------------------------------------

    @discord.slash_command(name="createclock", description="Create a time clock embed for a user.")
    @commands.has_permissions(administrator=True)
    async def createclock(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The user to create a time clock for."),  # type: ignore
        channel: discord.Option(str, default=None, description="A different channel for the clock."),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            employee_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=True)
            return

        row = await db.fetchone(
            "SELECT clockChannelId, clockMessageId, archived FROM employee WHERE id = ?", (employee_id,)
        )
        if row is None:
            await ctx.respond(f"{user} is not in the employee database. Add them with /addemployee first.", ephemeral=True)
            return
        if row["archived"]:
            await ctx.respond(
                f"{user} is archived (terminated). Reactivate them in Odoo (unarchive the employee) before creating a clock.",
                ephemeral=True,
            )
            return
        if row["clockMessageId"] is not None:
            confirm = Confirm(user=ctx.user, timeout=180)
            await ctx.response.send_message(
                "This employee already has a time clock. Proceed and replace it?", view=confirm, ephemeral=True
            )
            await confirm.wait()
            if not confirm.value:
                await ctx.followup.send("Cancelled (or timed out).", ephemeral=True)
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
                await ctx.respond(f"'{channel}' is not a valid channel mention.", ephemeral=True)
                return

        await self.make_clock(employee_id, channel_obj)

        note = f"Clock created successfully for {user}."
        if responded:
            await ctx.followup.send(note, ephemeral=True)
        else:
            await ctx.respond(note, ephemeral=True)

    async def _delete_clock_message(self, channel_id, message_id):
        try:
            channel = self.bot.get_channel(channel_id)
            if channel is not None:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"[Clock] Could not delete old clock message: {e}")

    @discord.slash_command(name="deleteclock", description="Delete a clock embed for a user.")
    @commands.has_permissions(administrator=True)
    async def deleteclock(
        self, ctx: discord.ApplicationContext,
        user: discord.Option(str, description="The user whose time clock to delete"),  # type: ignore
    ):
        db = await self._ensure_db()
        try:
            employee_id = int(user[2:-1])
        except (ValueError, IndexError):
            await ctx.respond(f"'{user}' is not a valid user mention.", ephemeral=True)
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
        await ctx.respond(f"Clock deleted for {user}.", ephemeral=True)

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
        date_object = autofill_incomplete_date(ctx.value.lower())
        if not date_object:
            return []
        saturdays = get_closest_saturdays(date_object)
        filtered = [d for d in saturdays if ctx.value.lower() in d]
        return [discord.OptionChoice(d, value=d) for d in filtered[:25]]

    async def employee_group_autocomplete(self, ctx: discord.AutocompleteContext):
        if self.db is None:
            return []
        rows = await self.db.fetchall("SELECT name FROM employee_group")
        return [r["name"] for r in rows if ctx.value.lower() in r["name"].lower()]

    @discord.slash_command(name="timecardreport", description="Generate a weekly punch report given an end date.")
    @commands.has_permissions(administrator=True)
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
                tuple([str(week_start), str(week_end)] + employee_ids),
            )
            if not punches:
                await ctx.respond(f"No punches for the week ending {week_end_date} in '{employee_group}'.", ephemeral=True)
                return

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
                    WHERE wt.punchID = ? ORDER BY wt.timeStarted
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

            employees = list(punch_data.keys())
            safe_group = employee_group.strip().replace(" ", "_")
            file_path = f"reports/{safe_group}_Weekly_Report_{week_end_date}.xlsx"

            # openpyxl/xlsxwriter are blocking -> build off the event loop.
            await asyncio.to_thread(
                generate_timecard_report, file_path, employees, punch_data, employee_data, week_end_date
            )

            reports_channel = self.bot.get_channel(int(os.getenv("TIMECARD_REPORTS_CHANNEL_ID")))
            if reports_channel:
                await reports_channel.send(file=discord.File(file_path))
                await ctx.respond(f"Weekly report for {week_end_date} sent to the reports channel.", ephemeral=True)
            else:
                await ctx.respond("Report generated, but the reports channel was not found.", ephemeral=True)
        except ValueError:
            await ctx.respond("Invalid date format. Please use YYYY-MM-DD.", ephemeral=True)
        except Exception as e:  # noqa: BLE001
            log.exception(f"[Report] error: {e}")
            await ctx.respond(f"An error occurred: {e}", ephemeral=True)

    @discord.slash_command(name="timecardexportdb", description="Send the timecard db file in chat.")
    async def timecardexportdb(self, ctx: discord.ApplicationContext):
        await self._ensure_db()
        if not has_perms(ctx.author, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await ctx.respond("You do not have permission to run this command.", ephemeral=True)
            return
        await ctx.respond("Here you go!", file=discord.File(self.db_path), ephemeral=True)

    # Inbound Odoo changes are handled by the pull-based inbox worker
    # (cogs/timetracking/odoo/inbox.py), entered via enqueue_inbound() above.
