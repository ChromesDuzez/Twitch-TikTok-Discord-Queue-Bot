"""Clock and approval views.

The central idea: **the database is the single source of truth**. Every button
re-reads :func:`state.load_state` before acting, and every state change ends by
calling :func:`render_clock`, which rebuilds both the embed and the buttons
from the DB. This replaces the old design that reconstructed clock state by
string-matching embed text (the cause of views desyncing on restart).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import discord

from botlog import log, timecard_log
from .db import Database, db_dir
from .modals import (
    CustomerInputModal, CustomerSelectMenu, DeleteShiftConfirmModal,
    EditPunchTimeModal, GetTimeSpent,
)
from .perms import CLOCK_ROLES, has_perms
from .odoo import sync
from .costing import punch_span
from .state import ClockState, _as_bool, load_state

CLOCK_ICON = (
    "https://media.discordapp.net/attachments/1224574847213109330/1244848933675728978/"
    "clkfbambooblack600x600-bgf8f8f8.png"
)


def _project_env(name: str) -> int | None:
    value = os.getenv(name)
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _intended(employee_id: int) -> discord.Object:
    """Lightweight stand-in with an ``.id`` for permission checks."""
    return discord.Object(id=employee_id)


def _can_operate(member: discord.Member, employee_id: int) -> bool:
    return has_perms(member, _intended(employee_id), accepted_roles=CLOCK_ROLES)


def _is_standard_actor(member: discord.Member) -> bool:
    """Whether a punch by this member is auto-approved (no admin review)."""
    roles = getattr(member, "roles", [])
    if any(getattr(r.permissions, "administrator", False) for r in roles):
        return True
    rid = os.getenv("TIMECARD_TIMECLOCK_ROLE_ID")
    if rid and discord.utils.get(roles, id=int(rid)):
        return True
    return False


# ---- rendering -------------------------------------------------------------

async def _employee_name(db: Database, employee_id: int) -> str:
    row = await db.fetchone("SELECT name FROM employee WHERE id = ?", (employee_id,))
    return row["name"] if row else str(employee_id)


def build_clock_embed(existing: discord.Embed | None, state: ClockState, name: str) -> discord.Embed:
    if existing is not None:
        embed = existing
    else:
        embed = discord.Embed()
        embed.add_field(
            name="Wondering how to clock in?",
            value="Click the green clock-in button and watch the field turn green. "
            "To clock out, hit the red clock-out button. Simple as that!",
        )
        embed.set_author(name=f"{name} Time Clock", icon_url=CLOCK_ICON)

    if state.clocked_in:
        embed.color = discord.Colour.brand_green()
        embed.title = "You ARE currently clocked in."
        embed.set_footer(text=f"{name} • punch #{state.current_punch}")
    else:
        embed.color = discord.Colour.brand_red()
        embed.title = "You are currently NOT clocked in."
        embed.set_footer(text=f"User: {name}")
    return embed


async def render_clock(cog, message: discord.Message, employee_id: int):
    """Re-read DB state and rebuild the message's embed + buttons in one edit."""
    state = await load_state(cog.db, employee_id)
    name = await _employee_name(cog.db, employee_id)
    existing = message.embeds[0] if message.embeds else None
    embed = build_clock_embed(existing, state, name)
    view = ClockView(cog, employee_id, message, state)
    await message.edit(embed=embed, view=view)


# ---- clock view ------------------------------------------------------------

class ClockView(discord.ui.View):
    def __init__(self, cog, employee_id: int, message: discord.Message, state: ClockState):
        super().__init__(timeout=None)
        self.cog = cog
        self.db: Database = cog.db
        self.employee_id = employee_id
        self.message = message
        self.state = state

        if not state.clocked_in:
            self.add_item(ClockInButton(employee_id))
        elif state.open_worktime:
            self.add_item(EndWorkButton(state.open_worktime_type, custom=False))
            self.add_item(EndWorkButton(state.open_worktime_type, custom=True))
        else:
            self.add_item(ClockOutButton(employee_id))
            if state.allow_construction:
                self.add_item(StartWorkButton("Construction"))
            if state.allow_service:
                self.add_item(StartWorkButton("Service"))
            if state.allow_office:
                self.add_item(StartWorkButton("Office"))
            if state.lunch_skipable:
                self.add_item(IgnoreLunchButton(state.ignore_lunch))

        # Always-visible self-service refresh (own row) so an employee can fix a
        # stale display themselves.
        self.add_item(RefreshButton(employee_id))

    # shared guard
    async def _guard(self, interaction: discord.Interaction) -> bool:
        if not _can_operate(interaction.user, self.employee_id):
            timecard_log.info(
                f"[Clock] {interaction.user} pressed a button on employee "
                f"{self.employee_id}'s clock — denied (not theirs)."
            )
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return False
        return True


class RefreshButton(discord.ui.Button):
    """Re-render the clock from DB state. Persistent (survives restarts) and on its
    own row so it's always available if the display goes stale."""
    def __init__(self, employee_id: int):
        super().__init__(label="Refresh", emoji="🔄", style=discord.ButtonStyle.secondary,
                         custom_id=f"clock:{employee_id}:refresh", row=1)

    async def callback(self, interaction: discord.Interaction):
        view: ClockView = self.view
        if not await view._guard(interaction):
            return
        await interaction.response.defer()
        await render_clock(view.cog, view.message, view.employee_id)
        timecard_log.info(f"[Clock] {interaction.user} refreshed employee {view.employee_id}'s clock.")


class ClockInButton(discord.ui.Button):
    def __init__(self, employee_id: int):
        super().__init__(label="Clock-In", style=discord.ButtonStyle.green,
                         custom_id=f"clock:{employee_id}:in")

    async def callback(self, interaction: discord.Interaction):
        view: ClockView = self.view
        if not await view._guard(interaction):
            return
        db = view.db
        state = await load_state(db, view.employee_id)
        if state.clocked_in:
            await interaction.response.send_message("You are already clocked in!", ephemeral=True)
            return

        await interaction.response.defer()

        # Desync guard: Discord shows clocked-out, but if Odoo already has an OPEN
        # attendance for this employee (a missed update), adopt that shift instead of
        # creating a duplicate/overlapping attendance. Never blocks clock-in on an
        # Odoo hiccup — on any error we fall through to the normal path.
        if view.cog.client.loaded:
            emp = await db.fetchone("SELECT odooId FROM employee WHERE id = ?", (view.employee_id,))
            if emp and emp["odooId"]:
                open_att = None
                try:
                    open_att = await view.cog.client.find_open_attendance(emp["odooId"])
                except Exception as e:  # noqa: BLE001
                    log.warning(f"[Clock] open-attendance check failed for employee {view.employee_id}: {e}")
                if open_att:
                    if view.cog.inbox is not None:
                        try:
                            await view.cog.inbox._reconcile_attendance(open_att["id"])  # import as local shift
                        except Exception as e:  # noqa: BLE001
                            log.warning(f"[Clock] adopting open attendance {open_att['id']} failed: {e}")
                    await render_clock(view.cog, view.message, view.employee_id)
                    timecard_log.info(
                        f"[Clock] {interaction.user}'s clock-in for employee {view.employee_id} found an open "
                        f"Odoo attendance ({open_att['id']}) — adopted it, no duplicate created.")
                    await interaction.followup.send(
                        "You're already clocked in (synced from Odoo).", ephemeral=True)
                    return

        now_str = sync.now_local_str()
        approved = _is_standard_actor(interaction.user)

        approval_message = None
        if not approved:
            admin_ch = view.cog.bot.get_channel(int(os.getenv("TIMECARD_ADMIN_CHANNEL_ID")))
            approval_message = await admin_ch.send(
                f"<@{view.employee_id}> attempted to login today at {now_str} in a "
                f"non-standard way.\nDo you approve of this login attempt?"
            )

        if approval_message is not None:
            punch_id = await db.execute(
                "INSERT INTO punch_clock (employeeID, punchInTime, punchInApproval, "
                "checkChannelId, checkMessageId) VALUES (?, ?, 0, ?, ?)",
                (view.employee_id, now_str, approval_message.channel.id, approval_message.id),
            )
            await approval_message.edit(view=await ApprovePunch.create(view.cog, punch_id, approval_message))
        else:
            punch_id = await db.execute(
                "INSERT INTO punch_clock (employeeID, punchInTime, punchInApproval) VALUES (?, ?, 1)",
                (view.employee_id, now_str),
            )

        await sync.enqueue(db, "punch", punch_id, "in")
        await render_clock(view.cog, view.message, view.employee_id)
        erow = await db.fetchone("SELECT name FROM employee WHERE id = ?", (view.employee_id,))
        ename = erow["name"] if erow else f"employee {view.employee_id}"
        timecard_log.info(
            f"[Clock] {interaction.user} clocked IN {ename} (employee {view.employee_id}) at {now_str} "
            f"(punch {punch_id}, auto-approved={approved})."
        )
        await interaction.followup.send("You clocked in.", ephemeral=True)


class ClockOutButton(discord.ui.Button):
    def __init__(self, employee_id: int):
        super().__init__(label="Clock-Out", style=discord.ButtonStyle.red,
                         custom_id=f"clock:{employee_id}:out")

    async def callback(self, interaction: discord.Interaction):
        view: ClockView = self.view
        if not await view._guard(interaction):
            return
        db = view.db
        state = await load_state(db, view.employee_id)
        if not state.clocked_in:
            await interaction.response.send_message("You are already clocked out!", ephemeral=True)
            return

        await interaction.response.defer()
        punch_id = state.current_punch
        now_str = sync.now_local_str()
        approved = _is_standard_actor(interaction.user)

        # Reuse an existing approval message (from an unapproved clock-in) if present.
        existing = await db.fetchone(
            "SELECT checkChannelId, checkMessageId FROM punch_clock WHERE id = ?", (punch_id,)
        )
        approval_message = None
        if not approved:
            if existing and existing["checkChannelId"] and existing["checkMessageId"]:
                approval_message = await view.cog.obtain_message(
                    int(existing["checkChannelId"]), int(existing["checkMessageId"])
                )
                await approval_message.edit(
                    content=approval_message.content
                    + f"\n<@{view.employee_id}> also attempted to logout at {now_str} in a non-standard way."
                )
            else:
                admin_ch = view.cog.bot.get_channel(int(os.getenv("TIMECARD_ADMIN_CHANNEL_ID")))
                approval_message = await admin_ch.send(
                    f"<@{view.employee_id}> attempted to logout today at {now_str} in a "
                    f"non-standard way.\nDo you approve of this logout attempt?"
                )

        if approval_message is not None:
            await db.execute(
                "UPDATE punch_clock SET punchOutTime = ?, punchOutApproval = 0, "
                "checkChannelId = ?, checkMessageId = ? WHERE id = ?",
                (now_str, approval_message.channel.id, approval_message.id, punch_id),
            )
            await approval_message.edit(view=await ApprovePunch.create(view.cog, punch_id, approval_message))
        else:
            await db.execute(
                "UPDATE punch_clock SET punchOutTime = ?, punchOutApproval = 1 WHERE id = ?",
                (now_str, punch_id),
            )

        await sync.enqueue(db, "punch", punch_id, "out")
        await render_clock(view.cog, view.message, view.employee_id)
        erow = await db.fetchone("SELECT name FROM employee WHERE id = ?", (view.employee_id,))
        ename = erow["name"] if erow else f"employee {view.employee_id}"
        timecard_log.info(
            f"[Clock] {interaction.user} clocked OUT {ename} (employee {view.employee_id}) at {now_str} "
            f"(punch {punch_id}, auto-approved={approved})."
        )
        await interaction.followup.send("You clocked out.", ephemeral=True)


class StartWorkButton(discord.ui.Button):
    def __init__(self, punch_type: str):
        self.punch_type = punch_type
        # Cache of the last search's results, keyed by str(odoo id), so the
        # select callback can recover the task/project/partner it chose.
        self._results: dict[str, dict] = {}
        super().__init__(label=f"Start {punch_type}", style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        view: ClockView = self.view
        if not await view._guard(interaction):
            return
        state = await load_state(view.db, view.employee_id)
        if not state.clocked_in:
            await interaction.response.send_message(
                "You are clocked out and can't start work until you clock in!", ephemeral=True
            )
            return

        if self.punch_type == "Office":
            # Office desk work: no customer/task. Link to the dedicated Office
            # project when configured (else it stays local-only).
            await interaction.response.defer()
            office_pid = _project_env("ODOO_OFFICE_PROJECT_ID") if view.cog.client.loaded else None
            await self._create_worktime(interaction, customer_id=0, task_id=None, project_id=office_pid)
            return

        if view.cog.client.loaded:
            # Odoo online: search real Odoo work items (tasks/projects).
            await interaction.response.send_modal(CustomerInputModal(on_submit=self._odoo_search))
        else:
            # Odoo offline: fall back to the local customer list.
            await interaction.response.send_modal(CustomerInputModal(on_submit=self._search_customers))

    # ---- Odoo online search -----------------------------------------------

    async def _odoo_search(self, interaction: discord.Interaction, term: str):
        view: ClockView = self.view
        client = view.cog.client
        self._results = {}

        if self.punch_type == "Construction":
            rows = await client.search_construction_projects(
                term, exclude_ids=[_project_env("ODOO_FIELD_SERVICE_PROJECT_ID"),
                                    _project_env("ODOO_OFFICE_PROJECT_ID")],
            ) or []
            for r in rows:
                partner = r.get("partner_id")
                self._results[str(r["id"])] = {
                    "task_id": None, "project_id": r["id"],
                    "partner_name": partner[1] if partner else "",
                }
            noun = "construction projects"
        else:  # Service
            fs_id = _project_env("ODOO_FIELD_SERVICE_PROJECT_ID")
            if not fs_id:
                await interaction.response.send_message(
                    "The Field Service project isn't configured yet. Ask an admin to set it "
                    "with `/configureprojects`.", ephemeral=True)
                return
            months = _project_env("ODOO_TASK_WINDOW_MONTHS") or 6
            rows = await client.search_service_tasks(term, fs_id, months=months) or []
            for r in rows:
                proj = r.get("project_id")
                partner = r.get("partner_id")
                self._results[str(r["id"])] = {
                    "task_id": r["id"], "project_id": proj[0] if proj else fs_id,
                    "partner_name": partner[1] if partner else "",
                }
            noun = "service tasks"

        if not self._results:
            await interaction.response.send_message(
                f"No open {noun} found for '{term}'.", ephemeral=True)
            return

        options = []
        for r in rows:
            label = str(r["display_name"])[:100]
            options.append(discord.SelectOption(label=label, value=str(r["id"])))
        picker = discord.ui.View()
        picker.add_item(CustomerSelectMenu(options=options, on_select=self._odoo_selected))
        await interaction.response.send_message(f"Select a job ({noun}):", view=picker, ephemeral=True)

    async def _odoo_selected(self, interaction: discord.Interaction, selected_id: int):
        info = self._results.get(str(selected_id))
        if not info:
            await interaction.followup.send("That selection is no longer available.", ephemeral=True)
            return
        customer_id = await self.view.cog.resolve_customer(info["partner_name"])
        await self._create_worktime(
            interaction, customer_id=customer_id,
            task_id=info["task_id"], project_id=info["project_id"], already_deferred=True,
        )

    # ---- Odoo offline (local customers) -----------------------------------

    async def _search_customers(self, interaction: discord.Interaction, name: str):
        rows = await self.view.db.fetchall(
            "SELECT id, name FROM customer WHERE name LIKE ? "
            "AND (archived IS NULL OR archived = 0) ORDER BY name LIMIT 25",
            (f"%{name}%",),
        )
        if not rows:
            await interaction.response.send_message("No customers found.", ephemeral=True)
            return
        options = [discord.SelectOption(label=r["name"], value=str(r["id"])) for r in rows]
        picker = discord.ui.View()
        picker.add_item(CustomerSelectMenu(options=options, on_select=self._local_selected))
        await interaction.response.send_message("Select a customer:", view=picker, ephemeral=True)

    async def _local_selected(self, interaction: discord.Interaction, customer_id: int):
        await self._create_worktime(
            interaction, customer_id=customer_id, task_id=None, project_id=None, already_deferred=True
        )

    # ---- shared worktime creation -----------------------------------------

    async def _create_worktime(self, interaction: discord.Interaction, customer_id: int,
                               task_id: int | None, project_id: int | None,
                               already_deferred: bool = True):
        view: ClockView = self.view
        state = await load_state(view.db, view.employee_id)

        async def reply(text):
            if already_deferred:
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)

        if not state.clocked_in:
            await reply("You are no longer clocked in.")
            return
        if state.open_worktime:
            await reply("You already have an open work punch; end it first.")
            return

        cust = await view.db.fetchone("SELECT name FROM customer WHERE id = ?", (customer_id,))
        customer_name = cust["name"] if cust else str(customer_id)
        await view.db.execute(
            "INSERT INTO work_time (punchID, customerID, punchType, timeStarted, odooTaskId, odooProjectId) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (state.current_punch, customer_id, self.punch_type, sync.now_local_str(), task_id, project_id),
        )
        # NOTE: the Odoo timesheet is enqueued at work-END (with final hours),
        # not here, so it is created once with the correct duration.
        await render_clock(view.cog, view.message, view.employee_id)
        timecard_log.info(
            f"[Work] {interaction.user} started {self.punch_type} work for '{customer_name}' "
            f"(punch {state.current_punch}, task={task_id}, project={project_id})."
        )
        if self.punch_type == "Office":
            await reply("Office work started.")
        else:
            await reply(f"{self.punch_type} work started for {customer_name}.")


def round_quarter_hours(hours: float) -> float:
    """Round a raw hour span to the nearest quarter-hour, floored at 0.25h.

    Shared by End-Work-Now and the Odoo check-out auto-close so both turn a span
    into billable hours the same way. Uses the canonical quarter-hour grid
    (``sync.quarter_hour_minutes``); for non-negative spans this matches the old
    ``round(h*4)/4 or 0.25`` exactly, and additionally floors clock-skew
    negatives to 0.25h."""
    return max(0.25, sync.quarter_hour_minutes(hours) / 60)


async def finalize_worktime(db: Database, worktime_id: int, hours: float) -> int:
    """Record a worktime's final duration and enqueue its Odoo timesheet line.

    The single place a worktime becomes "done": the End-Work-Now / End-Work-Custom
    buttons and the inbox's Odoo-check-out auto-close both go through here, so any
    future change to what finishing a worktime entails (extra Odoo fields, more
    enqueues, hooks) is made once. Returns the stored minutes; caps at the
    ``timeSpent`` CHECK ceiling (24h) so a runaway span can't wedge the write."""
    minutes = min(int(hours * 60), 1440)
    await db.execute(
        "UPDATE work_time SET timeSpent = ? WHERE id = ?", (minutes, worktime_id)
    )
    # Enqueue the Odoo timesheet now that the final hours are known.
    await sync.enqueue(db, "worktime", worktime_id, "create")
    return minutes


async def close_open_worktimes(db: Database, punch_id: int, end_local_str: str) -> list[tuple]:
    """End every open jobsite worktime on a punch at a given wall-clock time, exactly as
    "End Work Now" would — duration = timeStarted -> end (quarter-hour, floored at 0.25h).
    Used by the Odoo-check-out reconcile and the auto-clock-out so an open worktime is
    never stranded at 0h. Returns [(worktime_id, punchType, minutes), ...] for the caller
    to log; skips rows with an unparseable start time."""
    rows = await db.fetchall(
        "SELECT id, punchType, timeStarted FROM work_time "
        "WHERE punchID = ? AND timeSpent = 0 AND detached = 0",
        (punch_id,),
    )
    if not rows:
        return []
    try:
        end_dt = datetime.strptime(str(end_local_str)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        log.warning(f"[Worktime] Can't parse end time {end_local_str!r}; not closing worktimes on punch {punch_id}.")
        return []
    closed = []
    for wt in rows:
        try:
            started = datetime.strptime(str(wt["timeStarted"])[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            log.warning(f"[Worktime] worktime {wt['id']} has an unparseable start time; skipping auto-close.")
            continue
        span = (end_dt - started).total_seconds() / 3600
        minutes = await finalize_worktime(db, wt["id"], round_quarter_hours(span))
        closed.append((wt["id"], wt["punchType"], minutes))
    return closed


class EndWorkButton(discord.ui.Button):
    def __init__(self, punch_type: str, custom: bool):
        self.punch_type = punch_type
        self.custom = custom
        label = f"End {punch_type} Work {'Custom' if custom else 'Now'}"
        style = discord.ButtonStyle.secondary if custom else discord.ButtonStyle.primary
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction):
        view: ClockView = self.view
        if not await view._guard(interaction):
            return
        if self.custom:
            await interaction.response.send_modal(GetTimeSpent(on_submit=self._finish))
            return
        state = await load_state(view.db, view.employee_id)
        if not state.open_worktime:
            await interaction.response.send_message("No open work punch found.", ephemeral=True)
            return
        row = await view.db.fetchone(
            "SELECT timeStarted FROM work_time WHERE id = ?", (state.open_worktime,)
        )
        started = datetime.strptime(row["timeStarted"][:19], "%Y-%m-%d %H:%M:%S") if row else None
        if started is None:
            await interaction.response.send_message("Start time not found.", ephemeral=True)
            return
        # timeStarted was stored as an ISO string historically; both parse via [:19].
        hours = (datetime.now() - started).total_seconds() / 3600
        nearest_quarter = round_quarter_hours(hours)
        await interaction.response.defer()
        await self._finish(interaction, nearest_quarter, already_deferred=True)

    async def _finish(self, interaction: discord.Interaction, hours: float, already_deferred: bool = False):
        view: ClockView = self.view
        state = await load_state(view.db, view.employee_id)
        if not state.open_worktime:
            msg = "No open work punch found."
            if already_deferred:
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return
        worktime_id = state.open_worktime
        await finalize_worktime(view.db, worktime_id, hours)
        await render_clock(view.cog, view.message, view.employee_id)
        crow = await view.db.fetchone(
            "SELECT c.name FROM work_time wt LEFT JOIN customer c ON wt.customerID = c.id WHERE wt.id = ?",
            (worktime_id,))
        cust = f" for {crow['name']}" if crow and crow["name"] else ""
        timecard_log.info(
            f"[Work] {interaction.user} ended {self.punch_type} worktime {worktime_id} ({hours}h){cust}."
        )
        text = f"You completed your work at the jobsite in {hours} hour(s)."
        if already_deferred:
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)


class IgnoreLunchButton(discord.ui.Button):
    def __init__(self, ignoring: bool):
        label = "Ignoring Lunch Break" if ignoring else "NOT Ignoring Lunch Break"
        style = discord.ButtonStyle.success if ignoring else discord.ButtonStyle.danger
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction):
        view: ClockView = self.view
        if not await view._guard(interaction):
            return
        state = await load_state(view.db, view.employee_id)
        if state.current_punch is None:
            await interaction.response.send_message("You are not clocked in.", ephemeral=True)
            return
        new_value = not state.ignore_lunch
        await view.db.execute(
            "UPDATE punch_clock SET ignoreLunchBreak = ? WHERE id = ?",
            (1 if new_value else 0, state.current_punch),
        )
        await interaction.response.defer()
        await render_clock(view.cog, view.message, view.employee_id)
        timecard_log.info(
            f"[Clock] {interaction.user} set ignore-lunch={new_value} for punch {state.current_punch}."
        )


# ---- approval view ---------------------------------------------------------

class ApprovePunch(discord.ui.View):
    """Admin approval and correction for non-standard punches.

    Each unapproved direction gets an **Approve** button and an **Edit** button.
    Edit opens a modal pre-filled with the current time so an admin can correct
    a clock-in/out time (works with or without Odoo). The old EditPunch button
    referenced attributes that never existed on this view (``view.user``,
    ``view.fetch_message``), so
    it raised on every click and had no working behavior to preserve.
    """

    def __init__(self, cog, punch_id: int, message: discord.Message,
                 in_approval: bool, out_approval: bool):
        super().__init__(timeout=None)
        self.cog = cog
        self.db: Database = cog.db
        self.punch_id = punch_id
        self.message = message
        if not in_approval:
            self.add_item(ApproveButton("clock-in"))
            self.add_item(EditPunchButton("clock-in"))
        if not out_approval:
            self.add_item(ApproveButton("clock-out"))
            self.add_item(EditPunchButton("clock-out"))
        # Removing an accidental shift entirely (admin-only).
        self.add_item(DeleteShiftButton())

    @classmethod
    async def create(cls, cog, punch_id: int, message: discord.Message):
        row = await cog.db.fetchone(
            "SELECT punchInApproval, punchOutApproval FROM punch_clock WHERE id = ?", (punch_id,)
        )
        in_ok = _as_bool(row["punchInApproval"]) if row else True
        out_ok = _as_bool(row["punchOutApproval"]) if row else True
        return cls(cog, punch_id, message, in_ok, out_ok)


class ApproveButton(discord.ui.Button):
    def __init__(self, which: str):
        self.which = which  # "clock-in" or "clock-out"
        style = discord.ButtonStyle.green if which == "clock-in" else discord.ButtonStyle.red
        super().__init__(label=f"Approve {which}", style=style)

    async def callback(self, interaction: discord.Interaction):
        view: ApprovePunch = self.view
        if not has_perms(interaction.user, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return

        field = "punchInApproval" if self.which == "clock-in" else "punchOutApproval"
        await view.db.execute(
            f"UPDATE punch_clock SET {field} = 1 WHERE id = ?", (view.punch_id,)
        )

        row = await view.db.fetchone(
            "SELECT punchInApproval, punchOutApproval FROM punch_clock WHERE id = ?", (view.punch_id,)
        )
        in_ok, out_ok = _as_bool(row["punchInApproval"]), _as_bool(row["punchOutApproval"])
        content = view.message.content + f"\n✅ {interaction.user} approved this {self.which} attempt."

        if in_ok and out_ok:
            await view.db.execute(
                "UPDATE punch_clock SET checkChannelId = NULL, checkMessageId = NULL WHERE id = ?",
                (view.punch_id,),
            )
            await interaction.message.edit(content=content, view=None)
        else:
            await interaction.message.edit(
                content=content, view=await ApprovePunch.create(view.cog, view.punch_id, view.message)
            )
        timecard_log.info(f"[Approval] {interaction.user} approved {self.which} for punch {view.punch_id}.")
        await interaction.response.send_message(
            f"You approved this {self.which} attempt.", ephemeral=True
        )


class EditPunchButton(discord.ui.Button):
    """Admin-only: correct a punch's clock-in/out time via a pre-filled modal."""

    def __init__(self, which: str):
        self.which = which  # "clock-in" or "clock-out"
        super().__init__(label=f"Edit {which}", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: ApprovePunch = self.view
        if not has_perms(interaction.user, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        column = "punchInTime" if self.which == "clock-in" else "punchOutTime"
        row = await view.db.fetchone(
            f"SELECT {column} AS t FROM punch_clock WHERE id = ?", (view.punch_id,)
        )
        current = (row["t"] if row else None) or ""
        await interaction.response.send_modal(
            EditPunchTimeModal(which=self.which, current=current, on_submit=self._apply)
        )

    async def _apply(self, interaction: discord.Interaction, new_value: str):
        view: ApprovePunch = self.view
        column = "punchInTime" if self.which == "clock-in" else "punchOutTime"
        old = await view.db.fetchone(
            f"SELECT {column} AS t FROM punch_clock WHERE id = ?", (view.punch_id,)
        )
        old_value = old["t"] if old else None
        await view.db.execute(
            f"UPDATE punch_clock SET {column} = ? WHERE id = ?", (new_value, view.punch_id),
        )
        # Propagate the correction to Odoo if this punch is linked (best-effort).
        await sync.enqueue(view.db, "punch", view.punch_id, "edit")
        content = (
            view.message.content
            + f"\n✏️ {interaction.user} changed {self.which} time from "
            + f"`{old_value or 'unset'}` to `{new_value}`."
        )
        await interaction.message.edit(
            content=content, view=await ApprovePunch.create(view.cog, view.punch_id, view.message)
        )
        # Refresh the employee's clock message in case state changed.
        emp = await view.db.fetchone(
            "SELECT employeeID FROM punch_clock WHERE id = ?", (view.punch_id,)
        )
        if emp:
            await _refresh_clock_for_employee(view.cog, emp["employeeID"])
        timecard_log.info(
            f"[Punch] {interaction.user} edited {self.which} time of punch {view.punch_id}: "
            f"{old_value or 'unset'} -> {new_value}."
        )
        await interaction.response.send_message(
            f"Updated {self.which} time to `{new_value}`.", ephemeral=True
        )


async def _refresh_clock_for_employee(cog, employee_id: int):
    """Re-render an employee's clock message if they have one."""
    row = await cog.db.fetchone(
        "SELECT clockChannelId, clockMessageId FROM employee WHERE id = ?", (employee_id,)
    )
    if not row or not row["clockChannelId"] or not row["clockMessageId"]:
        return
    try:
        msg = await cog.obtain_message(row["clockChannelId"], row["clockMessageId"])
        await render_clock(cog, msg, employee_id)
    except Exception as e:  # noqa: BLE001
        timecard_log.warning(f"[Punch] Could not refresh clock for employee {employee_id}: {e}")


# ---- deletion (accidental shift + Odoo-side delete approvals) ---------------

async def delete_punch_cascade(cog, punch_id: int, to_odoo: bool = True,
                               annotate: str | None = None) -> int | None:
    """Delete a punch and its worktime, clean up its approval message, and
    (optionally) cascade the deletes to Odoo. Returns the employee id so the
    caller can refresh their clock. If ``annotate`` is given, the punch's approval
    message is kept — its buttons stripped and the text appended — instead of being
    deleted (used so a 'deleted by …' note survives)."""
    db = cog.db
    punch = await db.fetchone(
        "SELECT odooId, employeeID, checkChannelId, checkMessageId FROM punch_clock WHERE id = ?",
        (punch_id,),
    )
    if punch is None:
        return None
    worktimes = await db.fetchall("SELECT id, odooId, detached FROM work_time WHERE punchID = ?", (punch_id,))

    # Cancel any still-pending (non-delete) Odoo pushes for these rows.
    await db.execute(
        "UPDATE odoo_outbox SET status = 'skipped' WHERE status = 'pending' AND op != 'delete' "
        "AND entity_type = 'punch' AND entity_id = ?", (punch_id,))
    for wt in worktimes:
        await db.execute(
            "UPDATE odoo_outbox SET status = 'skipped' WHERE status = 'pending' AND op != 'delete' "
            "AND entity_type = 'worktime' AND entity_id = ?", (wt["id"],))

    # Cascade deletes to Odoo (lines before attendance; FIFO guarantees order).
    # A DETACHED worktime's Odoo line was deliberately unlinked from this shift by an
    # admin (kept, not deleted) -- so delete it locally (its punch is going away) but
    # leave its Odoo line alone; only its local orphan is cleaned up.
    if to_odoo:
        for wt in worktimes:
            if wt["odooId"] and not wt["detached"]:
                await sync.enqueue(db, "worktime", wt["id"], "delete", {"odoo_id": wt["odooId"]})
        if punch["odooId"]:
            await sync.enqueue(db, "punch", punch_id, "delete", {"odoo_id": punch["odooId"]})

    # Delete local rows, children first (FK-safe).
    await db.execute("DELETE FROM work_time WHERE punchID = ?", (punch_id,))
    await db.execute("DELETE FROM punch_clock WHERE id = ?", (punch_id,))

    # The approval message: annotate + strip buttons (keep it) when the caller passed
    # text, otherwise delete it so no orphaned buttons linger.
    if punch["checkChannelId"] and punch["checkMessageId"]:
        try:
            msg = await cog.obtain_message(punch["checkChannelId"], punch["checkMessageId"])
            if annotate:
                await msg.edit(content=msg.content + annotate, view=None)
            else:
                await msg.delete()
        except Exception:  # noqa: BLE001
            pass
    return punch["employeeID"]


async def delete_worktime_local(cog, worktime_id: int, to_odoo: bool = True) -> int | None:
    """Delete a single worktime row (optionally cascading to Odoo). Returns the
    employee id for a clock refresh."""
    db = cog.db
    wt = await db.fetchone("SELECT odooId, punchID FROM work_time WHERE id = ?", (worktime_id,))
    if wt is None:
        return None
    await db.execute(
        "UPDATE odoo_outbox SET status = 'skipped' WHERE status = 'pending' AND op != 'delete' "
        "AND entity_type = 'worktime' AND entity_id = ?", (worktime_id,))
    if to_odoo and wt["odooId"]:
        await sync.enqueue(db, "worktime", worktime_id, "delete", {"odoo_id": wt["odooId"]})
    await db.execute("DELETE FROM work_time WHERE id = ?", (worktime_id,))
    punch = await db.fetchone("SELECT employeeID FROM punch_clock WHERE id = ?", (wt["punchID"],))
    return punch["employeeID"] if punch else None


async def reassign_worktime(cog, worktime_id: int, new_punch_id: int, to_odoo: bool = True):
    """Move a worktime to a different punch/shift, keeping Odoo's shift link
    correct. A synced line has its ``x_studio_shift`` repointed at the new
    attendance; an unsynced one is (re)posted so its create links to the new
    attendance. Returns (old_employee_id, new_employee_id) for clock refreshes."""
    db = cog.db
    wt = await db.fetchone("SELECT punchID, odooId FROM work_time WHERE id = ?", (worktime_id,))
    if wt is None:
        return (None, None)
    old = await db.fetchone("SELECT employeeID FROM punch_clock WHERE id = ?", (wt["punchID"],))
    new = await db.fetchone("SELECT employeeID FROM punch_clock WHERE id = ?", (new_punch_id,))
    old_emp = old["employeeID"] if old else None
    if new is None or new_punch_id == wt["punchID"]:
        return (old_emp, None)  # no-op / bad target
    # Re-attach it (clears any soft-detach) onto the new punch.
    await db.execute(
        "UPDATE work_time SET punchID = ?, detached = 0 WHERE id = ?", (new_punch_id, worktime_id)
    )
    if to_odoo:
        if wt["odooId"]:
            await sync.enqueue(db, "worktime", worktime_id, "reassign")
        else:
            # Never synced: cancel any stale pending push, then let create link
            # it to the new attendance (its punchID now points there).
            await db.execute(
                "UPDATE odoo_outbox SET status = 'skipped' WHERE status = 'pending' "
                "AND op != 'delete' AND entity_type = 'worktime' AND entity_id = ?", (worktime_id,))
            await sync.enqueue(db, "worktime", worktime_id, "create")
    return (old_emp, new["employeeID"])


def _punch_label(row) -> str:
    """'MM-DD HH:MM->out (#id)' for a punch row (id/punchInTime/punchOutTime)."""
    pin = (row["punchInTime"] or "")[5:16] or "?"
    pout = (row["punchOutTime"] or "")[5:16] or "open"
    return f"{pin}→{pout} (#{row['id']})"


async def reassign_candidates(db, punch_id: int):
    """The employee's OTHER shifts, as reassignment targets for a punch being deleted."""
    p = await db.fetchone("SELECT employeeID FROM punch_clock WHERE id = ?", (punch_id,))
    if not p:
        return []
    return await db.fetchall(
        "SELECT id, punchInTime, punchOutTime FROM punch_clock WHERE employeeID = ? AND id != ? "
        "ORDER BY id DESC LIMIT 24", (p["employeeID"], punch_id))


class _WorktimeDecisionSelect(discord.ui.Select):
    """Per-worktime dropdown: delete it with the punch, or reassign to another."""

    def __init__(self, flow, wt, candidates):
        self.flow = flow
        self.wt_id = wt["id"]
        hrs = (wt["timeSpent"] or 0) / 60
        cust = f" {wt['cname']}" if wt["cname"] else ""
        options = [discord.SelectOption(
            label="Delete with punch", value="delete", default=True,
            description=f"{wt['punchType']} {hrs:g}h{cust}"[:100], emoji="\U0001f5d1️")]
        for c in candidates[:24]:
            options.append(discord.SelectOption(
                label=f"Reassign to #{c['id']}"[:100], value=f"punch:{c['id']}",
                description=_punch_label(c)[:100], emoji="➡️"))
        super().__init__(
            placeholder=f"#{wt['id']}: {wt['punchType']} {hrs:g}h{cust} — delete or reassign"[:150],
            min_values=1, max_values=1, options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        self.flow.decisions[self.wt_id] = self.values[0]
        for o in self.options:
            o.default = (o.value == self.values[0])
        await interaction.response.edit_message(view=self.flow)


class _ConfirmDeleteFlowButton(discord.ui.Button):
    def __init__(self, flow):
        super().__init__(label="Confirm", style=discord.ButtonStyle.danger, row=4)
        self.flow = flow

    async def callback(self, interaction: discord.Interaction):
        flow = self.flow
        await interaction.response.defer()
        emps, reassigned = set(), 0
        for wt_id, decision in flow.decisions.items():
            if isinstance(decision, str) and decision.startswith("punch:"):
                old_e, new_e = await reassign_worktime(flow.cog, wt_id, int(decision.split(":")[1]))
                emps.update(e for e in (old_e, new_e) if e)
                reassigned += 1
        # Whatever is still marked "delete" remains on the punch and is removed
        # by the cascade; reassigned rows now hang off their new punch instead.
        annotate = None
        if flow.annotate:
            kept = f" ({reassigned} worktime reassigned)" if reassigned else ""
            annotate = flow.annotate + kept + "."
        emp = await delete_punch_cascade(
            flow.cog, flow.punch_id, to_odoo=flow.cascade_to_odoo, annotate=annotate)
        if emp:
            emps.add(emp)
        for e in emps:
            await _refresh_clock_for_employee(flow.cog, e)
        flow.disable_all_items()
        msg = f"Deleted shift #{flow.punch_id}."
        if reassigned:
            msg += (f" Reassigned {reassigned} worktime "
                    f"entr{'y' if reassigned == 1 else 'ies'} to another shift "
                    f"(Odoo shift links updated).")
        await interaction.edit_original_response(content=msg, view=flow)
        if flow.on_complete:
            try:
                await flow.on_complete(interaction, reassigned)
            except Exception as e:  # noqa: BLE001
                log.warning(f"[Punch] delete-flow on_complete failed for punch {flow.punch_id}: {e}")
        timecard_log.info(f"[Punch] {interaction.user} confirmed delete review for punch {flow.punch_id}: {msg}")
        flow.stop()


class _CancelDeleteFlowButton(discord.ui.Button):
    def __init__(self, flow):
        super().__init__(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
        self.flow = flow

    async def callback(self, interaction: discord.Interaction):
        self.flow.disable_all_items()
        await interaction.response.edit_message(content="Cancelled — nothing was deleted.", view=self.flow)
        self.flow.stop()


class DeletePunchFlow(discord.ui.View):
    """Interactive /deletepunch review: decide each linked worktime's fate
    (delete with the punch or reassign to another shift) before deleting."""

    def __init__(self, cog, punch_id, worktimes, candidates, author_id,
                 *, cascade_to_odoo: bool = True, annotate: str | None = None, on_complete=None):
        super().__init__(timeout=180)
        self.cog = cog
        self.punch_id = punch_id
        self.author_id = author_id
        # cascade_to_odoo: push the deletes to Odoo (False when Odoo already deleted the
        # shift). annotate: keep + annotate the punch's approval message instead of deleting
        # it. on_complete(interaction, reassigned): extra bookkeeping (e.g. the Odoo-delete
        # path clears its pending action + marks its own prompt).
        self.cascade_to_odoo = cascade_to_odoo
        self.annotate = annotate
        self.on_complete = on_complete
        self.decisions = {wt["id"]: "delete" for wt in worktimes}
        for wt in worktimes:  # up to 4 (Discord row limit; the 5th row is the buttons)
            self.add_item(_WorktimeDecisionSelect(self, wt, candidates))
        self.add_item(_ConfirmDeleteFlowButton(self))
        self.add_item(_CancelDeleteFlowButton(self))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your prompt.", ephemeral=True)
            return False
        return True


class DeleteShiftButton(discord.ui.Button):
    """Admin-only: delete an accidental shift — behind a typed-CONFIRM modal that shows
    the shift's details. On confirm, the approval message is kept (buttons stripped) with
    a 'deleted by …' note, rather than vanishing."""

    def __init__(self):
        super().__init__(label="Delete shift", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        view: ApprovePunch = self.view
        if not has_perms(interaction.user, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        details = await self._shift_details(view.db, view.punch_id)
        if details is None:
            await interaction.response.send_message("That shift no longer exists.", ephemeral=True)
            return
        wts = await view.db.fetchall(
            "SELECT wt.id, wt.punchType, wt.timeSpent, c.name AS cname FROM work_time wt "
            "LEFT JOIN customer c ON wt.customerID = c.id WHERE wt.punchID = ? "
            "ORDER BY wt.timeStarted, wt.id", (view.punch_id,))
        # 1–4 worktime entries: open the per-worktime review (delete or reassign each).
        # Discord caps a view at 4 such dropdowns, so for 0 or >4 fall back to the
        # typed-CONFIRM modal (and point >4 at /reassignworktime to keep any).
        if 1 <= len(wts) <= 4:
            candidates = await reassign_candidates(view.db, view.punch_id)
            note = "" if candidates else "\n(No other shift for this employee, so reassignment isn't available.)"
            flow = DeletePunchFlow(
                view.cog, view.punch_id, wts, candidates, interaction.user.id,
                cascade_to_odoo=True, annotate=f"\n🗑️ {interaction.user} deleted this shift")
            await interaction.response.send_message(
                f"Deleting shift #{view.punch_id} — for each worktime entry choose **delete with "
                f"the shift** or **reassign to another shift**, then Confirm.{note}",
                view=flow, ephemeral=True)
            return
        await interaction.response.send_modal(
            DeleteShiftConfirmModal(view.punch_id, details, self._do_delete))

    async def _shift_details(self, db, punch_id) -> str | None:
        """A human summary of the shift for the confirm modal (None if it's gone)."""
        punch = await db.fetchone(
            "SELECT pc.punchInTime, pc.punchOutTime, e.name AS ename FROM punch_clock pc "
            "JOIN employee e ON pc.employeeID = e.id WHERE pc.id = ?", (punch_id,))
        if punch is None:
            return None
        wts = await db.fetchall(
            "SELECT wt.id, wt.punchType, wt.timeSpent, wt.detached, c.name AS cname "
            "FROM work_time wt LEFT JOIN customer c ON wt.customerID = c.id "
            "WHERE wt.punchID = ? ORDER BY wt.timeStarted, wt.id", (punch_id,))
        lines = [
            f"Employee:  {punch['ename']}",
            f"Clock-in:  {punch['punchInTime'] or 'unset'}",
            f"Clock-out: {punch['punchOutTime'] or 'open'}",
            "",
            f"Worktime ({len(wts)}):" if wts else "Worktime: none",
        ]
        for w in wts:
            hrs = (w["timeSpent"] or 0) / 60
            cust = f" · {w['cname']}" if w["cname"] else ""
            tag = "  [detached — Odoo line kept]" if w["detached"] else ""
            lines.append(f" • #{w['id']} {w['punchType']} {hrs:g}h{cust}{tag}")
        lines += ["", "This permanently deletes the shift and its worktime (and the matching "
                  "Odoo records). This cannot be undone."]
        return "\n".join(lines)

    async def _do_delete(self, interaction: discord.Interaction):
        view: ApprovePunch = self.view
        await interaction.response.defer(ephemeral=True)
        wt_count = (await view.db.fetchone(
            "SELECT count(*) AS c FROM work_time WHERE punchID = ?", (view.punch_id,)))["c"]
        wt_note = (f" and its {wt_count} worktime entr{'y' if wt_count == 1 else 'ies'}"
                   if wt_count else "")
        # Keep the approval message (annotate + strip buttons) rather than deleting it.
        emp = await delete_punch_cascade(
            view.cog, view.punch_id, to_odoo=True,
            annotate=f"\n🗑️ {interaction.user} deleted this shift{wt_note}.")
        if emp is None:
            await interaction.followup.send("That shift no longer exists.", ephemeral=True)
            return
        await _refresh_clock_for_employee(view.cog, emp)
        timecard_log.info(
            f"[Punch] {interaction.user} deleted shift (punch {view.punch_id}, {wt_count} worktime).")
        await interaction.followup.send("Shift deleted.", ephemeral=True)


class DeleteApproval(discord.ui.View):
    """Admin approval for an Odoo-originated deletion (Discord is source of truth)."""

    def __init__(self, cog, pending_id: int, model: str, odoo_id: int,
                 local_kind: str, local_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.db: Database = cog.db
        self.pending_id = pending_id
        self.model = model
        self.odoo_id = odoo_id
        self.local_kind = local_kind
        self.local_id = local_id
        self.add_item(DeleteApproveButton())
        self.add_item(DeleteRejectButton())

    @classmethod
    def from_row(cls, cog, row):
        return cls(cog, row["id"], row["model"], row["odoo_id"], row["local_kind"], row["local_id"])


class DeleteApproveButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Approve deletion", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        view: DeleteApproval = self.view
        if not has_perms(interaction.user, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        # Odoo already deleted it -> don't cascade back to Odoo.
        # A single timesheet deletion: nothing to review.
        if view.local_kind != "punch":
            emp = await delete_worktime_local(view.cog, view.local_id, to_odoo=False)
            if emp:
                await _refresh_clock_for_employee(view.cog, emp)
            await view.cog.clear_pending_action(view.pending_id)
            await interaction.message.edit(
                content=interaction.message.content + f"\n✅ {interaction.user} approved the deletion.",
                view=None)
            timecard_log.info(f"[Delete] {interaction.user} approved Odoo deletion of {view.model}:{view.odoo_id}.")
            await interaction.response.send_message("Deletion approved.", ephemeral=True)
            return

        # A shift: if it has worktime, review each entry (delete or reassign to a surviving
        # shift) before removing it — mirrors the Discord "Delete shift" flow. The punch's
        # own approval message (if any) is annotated "deleted in Odoo" instead of removed.
        annotate = f"\n🗑️ this shift was deleted in Odoo — approved by {interaction.user}."
        wts = await view.db.fetchall(
            "SELECT wt.id, wt.punchType, wt.timeSpent, c.name AS cname FROM work_time wt "
            "LEFT JOIN customer c ON wt.customerID = c.id WHERE wt.punchID = ? "
            "ORDER BY wt.timeStarted, wt.id", (view.local_id,))
        prompt_msg, pending_id, cog = interaction.message, view.pending_id, view.cog
        if 1 <= len(wts) <= 4:
            candidates = await reassign_candidates(view.db, view.local_id)
            note = "" if candidates else "\n(No other shift for this employee, so reassignment isn't available.)"

            async def _on_complete(flow_interaction, reassigned):
                await cog.clear_pending_action(pending_id)
                try:
                    await prompt_msg.edit(
                        content=prompt_msg.content + f"\n✅ {flow_interaction.user} approved the deletion.",
                        view=None)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"[Delete] couldn't annotate delete prompt for punch {view.local_id}: {e}")

            flow = DeletePunchFlow(
                cog, view.local_id, wts, candidates, interaction.user.id,
                cascade_to_odoo=False, annotate=annotate, on_complete=_on_complete)
            await interaction.response.send_message(
                f"Odoo deleted this shift — for each worktime entry choose **delete** or "
                f"**reassign to another shift**, then Confirm.{note}", view=flow, ephemeral=True)
            return

        # No worktime (or >4 — can't show per-entry dropdowns): apply the delete directly.
        emp = await delete_punch_cascade(cog, view.local_id, to_odoo=False, annotate=annotate)
        if emp:
            await _refresh_clock_for_employee(cog, emp)
        await cog.clear_pending_action(pending_id)
        extra = f" ({len(wts)} worktime removed — reassign any to keep with /reassignworktime beforehand)" if len(wts) > 4 else ""
        await interaction.message.edit(
            content=interaction.message.content + f"\n✅ {interaction.user} approved the deletion.{extra}",
            view=None)
        timecard_log.info(f"[Delete] {interaction.user} approved Odoo deletion of {view.model}:{view.odoo_id}.")
        await interaction.response.send_message("Deletion approved.", ephemeral=True)


class DeleteRejectButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Reject (restore in Odoo)", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: DeleteApproval = self.view
        if not has_perms(interaction.user, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        kind = "punch" if view.local_kind == "punch" else "worktime"
        await sync.enqueue(view.db, kind, view.local_id, "restore")
        await view.cog.clear_pending_action(view.pending_id)
        await interaction.message.edit(
            content=interaction.message.content
            + f"\n♻️ {interaction.user} rejected — Discord wins; restoring in Odoo.",
            view=None)
        timecard_log.info(
            f"[Delete] {interaction.user} rejected Odoo deletion of {view.model}:{view.odoo_id}; queued restore.")
        await interaction.response.send_message("Kept locally; queued restore to Odoo.", ephemeral=True)


# ---- read-only week viewer (decide what to edit) ----------------------------

def _hm(ts) -> str:
    """'HH:MM' out of a stored 'YYYY-MM-DD HH:MM:SS' string."""
    return (ts or "")[11:16] or "??:??"


# Discord counts RAW characters (markdown + full URLs included), and an embed description
# maxes at 4096. We paginate well under that so a busy week (lots of hyperlinked worktime)
# is fully viewable across pages instead of truncated mid-link.
_PAGE_BUDGET = 3800


def _paginate_blocks(blocks: list[list[str]], header: str) -> list[str]:
    """Pack punch *blocks* (a punch header line followed by its worktime lines) into page
    bodies that, with the repeated `header`, stay under the embed budget.

    A whole block is kept **together** on one page; if it won't fit in the current page's
    remaining room it moves to the next page. Only when a single punch has more worktime
    than fits on an entire page is it split — and then each continuation page re-states the
    punch header tagged ``(con't)`` so the entries are never orphaned. Never splits a line,
    so a hyperlink is never cut. Returns at least one page."""
    cap = _PAGE_BUDGET - (len(header) + 1 if header else 0)   # line room; header repeats
    pages: list[str] = []
    cur: list[str] = []
    cur_len = 0

    def _flush():
        nonlocal cur, cur_len
        if cur:
            pages.append("\n".join(cur))
            cur, cur_len = [], 0

    def _place(line):
        nonlocal cur_len
        cur_len += len(line) + (1 if cur else 0)   # + newline when joining to prior content
        cur.append(line)

    for block in blocks:
        block_len = sum(len(l) for l in block) + max(0, len(block) - 1)
        if cur and cur_len + block_len + 1 <= cap:
            for l in block:               # fits in what's left of the current page
                _place(l)
            continue
        if block_len <= cap:
            _flush()                      # whole block moves to a fresh page
            for l in block:
                _place(l)
            continue
        # Oversized: one punch with more worktime than a page holds -> split with (con't),
        # re-stating the header atop each continuation page.
        _flush()
        head, items, first = block[0], block[1:], True
        i = 0
        while i < len(items):
            seg_head = head if first else head + " _(con't)_"
            cur, cur_len = [seg_head], len(seg_head)
            progressed = False
            while i < len(items) and cur_len + len(items[i]) + 1 <= cap:
                _place(items[i]); i += 1; progressed = True
            if not progressed:            # one worktime line too big to ever fit — force it
                _place(items[i]); i += 1  # (final 4096 guard truncates if truly pathological)
            _flush()
            first = False
    _flush()   # emit the final (partial) page
    return pages or [""]


async def build_timecard_embed(cog, emp_id: int, ename: str, week_end_dt: datetime,
                               *, page: int = 0) -> tuple[discord.Embed, int]:
    """A read-only embed of one employee's week: each punch (with its id, times,
    approval + Odoo status) and its worktime entries nested beneath (id, type,
    hours, customer, sync status). Surfaces the ids the edit commands key off of,
    and unlike the reports it *shows* detached worktime so it can be spotted."""
    db = cog.db
    client = getattr(cog, "client", None)
    linkable = bool(client and client.loaded)
    week_start = week_end_dt - timedelta(days=6)
    lo = week_start.strftime("%Y-%m-%d")
    hi = (week_end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    punches = await db.fetchall(
        "SELECT id, punchInTime, punchOutTime, punchInApproval, punchOutApproval, odooId, legacy, "
        "ignoreLunchBreak "
        "FROM punch_clock WHERE employeeID = ? AND punchInTime >= ? AND punchInTime < ? "
        "ORDER BY punchInTime",
        (emp_id, lo, hi),
    )
    blocks, week_minutes = [], 0   # each block = [punch header, *its worktime lines]
    week_gross = week_net = 0.0    # shift hours on the clock / after the lunch rule
    for p in punches:
        try:
            day = datetime.strptime(p["punchInTime"][:19], "%Y-%m-%d %H:%M:%S").strftime("%a %m-%d")
        except (ValueError, TypeError):
            day = (p["punchInTime"] or "?")[:10]
        # Shift duration + net (lunch-adjusted), using the same costing primitive the reports use.
        try:
            gross, _lunch, net = punch_span(p["punchInTime"], p["punchOutTime"], p["ignoreLunchBreak"])
        except (ValueError, TypeError):
            gross = net = 0.0
        week_gross += gross
        week_net += net
        pout = _hm(p["punchOutTime"]) if p["punchOutTime"] else "open"
        dur = f"  ({gross:g}h)" if p["punchOutTime"] else ""
        marks = []
        if not p["punchOutTime"]:
            marks.append("🟡")
        marks.append("✅" if (p["punchInApproval"] and p["punchOutApproval"]) else "🕓")
        if p["legacy"]:
            marks.append("🗄️")
        elif p["odooId"]:
            marks.append("☁️")
        block = [f"**▸ #{p['id']} · {day}  {_hm(p['punchInTime'])}→{pout}{dur}**  {' '.join(marks)}"]
        wts = await db.fetchall(
            "SELECT wt.id, wt.punchType, wt.timeSpent, wt.odooId, wt.detached, wt.odooProjectId, "
            "wt.odooTaskId, c.name AS cname, c.odooId AS codoo "
            "FROM work_time wt LEFT JOIN customer c ON wt.customerID = c.id "
            "WHERE wt.punchID = ? ORDER BY wt.timeStarted, wt.id",
            (p["id"],),
        )
        if not wts:
            block.append("　• _no worktime_")
        for w in wts:
            hrs = (w["timeSpent"] or 0) / 60
            if w["detached"]:
                tag = "⛔ detached"
            elif w["odooId"]:
                tag = "☁️"
            else:
                tag = "local"
            if not w["detached"]:
                week_minutes += (w["timeSpent"] or 0)
            # Link the "#id type Xh" span to the Odoo work item: a Service worktime's
            # Field Service task, else its project's timesheets. Plain text when offline.
            label = f"#{w['id']} {w['punchType']} {hrs:g}h"
            wt_url = (client.fs_task_url(w["odooTaskId"]) if w["odooTaskId"]
                      else client.project_url(w["odooProjectId"])) if linkable else None
            if wt_url:
                label = f"[{label}]({wt_url})"
            # Customer name -> the Odoo contact (Office has no customer, so no link).
            cust = ""
            if w["cname"]:
                cust_url = client.partner_url(w["codoo"]) if linkable else None
                cname = f"[{w['cname']}]({cust_url})" if cust_url else w["cname"]
                cust = f" · {cname}"
            block.append(f"　• {label}{cust}  ({tag})")
        blocks.append(block)
    if not punches:
        blocks = [["_No punches this week._"]]
    header = f"**Σ {week_gross:g}h clocked · {week_net:g}h after lunch**" if punches else ""
    # Keep each punch + its worktime together on a page (split only an oversized punch).
    pages = _paginate_blocks(blocks, header)
    total = len(pages)
    page = max(0, min(page, total - 1))
    desc = f"{header}\n{pages[page]}" if header else pages[page]
    if len(desc) > 4096:   # last-ditch guard (a single pathologically long line)
        desc = desc[:4080] + "\n… _(truncated)_"
    title = f"{ename} — week ending {week_end_dt.strftime('%Y-%m-%d')}"
    if total > 1:
        title += f"  ·  page {page + 1}/{total}"
    embed = discord.Embed(title=title, description=desc, color=0x5865F2)
    page_note = f" · page {page + 1}/{total}" if total > 1 else ""
    embed.set_footer(
        text=f"{len(punches)} punch(es) · {week_minutes / 60:g}h worktime{page_note}   |   "
             "✅approved 🕓pending 🟡open ☁️synced ⛔detached 🗄️legacy   |   edit by the #ids"
    )
    return embed, total


# ---- /viewtimecard persistent-view store (JSON) ----------------------------
# Non-ephemeral viewtimecard views survive restart: their state is kept in a small
# JSON file (message_id -> {channel_id, emp_id, ename, week_end, author_id, token,
# created_at}) and re-attached on startup, mirroring how the clock messages persist.
# Ephemeral views are session-only and never stored (they can't be re-fetched).

def _timecard_views_path() -> str:
    return os.path.join(db_dir(os.getcwd()), "timecard_views.json")


def load_timecard_views() -> dict:
    try:
        with open(_timecard_views_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_timecard_views(views: dict) -> None:
    path = _timecard_views_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(views, f)
    os.replace(tmp, path)   # atomic


def put_timecard_view(message_id: int, entry: dict) -> None:
    views = load_timecard_views()
    views[str(message_id)] = entry
    save_timecard_views(views)


def remove_timecard_view(message_id) -> None:
    views = load_timecard_views()
    if views.pop(str(message_id), None) is not None:
        save_timecard_views(views)


def update_timecard_view_week(message_id, week_end: str) -> None:
    views = load_timecard_views()
    key = str(message_id)
    if key in views:
        views[key]["week_end"] = week_end
        save_timecard_views(views)


# Ephemeral (session) views listen this long. 15 min is the hard ceiling — Discord's
# ephemeral interaction token expires at 15 min, after which the buttons can't be
# edited/answered anyway. We stop at 14 min so on_timeout still has ~1 min of valid
# token to strip the buttons before it dies.
SESSION_VIEW_TIMEOUT = 840


class TimecardWeekView(discord.ui.View):
    """Prev/Next/Refresh/Close paging around build_timecard_embed for /viewtimecard.

    Two modes: **persistent** (timeout=None, stable custom_ids, JSON-backed,
    re-attached on restart) for non-ephemeral messages; and **session**
    (SESSION_VIEW_TIMEOUT) for ephemeral ones — which strip their own buttons on
    timeout/shutdown."""

    def __init__(self, cog, emp_id: int, ename: str, week_end_dt: datetime, author_id: int,
                 *, persistent: bool = False, token: str | None = None):
        super().__init__(timeout=None if persistent else SESSION_VIEW_TIMEOUT)
        self.cog = cog
        self.emp_id = emp_id
        self.ename = ename
        self.week_end_dt = week_end_dt
        self.author_id = author_id
        self.persistent = persistent
        self.token = token
        self.message: discord.Message | None = None   # set after send / on re-attach
        self.page = 0            # current page within the displayed week
        self.total_pages = 1     # set by render_embed()
        # Row 0: week navigation + refresh/close (◀/▶ = prev/next week). Row 1: in-week
        # page navigation (▲/▼) for a busy week whose lines overflow one embed — up/down
        # so it reads distinctly from the left/right week arrows.
        specs = [("◀ Prev week", "prev", self._prev, 0), ("Next week ▶", "next", self._next, 0),
                 ("🔄 Refresh", "refresh", self._refresh, 0), ("✖ Close", "close", self._close, 0),
                 ("▲ Page", "pageprev", self._page_prev, 1), ("▼ Page", "pagenext", self._page_next, 1)]
        self._page_btns: dict[str, discord.ui.Button] = {}
        for label, key, cb, row in specs:
            style = discord.ButtonStyle.danger if key == "close" else discord.ButtonStyle.secondary
            # Persistent views require a stable custom_id on every item; session views
            # let py-cord auto-generate them.
            btn = discord.ui.Button(label=label, style=style, row=row,
                                    custom_id=(f"tcv:{token}:{key}" if persistent else None))
            btn.callback = cb
            self.add_item(btn)
            if key in ("pageprev", "pagenext"):
                self._page_btns[key] = btn

    def _sync_page_buttons(self):
        """Enable the page buttons only when the week spans more than one page, and grey
        out the end you're already at."""
        multi = self.total_pages > 1
        self._page_btns["pageprev"].disabled = (not multi) or self.page <= 0
        self._page_btns["pagenext"].disabled = (not multi) or self.page >= self.total_pages - 1

    async def render_embed(self) -> discord.Embed:
        """Build the embed for the current week+page, caching the page count and syncing
        the page buttons. The single place the view turns state into an embed."""
        embed, total = await build_timecard_embed(
            self.cog, self.emp_id, self.ename, self.week_end_dt, page=self.page)
        self.total_pages = max(1, total)
        self.page = max(0, min(self.page, self.total_pages - 1))
        self._sync_page_buttons()
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your view.", ephemeral=True)
            return False
        return True

    def _persist_week(self):
        if self.persistent and self.message is not None:
            update_timecard_view_week(self.message.id, self.week_end_dt.strftime("%Y-%m-%d"))

    async def _rerender(self, interaction: discord.Interaction):
        try:
            embed = await self.render_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        except discord.NotFound:
            pass  # message/interaction expired — nothing to update
        except Exception as e:  # noqa: BLE001 - never let a stale press bubble to a crash
            log.warning(f"[Timecard] rerender failed: {e}")

    async def _prev(self, interaction: discord.Interaction):
        self.week_end_dt -= timedelta(days=7)
        self.page = 0   # new week starts at its first page
        self._persist_week()
        await self._rerender(interaction)

    async def _next(self, interaction: discord.Interaction):
        self.week_end_dt += timedelta(days=7)
        self.page = 0
        self._persist_week()
        await self._rerender(interaction)

    async def _refresh(self, interaction: discord.Interaction):
        await self._rerender(interaction)

    async def _page_prev(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
        await self._rerender(interaction)

    async def _page_next(self, interaction: discord.Interaction):
        if self.page < self.total_pages - 1:
            self.page += 1
        await self._rerender(interaction)

    async def _close(self, interaction: discord.Interaction):
        # Close = remove the message. Persistent (real channel) messages are deleted;
        # ephemeral ones can't be hard-deleted by a bot, so they're blanked instead.
        self.stop()
        if self.persistent and self.message is not None:
            remove_timecard_view(self.message.id)
        try:
            if self.persistent:
                await interaction.response.defer()
                await (self.message or interaction.message).delete()
            else:
                await interaction.response.edit_message(content="🗒️ Timecard view closed.", embed=None, view=None)
        except Exception as e:  # noqa: BLE001
            log.warning(f"[Timecard] close failed: {e}")

    async def on_timeout(self):
        # Session (ephemeral) views: strip the buttons once we stop listening.
        if self.message is not None:
            try:
                await self.message.edit(view=None)
            except Exception:  # noqa: BLE001 - token may already be expired
                pass

    async def on_error(self, error, item, interaction):  # noqa: D401
        log.warning(f"[Timecard] view error on {getattr(item, 'custom_id', item)}: {error}")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "This timecard view has expired — run /viewtimecard again.", ephemeral=True)
        except Exception:  # noqa: BLE001
            pass
