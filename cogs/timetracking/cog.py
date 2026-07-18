"""TimeTracking cog: slash commands, view re-attachment, inbound webhook apply.

Behaviour matches the original cog; the internals now use the async db layer,
render clock messages from DB state (never from embed text), and enqueue Odoo
sync jobs after local commits.
"""

from __future__ import annotations

import asyncio
import os

import discord
from discord.ext import commands

from botlog import log
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
from .views import ApprovePunch, render_clock


class TimeTracking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_path: str | None = None  # resolved (version-stamped) on first use
        self.db: Database | None = None
        self.client = OdooClient(
            os.getenv("ODOO_URL"), os.getenv("ODOO_DB"),
            os.getenv("ODOO_USERNAME"), os.getenv("ODOO_API_KEY"),
        )
        self.sync: sync.SyncWorker | None = None
        self.inbox: inbox.InboxWorker | None = None
        self._lock = asyncio.Lock()
        self._odoo_employees: list | None = None  # cached hr.employee list for autocomplete

    # ---- lifecycle ---------------------------------------------------------

    async def _ensure_db(self) -> Database:
        """Open + migrate the DB on first use (backs up existing data first)."""
        async with self._lock:
            if self.db is None:
                target, source = resolve_db_path(os.getcwd())
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
                self.sync = sync.SyncWorker(self.db, self.client)
                self.sync.start()
                self.inbox = inbox.InboxWorker(self)
                self.inbox.start()
        return self.db

    async def enqueue_inbound(self, model: str, odoo_id: int, action=None, write_uid=None):
        """Entry point for the webhook: queue an inbound Odoo change to reconcile."""
        await self._ensure_db()
        await inbox.enqueue_inbound(self.db, model, odoo_id, action, write_uid)

    async def obtain_message(self, channel_id: int, message_id: int) -> discord.Message:
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        return await channel.fetch_message(message_id)

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
        """Suggest Odoo hr.employee records. Cached per run to avoid hammering
        the API on every keystroke."""
        if not self.client.loaded:
            return []
        if self._odoo_employees is None:
            try:
                self._odoo_employees = await self.client.get_employee_list() or []
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Odoo] employee autocomplete fetch failed: {e}")
                return []
        term = ctx.value.lower()
        matches = [e for e in self._odoo_employees if term in e["display_name"].lower()]
        return [discord.OptionChoice(name=e["display_name"], value=e["id"]) for e in matches[:25]]

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
        user: discord.Option(str, description="The employee's Discord user"),  # type: ignore
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
        await db.execute("UPDATE employee SET odooId = ? WHERE id = ?", (odoo_employee, emp_id))
        await ctx.respond(f"Linked {row['name']} (<@{emp_id}>) to Odoo employee {odoo_employee}.", ephemeral=True)

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
            "SELECT clockChannelId, clockMessageId FROM employee WHERE id = ?", (employee_id,)
        )
        if row is None:
            await ctx.respond(f"{user} is not in the employee database. Add them with /addemployee first.", ephemeral=True)
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

        user_obj = await self.bot.fetch_user(employee_id)
        embed = discord.Embed(title="You are currently NOT clocked in.", color=discord.Colour.brand_red())
        embed.add_field(
            name="Wondering how to clock in?",
            value="Click the green clock-in button and watch the field turn green. "
            "To clock out, hit the red clock-out button. Simple as that!",
        )
        embed.set_footer(text=f"User: {user_obj.name}")
        message = await channel_obj.send(embed=embed)

        await db.execute(
            "UPDATE employee SET clockChannelId = ?, clockMessageId = ? WHERE id = ?",
            (message.channel.id, message.id, employee_id),
        )
        # render_clock reads the true state (clocked-in or not) from the DB.
        await render_clock(self, message, employee_id)

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
