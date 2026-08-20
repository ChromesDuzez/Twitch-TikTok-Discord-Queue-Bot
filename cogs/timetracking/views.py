"""Clock and approval views.

The central idea: **the database is the single source of truth**. Every button
re-reads :func:`state.load_state` before acting, and every state change ends by
calling :func:`render_clock`, which rebuilds both the embed and the buttons
from the DB. This replaces the old design that reconstructed clock state by
string-matching embed text (the cause of views desyncing on restart).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import discord

from botlog import timecard_log
from .db import Database
from .modals import Confirm, CustomerInputModal, CustomerSelectMenu, EditPunchTimeModal, GetTimeSpent
from .perms import CLOCK_ROLES, has_perms
from .odoo import sync
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
        timecard_log.info(
            f"[Clock] {interaction.user} clocked IN employee {view.employee_id} "
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
        timecard_log.info(
            f"[Clock] {interaction.user} clocked OUT employee {view.employee_id} "
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
                    "The Field Service project id is not configured. Ask an admin to set "
                    "ODOO_FIELD_SERVICE_PROJECT_ID.", ephemeral=True)
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
        nearest_quarter = round(hours * 4) / 4 or 0.25
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
        await view.db.execute(
            "UPDATE work_time SET timeSpent = ? WHERE id = ?",
            (int(hours * 60), worktime_id),
        )
        # Enqueue the Odoo timesheet now that the final hours are known.
        await sync.enqueue(view.db, "worktime", worktime_id, "create")
        await render_clock(view.cog, view.message, view.employee_id)
        timecard_log.info(
            f"[Work] {interaction.user} ended {self.punch_type} worktime {worktime_id} ({hours}h)."
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

async def delete_punch_cascade(cog, punch_id: int, to_odoo: bool = True) -> int | None:
    """Delete a punch and its worktime, clean up its approval message, and
    (optionally) cascade the deletes to Odoo. Returns the employee id so the
    caller can refresh their clock."""
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

    # Remove the approval message so no orphaned buttons linger.
    if punch["checkChannelId"] and punch["checkMessageId"]:
        try:
            msg = await cog.obtain_message(punch["checkChannelId"], punch["checkMessageId"])
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
        emp = await delete_punch_cascade(flow.cog, flow.punch_id, to_odoo=True)
        if emp:
            emps.add(emp)
        for e in emps:
            await _refresh_clock_for_employee(flow.cog, e)
        flow.disable_all_items()
        msg = f"Deleted punch #{flow.punch_id}."
        if reassigned:
            msg += (f" Reassigned {reassigned} worktime "
                    f"entr{'y' if reassigned == 1 else 'ies'} to another shift "
                    f"(Odoo shift links updated).")
        await interaction.edit_original_response(content=msg, view=flow)
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

    def __init__(self, cog, punch_id, worktimes, candidates, author_id):
        super().__init__(timeout=180)
        self.cog = cog
        self.punch_id = punch_id
        self.author_id = author_id
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
    """Admin-only: delete an accidental shift from the approval message."""

    def __init__(self):
        super().__init__(label="Delete shift", style=discord.ButtonStyle.danger)

    async def callback(self, interaction: discord.Interaction):
        view: ApprovePunch = self.view
        if not has_perms(interaction.user, accepted_roles=("TIMECARD_ADMIN_ROLE",)):
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        row = await view.db.fetchone(
            "SELECT count(*) AS c FROM work_time WHERE punchID = ?", (view.punch_id,)
        )
        wt_count = row["c"]
        if wt_count:
            confirm = Confirm(user=interaction.user, timeout=60)
            await interaction.response.send_message(
                f"This shift has {wt_count} worktime entr{'y' if wt_count == 1 else 'ies'}. "
                "Delete them too?", view=confirm, ephemeral=True)
            await confirm.wait()
            if not confirm.value:
                await interaction.followup.send("Deletion cancelled.", ephemeral=True)
                return
        else:
            await interaction.response.defer(ephemeral=True)
        emp = await delete_punch_cascade(view.cog, view.punch_id, to_odoo=True)
        if emp:
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
        if view.local_kind == "punch":
            emp = await delete_punch_cascade(view.cog, view.local_id, to_odoo=False)
        else:
            emp = await delete_worktime_local(view.cog, view.local_id, to_odoo=False)
        if emp:
            await _refresh_clock_for_employee(view.cog, emp)
        await view.cog.clear_pending_action(view.pending_id)
        await interaction.message.edit(
            content=interaction.message.content + f"\n✅ {interaction.user} approved the deletion.",
            view=None)
        timecard_log.info(
            f"[Delete] {interaction.user} approved Odoo deletion of {view.model}:{view.odoo_id}.")
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


async def build_timecard_embed(cog, emp_id: int, ename: str, week_end_dt: datetime) -> discord.Embed:
    """A read-only embed of one employee's week: each punch (with its id, times,
    approval + Odoo status) and its worktime entries nested beneath (id, type,
    hours, customer, sync status). Surfaces the ids the edit commands key off of,
    and unlike the reports it *shows* detached worktime so it can be spotted."""
    db = cog.db
    week_start = week_end_dt - timedelta(days=6)
    lo = week_start.strftime("%Y-%m-%d")
    hi = (week_end_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    punches = await db.fetchall(
        "SELECT id, punchInTime, punchOutTime, punchInApproval, punchOutApproval, odooId, legacy "
        "FROM punch_clock WHERE employeeID = ? AND punchInTime >= ? AND punchInTime < ? "
        "ORDER BY punchInTime",
        (emp_id, lo, hi),
    )
    lines, week_minutes = [], 0
    for p in punches:
        try:
            day = datetime.strptime(p["punchInTime"][:19], "%Y-%m-%d %H:%M:%S").strftime("%a %m-%d")
        except (ValueError, TypeError):
            day = (p["punchInTime"] or "?")[:10]
        pout = _hm(p["punchOutTime"]) if p["punchOutTime"] else "open"
        marks = []
        if not p["punchOutTime"]:
            marks.append("🟡")
        marks.append("✅" if (p["punchInApproval"] and p["punchOutApproval"]) else "🕓")
        if p["legacy"]:
            marks.append("🗄️")
        elif p["odooId"]:
            marks.append("☁️")
        lines.append(f"**▸ #{p['id']} · {day}  {_hm(p['punchInTime'])}→{pout}**  {' '.join(marks)}")
        wts = await db.fetchall(
            "SELECT wt.id, wt.punchType, wt.timeSpent, wt.odooId, wt.detached, c.name AS cname "
            "FROM work_time wt LEFT JOIN customer c ON wt.customerID = c.id "
            "WHERE wt.punchID = ? ORDER BY wt.timeStarted, wt.id",
            (p["id"],),
        )
        if not wts:
            lines.append("　• _no worktime_")
        for w in wts:
            hrs = (w["timeSpent"] or 0) / 60
            cust = f" · {w['cname']}" if w["cname"] else ""
            if w["detached"]:
                tag = "⛔ detached"
            elif w["odooId"]:
                tag = "☁️"
            else:
                tag = "local"
            if not w["detached"]:
                week_minutes += (w["timeSpent"] or 0)
            lines.append(f"　• #{w['id']} {w['punchType']} {hrs:g}h{cust}  ({tag})")
    if not punches:
        lines.append("_No punches this week._")
    desc = "\n".join(lines)
    if len(desc) > 4000:
        desc = desc[:3980] + "\n… _(truncated — narrow the week)_"
    embed = discord.Embed(
        title=f"{ename} — week ending {week_end_dt.strftime('%Y-%m-%d')}",
        description=desc,
        color=0x5865F2,
    )
    embed.set_footer(
        text=f"{len(punches)} punch(es) · {week_minutes / 60:g}h worktime   |   "
             "✅approved 🕓pending 🟡open ☁️synced ⛔detached 🗄️legacy   |   edit by the #ids"
    )
    return embed


class TimecardWeekView(discord.ui.View):
    """Prev/Next/Refresh paging around build_timecard_embed for /viewtimecard."""

    def __init__(self, cog, emp_id: int, ename: str, week_end_dt: datetime, author_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.emp_id = emp_id
        self.ename = ename
        self.week_end_dt = week_end_dt
        self.author_id = author_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your view.", ephemeral=True)
            return False
        return True

    async def _rerender(self, interaction: discord.Interaction):
        embed = await build_timecard_embed(self.cog, self.emp_id, self.ename, self.week_end_dt)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Prev week", style=discord.ButtonStyle.secondary)
    async def prev_week(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.week_end_dt -= timedelta(days=7)
        await self._rerender(interaction)

    @discord.ui.button(label="Next week ▶", style=discord.ButtonStyle.secondary)
    async def next_week(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.week_end_dt += timedelta(days=7)
        await self._rerender(interaction)

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._rerender(interaction)
