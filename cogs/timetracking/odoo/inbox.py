"""Inbound Odoo -> SQLite reconciliation (pull-based).

Odoo's automation POSTs a *pointer* -- ``{_model, _id, _action, write_uid}`` --
to the webhook. We don't trust the payload's contents; instead we enqueue an
inbox row and a background worker **pulls the authoritative record from Odoo**
and reconciles it into SQLite.

Why pull-based (vs. an ``x_studio_recentlyupdated`` flag + time-window ignore
list):

* **Echo suppression is free.** If the pulled record already matches local
  state (because the bot just wrote it), reconciliation is a no-op. No clock
  skew, no timing window, no second Odoo automation to reset a flag.
* **Minimal Odoo config.** One automation per model that sends the id -- no
  Studio field, no reset automation to misconfigure.
* **Self-healing.** A missed webhook just means stale-until-next-touch; a
  duplicate is a harmless no-op.

Optional extra: set ``ODOO_BOT_UID`` to the bot's Odoo user id and echoes are
skipped before the pull even happens (saves an API round-trip).
"""

from __future__ import annotations

import asyncio
import os

from botlog import timecard_log as log  # inbound reconcile activity -> TIMECARD_LOG_ID
from ..db import Database
from . import sync
from .client import OdooClient, shift_field

SUPPORTED_MODELS = ("res.partner", "hr.attendance", "account.analytic.line", "hr.employee")
DRAIN_INTERVAL = 5  # seconds; short so Odoo edits reflect in Discord quickly
MAX_ATTEMPTS = 5


def _project_env(name: str) -> int | None:
    value = os.getenv(name)
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _punch_type_for_project(project_id) -> str:
    """Map an Odoo project id to a local worktime category."""
    if project_id and project_id == _project_env("ODOO_OFFICE_PROJECT_ID"):
        return "Office"
    if project_id and project_id == _project_env("ODOO_FIELD_SERVICE_PROJECT_ID"):
        return "Service"
    return "Construction"


def _quarter_hour_minutes(hours) -> int:
    """Hours -> minutes on the quarter-hour grid, clamped to the schema range."""
    return max(0, min(1440, int(round((hours or 0) * 4) / 4 * 60)))


def _employee_field_map() -> dict:
    """Local employee column -> Odoo hr.employee field name. The address/phone
    fields vary by Odoo version/config, so they're env-overridable; defaults are
    the standard Odoo 19 private-address fields. `addressState` is a Many2one
    (res.country.state) whose display name we store."""
    return {
        "phoneNumber":  os.getenv("ODOO_EMPLOYEE_PHONE_FIELD", "private_phone"),
        "addressLine1": os.getenv("ODOO_EMPLOYEE_STREET_FIELD", "private_street"),
        "addressLine2": os.getenv("ODOO_EMPLOYEE_STREET2_FIELD", "private_street2"),
        "addressCity":  os.getenv("ODOO_EMPLOYEE_CITY_FIELD", "private_city"),
        "addressState": os.getenv("ODOO_EMPLOYEE_STATE_FIELD", "private_state_id"),
        "addressZip":   os.getenv("ODOO_EMPLOYEE_ZIP_FIELD", "private_zip"),
    }


async def enqueue_inbound(db: Database, model: str, odoo_id: int,
                          action: str | None = None, write_uid: int | None = None):
    """Queue an inbound change. De-dupes against any pending row for the same
    (model, id) -- this is the debounce that collapses rapid repeat webhooks."""
    existing = await db.fetchone(
        "SELECT id FROM odoo_inbox WHERE model = ? AND odoo_id = ? AND status = 'pending'",
        (model, odoo_id),
    )
    if existing:
        log.debug(f"[Inbox] Debounced duplicate {model}:{odoo_id}")
        return
    await db.execute(
        "INSERT INTO odoo_inbox (model, odoo_id, action, write_uid, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (model, odoo_id, action, write_uid, sync.now_local_str()),
    )
    log.info(f"[Inbox] Queued {action or 'update'} for {model}:{odoo_id}")


async def enqueue_inbound_delete(db: Database, model: str, odoo_id: int):
    """Queue an Odoo-side deletion. De-dupes against a pending delete for the
    same (model, id). Kept separate from updates so the two don't collide."""
    existing = await db.fetchone(
        "SELECT id FROM odoo_inbox WHERE model = ? AND odoo_id = ? AND action = 'delete' AND status = 'pending'",
        (model, odoo_id),
    )
    if existing:
        return
    await db.execute(
        "INSERT INTO odoo_inbox (model, odoo_id, action, status, created_at) "
        "VALUES (?, ?, 'delete', 'pending', ?)",
        (model, odoo_id, sync.now_local_str()),
    )
    log.info(f"[Inbox] Queued DELETE for {model}:{odoo_id}")


class InboxWorker:
    """Background task that drains the inbound queue and reconciles records."""

    def __init__(self, cog):
        self.cog = cog
        self.db: Database = cog.db
        self.client: OdooClient = cog.client
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        while True:
            try:
                if self.client.loaded:
                    await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.error(f"[Inbox] Drain loop error: {e}")
            await asyncio.sleep(DRAIN_INTERVAL)

    async def drain(self):
        rows = await self.db.fetchall(
            "SELECT * FROM odoo_inbox WHERE status = 'pending' ORDER BY id ASC LIMIT 50"
        )
        for row in rows:
            await self._process(row)

    async def _process(self, row):
        # Cheap echo filter: skip changes the bot itself made in Odoo.
        bot_uid = os.getenv("ODOO_BOT_UID")
        if bot_uid and row["write_uid"] is not None and str(row["write_uid"]) == str(bot_uid):
            log.debug(f"[Inbox] Skipping self-authored change {row['model']}:{row['odoo_id']}")
            await self.db.execute("UPDATE odoo_inbox SET status = 'done' WHERE id = ?", (row["id"],))
            return
        try:
            if row["action"] == "delete":
                result = await self._handle_delete(row["model"], row["odoo_id"])
            else:
                result = await self._reconcile(row["model"], row["odoo_id"])
            if result == "retry":
                return  # dependency not ready yet; leave pending, try next pass
            await self.db.execute("UPDATE odoo_inbox SET status = 'done' WHERE id = ?", (row["id"],))
            if result:
                log.info(f"[Inbox] {row['action'] or 'update'} {row['model']}:{row['odoo_id']} applied.")
            else:
                log.debug(f"[Inbox] {row['model']}:{row['odoo_id']} no-op.")
        except Exception as e:  # noqa: BLE001
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            await self.db.execute(
                "UPDATE odoo_inbox SET attempts = ?, last_error = ?, status = ? WHERE id = ?",
                (attempts, str(e)[:500], status, row["id"]),
            )
            log.warning(f"[Inbox] {row['model']}:{row['odoo_id']} error: {e}")

    async def _reconcile(self, model: str, odoo_id: int) -> bool:
        if model == "res.partner":
            return await self._reconcile_partner(odoo_id)
        if model == "hr.attendance":
            return await self._reconcile_attendance(odoo_id)
        if model == "account.analytic.line":
            return await self._reconcile_analytic_line(odoo_id)
        if model == "hr.employee":
            return await self._reconcile_employee(odoo_id)
        log.warning(f"[Inbox] Unsupported model: {model}")
        return False

    async def _handle_delete(self, model: str, odoo_id: int):
        """An Odoo-side deletion. Customers archive/delete directly; attendance &
        analytic-line deletions require admin approval (Discord is source of truth)."""
        if model == "res.partner":
            row = await self.db.fetchone("SELECT id FROM customer WHERE odooId = ?", (odoo_id,))
            if row is None:
                return False
            referenced = await self.db.fetchone(
                "SELECT 1 FROM work_time WHERE customerID = ? LIMIT 1", (row["id"],)
            )
            if referenced:
                await self.db.execute("UPDATE customer SET archived = 1 WHERE id = ?", (row["id"],))
                log.info(f"[Inbox] Customer {row['id']} archived (referenced locally).")
            else:
                await self.db.execute("DELETE FROM customer WHERE id = ?", (row["id"],))
                log.info(f"[Inbox] Customer {row['id']} deleted (unreferenced).")
            return True
        if model == "hr.attendance":
            punch = await self.db.fetchone("SELECT id FROM punch_clock WHERE odooId = ?", (odoo_id,))
            if punch is None:
                return False  # already gone locally
            await self.cog.post_delete_approval(model, odoo_id, "punch", punch["id"])
            return True
        if model == "account.analytic.line":
            wt = await self.db.fetchone("SELECT id FROM work_time WHERE odooId = ?", (odoo_id,))
            if wt is None:
                return False
            await self.cog.post_delete_approval(model, odoo_id, "worktime", wt["id"])
            return True
        log.warning(f"[Inbox] Unsupported delete model: {model}")
        return False

    # ---- per-model reconcilers (idempotent) --------------------------------

    async def _reconcile_partner(self, odoo_id: int) -> bool:
        rec = await self.client.read_record("res.partner", odoo_id, ["id", "display_name", "active"])
        if rec is None:
            return False  # deleted in Odoo; the /delete webhook handles removal
        name = rec["display_name"]
        archived = 0 if rec.get("active", True) else 1  # Odoo archive -> hide locally
        row = await self.db.fetchone(
            "SELECT id, name, archived FROM customer WHERE odooId = ?", (odoo_id,)
        )
        if row is None:
            await self.db.execute(
                "INSERT INTO customer (name, odooId, archived) VALUES (?, ?, ?)", (name, odoo_id, archived)
            )
            return True
        if row["name"] != name or bool(row["archived"]) != bool(archived):
            await self.db.execute(
                "UPDATE customer SET name = ?, archived = ? WHERE id = ?", (name, archived, row["id"])
            )
            return True
        return False

    async def _local_employee_id(self, emp_odoo_id) -> int | None:
        """Map an Odoo hr.employee id to a local employee row (by odooId)."""
        if not emp_odoo_id:
            return None
        row = await self.db.fetchone("SELECT id FROM employee WHERE odooId = ?", (emp_odoo_id,))
        return row["id"] if row else None

    async def _punch_by_attendance(self, att_odoo_id) -> int | None:
        """Map an Odoo hr.attendance id to the local punch it represents."""
        if not att_odoo_id:
            return None
        row = await self.db.fetchone("SELECT id FROM punch_clock WHERE odooId = ?", (att_odoo_id,))
        return row["id"] if row else None

    async def _refresh_punch_clock(self, punch_id):
        """Re-render the clock of whichever employee owns this punch."""
        if not punch_id:
            return
        row = await self.db.fetchone("SELECT employeeID FROM punch_clock WHERE id = ?", (punch_id,))
        if row:
            await self._refresh_employee_clock(row["employeeID"])

    async def _reconcile_employee(self, odoo_id: int) -> bool:
        """Mirror an Odoo hr.employee onto the local employee (one-way; Odoo is the
        system of record for employee demographics).

        Handles two things: archive/unarchive (a temp worker terminated in Odoo
        loses their Discord clock, history kept), and a **one-way pull of name /
        phone / address** so the weekly report's employee header stays current.
        Only employees linked via ``employee.odooId`` are affected; unknown Odoo
        employees are ignored. Empty Odoo values never blank out a populated local
        value (guards against an incomplete HR record wiping good data).
        """
        fmap = _employee_field_map()
        fields = list(dict.fromkeys(["id", "active", "name", *fmap.values()]))
        rec = await self.client.read_record("hr.employee", odoo_id, fields)
        if rec is None:
            return False  # deleted in Odoo; employees are archived, not deleted
        emp = await self.db.fetchone(
            "SELECT id, name, phoneNumber, addressLine1, addressLine2, addressCity, "
            "addressState, addressZip FROM employee WHERE odooId = ?",
            (odoo_id,),
        )
        if emp is None:
            log.debug(f"[Inbox] hr.employee {odoo_id} not linked locally; skipping.")
            return False

        # 1. archive / reactivate (this removes/keeps the clock).
        changed = await self.cog.set_employee_archived(emp["id"], not rec.get("active", True))

        # 2. one-way demographic pull. A Many2one (state) comes back as [id, name];
        # everything else is scalar. Only overwrite when Odoo has a value.
        def _val(raw):
            if isinstance(raw, (list, tuple)):
                return str(raw[1]).strip() if len(raw) > 1 else ""
            return "" if raw in (False, None) else str(raw).strip()

        incoming = {"name": _val(rec.get("name"))}
        incoming.update({col: _val(rec.get(src)) for col, src in fmap.items()})
        updates = {c: v for c, v in incoming.items() if v and v != (emp[c] or "")}
        if updates:
            sets = ", ".join(f"{c} = ?" for c in updates)  # keys are internal, not user input
            await self.db.execute(
                f"UPDATE employee SET {sets} WHERE id = ?", (*updates.values(), emp["id"])
            )
            log.info(f"[Inbox] Synced employee {emp['id']} from Odoo: {', '.join(updates)}.")
            if "name" in updates:
                await self._refresh_employee_clock(emp["id"])  # name shows on the clock
            changed = True
        return changed

    async def _reconcile_attendance(self, odoo_id: int) -> bool:
        rec = await self.client.read_record(
            "hr.attendance", odoo_id, ["id", "employee_id", "check_in", "check_out"]
        )
        if rec is None:
            return False
        new_in = sync.utc_str_to_local_str(rec["check_in"]) if rec.get("check_in") else None
        new_out = sync.utc_str_to_local_str(rec["check_out"]) if rec.get("check_out") else None
        emp_field = rec.get("employee_id")
        emp_odoo = emp_field[0] if isinstance(emp_field, (list, tuple)) else None
        local_emp = await self._local_employee_id(emp_odoo)

        punch = await self.db.fetchone(
            "SELECT id, employeeID, punchInTime, punchOutTime FROM punch_clock WHERE odooId = ?",
            (odoo_id,),
        )

        if punch is None:
            # Odoo-authored attendance (an admin built the shift in Odoo): mirror it
            # as a local punch so the shift -- and any timesheet lines hanging off
            # it -- become visible in Discord.
            if local_emp is None:
                log.debug(f"[Inbox] hr.attendance {odoo_id} employee not linked locally; skipping.")
                return False
            if not new_in:
                return False
            # Echo guard: if the bot just created this attendance outbound and hasn't
            # written the odooId back yet, an unlinked local punch already matches
            # (same employee + check-in). Adopt it instead of duplicating.
            twin = await self.db.fetchone(
                "SELECT id FROM punch_clock WHERE employeeID = ? AND punchInTime = ? AND odooId IS NULL",
                (local_emp, new_in),
            )
            if twin is not None:
                await self.db.execute(
                    "UPDATE punch_clock SET odooId = ? WHERE id = ?", (odoo_id, twin["id"])
                )
                return True
            await self.db.execute(
                "INSERT INTO punch_clock (employeeID, punchInTime, punchOutTime, odooId) "
                "VALUES (?, ?, ?, ?)",
                (local_emp, new_in, new_out, odoo_id),
            )
            await self._refresh_employee_clock(local_emp)
            log.info(f"[Inbox] Created local punch from Odoo hr.attendance {odoo_id} for employee {local_emp}.")
            return True

        # Existing punch: reconcile times and a possible employee reassignment.
        # (The bot never rewrites employee_id in Odoo, so any change here is a real
        # admin edit; an unlinked target employee is kept as-is rather than lost.)
        new_emp = local_emp if local_emp is not None else punch["employeeID"]
        if emp_odoo and local_emp is None:
            log.warning(f"[Inbox] hr.attendance {odoo_id} reassigned to unlinked Odoo employee {emp_odoo}; keeping current.")
        if (new_in == punch["punchInTime"] and new_out == punch["punchOutTime"]
                and new_emp == punch["employeeID"]):
            return False  # already matches (echo of our own write)

        await self.db.execute(
            "UPDATE punch_clock SET employeeID = ?, punchInTime = ?, punchOutTime = ? WHERE id = ?",
            (new_emp, new_in, new_out, punch["id"]),
        )
        await self._refresh_employee_clock(punch["employeeID"])
        if new_emp != punch["employeeID"]:
            await self._refresh_employee_clock(new_emp)
            log.info(f"[Inbox] hr.attendance {odoo_id} reassigned employee {punch['employeeID']} -> {new_emp}.")
        return True

    async def _reconcile_analytic_line(self, odoo_id: int):
        fields = ["id", "unit_amount", "date", "task_id", "project_id", "partner_id", shift_field()]
        rec = await self.client.read_record("account.analytic.line", odoo_id, fields)
        if rec is None:
            return False
        minutes = _quarter_hour_minutes(rec.get("unit_amount"))
        proj = rec.get("project_id")
        proj_id = proj[0] if isinstance(proj, (list, tuple)) else None
        task = rec.get("task_id")
        task_id = task[0] if isinstance(task, (list, tuple)) else None
        partner = rec.get("partner_id")
        punch_type = _punch_type_for_project(proj_id)
        work_date = str(rec.get("date") or sync.now_local_str())[:10]

        # Resolve which punch this line is attached to via the shift link, if any.
        shift = rec.get(shift_field()) if self.client.shift_field_available else None
        att_odoo = (shift[0] if isinstance(shift, (list, tuple)) else shift) if shift else None
        linked_punch = await self._punch_by_attendance(att_odoo)

        wt = await self.db.fetchone(
            "SELECT id, punchID, customerID, punchType, timeSpent, timeStarted, odooTaskId, "
            "odooProjectId, detached FROM work_time WHERE odooId = ?",
            (odoo_id,),
        )

        if wt is not None:
            # Existing line -> re-sync every field an admin could have changed in
            # Odoo (hours, project/category, task, customer, and the shift it belongs
            # to). Attribution is Discord-first, but admins do correct these directly
            # in Odoo, so we mirror them back rather than let them drift.
            new_customer = wt["customerID"]
            if isinstance(partner, (list, tuple)):
                new_customer = await self.cog.resolve_customer(partner[1])
            # NB: timeStarted is deliberately *not* reconciled -- it's shift-derived.
            #
            # Shift link decides attachment:
            #  * resolves to a known punch  -> (re)attach there and clear 'detached'
            #  * explicitly cleared in Odoo -> SOFT-DETACH: keep the row + its last
            #    punch, but flag detached so it's hidden from reports/clock and not
            #    synced, until a shift is re-assigned (which re-attaches this same row)
            #  * set to an untracked shift  -> leave the current state alone
            # (A real Odoo-side *delete* of the line hard-removes the row elsewhere.)
            if self.client.shift_field_available and linked_punch is not None:
                new_punch, new_detached = linked_punch, 0
            elif self.client.shift_field_available and not shift:
                new_punch, new_detached = wt["punchID"], 1
            else:
                new_punch, new_detached = wt["punchID"], wt["detached"]

            changed = (
                minutes != wt["timeSpent"] or punch_type != wt["punchType"]
                or (proj_id or None) != (wt["odooProjectId"] or None)
                or (task_id or None) != (wt["odooTaskId"] or None)
                or new_customer != wt["customerID"] or new_punch != wt["punchID"]
                or new_detached != wt["detached"]
            )
            if not changed:
                return False
            await self.db.execute(
                "UPDATE work_time SET punchID = ?, customerID = ?, punchType = ?, timeSpent = ?, "
                "odooTaskId = ?, odooProjectId = ?, detached = ? WHERE id = ?",
                (new_punch, new_customer, punch_type, minutes, task_id, proj_id, new_detached, wt["id"]),
            )
            await self._refresh_punch_clock(wt["punchID"])
            if new_punch != wt["punchID"]:
                await self._refresh_punch_clock(new_punch)
            if new_detached != wt["detached"]:
                log.info(f"[Inbox] analytic.line {odoo_id} "
                         f"{'soft-detached (shift cleared)' if new_detached else 're-attached to a shift'}.")
            elif new_punch != wt["punchID"]:
                log.info(f"[Inbox] analytic.line {odoo_id} moved punch {wt['punchID']} -> {new_punch}.")
            log.info(f"[Inbox] Updated local worktime from Odoo analytic.line {odoo_id}.")
            return True

        # No local worktime yet -> create it from the shift link (Odoo-authored line).
        if not self.client.shift_field_available:
            log.debug(f"[Inbox] Shift field unavailable; can't attribute analytic.line {odoo_id}.")
            return False
        if not shift:
            log.debug(f"[Inbox] analytic.line {odoo_id} has no shift link; skipping.")
            return False
        if linked_punch is None:
            return "retry"  # the parent attendance isn't synced locally yet
        customer_id = await self.cog.resolve_customer(partner[1]) if isinstance(partner, (list, tuple)) else 0
        # timeStarted is shift-derived: use the parent punch's start; fall back to
        # the Odoo line's date only if the punch somehow has no start recorded.
        pstart = await self.db.fetchone("SELECT punchInTime FROM punch_clock WHERE id = ?", (linked_punch,))
        started = (pstart["punchInTime"] if pstart and pstart["punchInTime"] else f"{work_date} 00:00:00")
        await self.db.execute(
            "INSERT INTO work_time (punchID, customerID, punchType, timeSpent, timeStarted, "
            "odooId, odooTaskId, odooProjectId) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (linked_punch, customer_id, punch_type, minutes, started, odoo_id, task_id, proj_id),
        )
        await self._refresh_punch_clock(linked_punch)
        log.info(f"[Inbox] Created local worktime from Odoo analytic.line {odoo_id} on punch {linked_punch}.")
        return True

    async def _refresh_employee_clock(self, employee_id: int):
        """Re-render an employee's clock message after an Odoo-driven change."""
        from ..views import render_clock  # local import avoids any import cycle

        row = await self.db.fetchone(
            "SELECT clockChannelId, clockMessageId FROM employee WHERE id = ?", (employee_id,)
        )
        if not row or not row["clockChannelId"] or not row["clockMessageId"]:
            return
        try:
            msg = await self.cog.obtain_message(row["clockChannelId"], row["clockMessageId"])
            await render_clock(self.cog, msg, employee_id)
        except Exception as e:  # noqa: BLE001
            log.warning(f"[Inbox] Could not refresh clock for employee {employee_id}: {e}")
