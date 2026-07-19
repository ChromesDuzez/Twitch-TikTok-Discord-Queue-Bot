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

SUPPORTED_MODELS = ("res.partner", "hr.attendance", "account.analytic.line")
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

    async def _reconcile_attendance(self, odoo_id: int) -> bool:
        rec = await self.client.read_record(
            "hr.attendance", odoo_id, ["id", "employee_id", "check_in", "check_out"]
        )
        if rec is None:
            return False
        punch = await self.db.fetchone(
            "SELECT id, employeeID, punchInTime, punchOutTime FROM punch_clock WHERE odooId = ?",
            (odoo_id,),
        )
        if punch is None:
            # We only reconcile attendances the bot already tracks locally.
            log.debug(f"[Inbox] No local punch for hr.attendance {odoo_id}; skipping.")
            return False

        new_in = sync.utc_str_to_local_str(rec["check_in"]) if rec.get("check_in") else None
        new_out = sync.utc_str_to_local_str(rec["check_out"]) if rec.get("check_out") else None
        if new_in == punch["punchInTime"] and new_out == punch["punchOutTime"]:
            return False  # already matches (echo of our own write)

        await self.db.execute(
            "UPDATE punch_clock SET punchInTime = ?, punchOutTime = ? WHERE id = ?",
            (new_in, new_out, punch["id"]),
        )
        await self._refresh_employee_clock(punch["employeeID"])
        return True

    async def _reconcile_analytic_line(self, odoo_id: int):
        fields = ["id", "unit_amount", "date", "task_id", "project_id", "partner_id", shift_field()]
        rec = await self.client.read_record("account.analytic.line", odoo_id, fields)
        if rec is None:
            return False
        minutes = _quarter_hour_minutes(rec.get("unit_amount"))

        wt = await self.db.fetchone(
            "SELECT id, timeSpent FROM work_time WHERE odooId = ?", (odoo_id,)
        )
        if wt is not None:  # existing -> update hours (idempotent)
            if minutes == wt["timeSpent"]:
                return False
            await self.db.execute("UPDATE work_time SET timeSpent = ? WHERE id = ?", (minutes, wt["id"]))
            return True

        # No local worktime yet -> create it from the shift link (Odoo-authored line).
        if not self.client.shift_field_available:
            log.debug(f"[Inbox] Shift field unavailable; can't attribute analytic.line {odoo_id}.")
            return False
        shift = rec.get(shift_field())
        if not shift:
            log.debug(f"[Inbox] analytic.line {odoo_id} has no shift link; skipping.")
            return False
        att_odoo = shift[0] if isinstance(shift, (list, tuple)) else shift
        punch = await self.db.fetchone("SELECT id FROM punch_clock WHERE odooId = ?", (att_odoo,))
        if punch is None:
            return "retry"  # the parent attendance isn't synced locally yet

        proj = rec.get("project_id")
        proj_id = proj[0] if isinstance(proj, (list, tuple)) else None
        task = rec.get("task_id")
        task_id = task[0] if isinstance(task, (list, tuple)) else None
        partner = rec.get("partner_id")
        customer_id = await self.cog.resolve_customer(partner[1]) if isinstance(partner, (list, tuple)) else 0
        work_date = str(rec.get("date") or sync.now_local_str())[:10]

        await self.db.execute(
            "INSERT INTO work_time (punchID, customerID, punchType, timeSpent, timeStarted, "
            "odooId, odooTaskId, odooProjectId) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (punch["id"], customer_id, _punch_type_for_project(proj_id), minutes,
             f"{work_date} 00:00:00", odoo_id, task_id, proj_id),
        )
        log.info(f"[Inbox] Created local worktime from Odoo analytic.line {odoo_id} on punch {punch['id']}.")
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
