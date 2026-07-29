"""Odoo sync orchestration (SQLite-authoritative, best-effort).

Design: every state change is committed to SQLite first, then an *outbox* row
is enqueued. A background worker drains the outbox and pushes to Odoo, storing
returned Odoo ids back on the local rows. If Odoo is offline or unconfigured,
rows simply stay ``pending`` and are retried later -- nothing is ever lost and
the bot keeps working normally.

Inbound updates from Odoo (via the secured webhook) are applied here too, so
Odoo-originated changes flow through the same local data layer.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime

import pytz

from botlog import timecard_log as log  # sync activity -> TIMECARD_LOG_ID
from ..db import Database
from .client import OdooClient

MAX_ATTEMPTS = 5
DRAIN_INTERVAL = 30  # seconds between outbox drains


def _timezone():
    return pytz.timezone(os.getenv("TIMEZONE", "America/Chicago"))


def now_local_str() -> str:
    """Current wall-clock time in the configured timezone (naive string)."""
    return datetime.now(_timezone()).strftime("%Y-%m-%d %H:%M:%S")


def local_str_to_utc_str(local_str: str) -> str:
    """Convert a stored naive local timestamp to a UTC string for Odoo."""
    naive = datetime.strptime(local_str, "%Y-%m-%d %H:%M:%S")
    localized = _timezone().localize(naive)
    return localized.astimezone(pytz.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_str_to_local_str(utc_str: str) -> str:
    """Convert an Odoo UTC datetime string to our naive local string format."""
    naive = datetime.strptime(str(utc_str)[:19], "%Y-%m-%d %H:%M:%S")
    utc = pytz.utc.localize(naive)
    return utc.astimezone(_timezone()).strftime("%Y-%m-%d %H:%M:%S")


async def enqueue(db: Database, entity_type: str, entity_id: int, op: str, payload: dict | None = None):
    """Add a change to the Odoo outbox (called right after a local commit)."""
    await db.execute(
        "INSERT INTO odoo_outbox (entity_type, entity_id, op, payload, status, created_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?)",
        (entity_type, entity_id, op, json.dumps(payload or {}), now_local_str()),
    )


class SyncWorker:
    """Background task that drains the Odoo outbox."""

    def __init__(self, db: Database, client: OdooClient):
        self.db = db
        self.client = client
        self._task: asyncio.Task | None = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self):
        probe = 0
        while True:
            try:
                if self.client.loaded:
                    # Re-probe the Studio shift field periodically (self-heals if
                    # it's added later); gates deletion support. ~every 10th pass.
                    if probe % 10 == 0:
                        await self.client.check_shift_field()
                    probe += 1
                    await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log.error(f"[Sync] Drain loop error: {e}")
            await asyncio.sleep(DRAIN_INTERVAL)

    async def drain(self):
        rows = await self.db.fetchall(
            "SELECT * FROM odoo_outbox WHERE status = 'pending' ORDER BY id ASC LIMIT 50"
        )
        for row in rows:
            await self._process(row)

    async def _process(self, row):
        try:
            payload = json.loads(row["payload"] or "{}")
            handled = await self._dispatch(row["entity_type"], row["entity_id"], row["op"], payload)
            if handled == "retry":
                return  # dependency not ready yet; leave pending, try next pass
            status = "done" if handled else "skipped"
            await self.db.execute(
                "UPDATE odoo_outbox SET status = ? WHERE id = ?", (status, row["id"])
            )
        except Exception as e:  # noqa: BLE001
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
            await self.db.execute(
                "UPDATE odoo_outbox SET attempts = ?, last_error = ?, status = ? WHERE id = ?",
                (attempts, str(e)[:500], status, row["id"]),
            )
            log.warning(f"[Sync] Outbox #{row['id']} ({row['entity_type']}/{row['op']}) error: {e}")

    async def _dispatch(self, entity_type: str, entity_id: int, op: str, payload: dict):
        """Return True (done), False (skip), or 'retry' (dependency pending)."""
        if entity_type == "customer" and op == "create":
            return await self._sync_customer(entity_id)
        if entity_type == "punch" and op == "in":
            return await self._sync_punch_in(entity_id)
        if entity_type == "punch" and op == "out":
            return await self._sync_punch_out(entity_id)
        if entity_type == "punch" and op == "edit":
            return await self._sync_punch_edit(entity_id)
        if entity_type == "worktime" and op == "create":
            return await self._sync_worktime(entity_id)
        # deletions (Discord -> Odoo): odoo id is carried in the payload because
        # the local row is already gone by the time this drains.
        if entity_type in ("punch", "worktime") and op == "delete":
            return await self._delete_in_odoo(entity_type, payload.get("odoo_id"))
        # restores (reject an Odoo-side delete; Discord wins): re-create in Odoo.
        if entity_type == "punch" and op == "restore":
            return await self._restore_punch(entity_id)
        if entity_type == "worktime" and op == "restore":
            return await self._restore_worktime(entity_id)
        log.warning(f"[Sync] Unknown outbox job: {entity_type}/{op}")
        return False

    # ---- deletions (Discord -> Odoo) --------------------------------------

    async def _delete_in_odoo(self, entity_type: str, odoo_id):
        """Unlink an hr.attendance (punch) or account.analytic.line (worktime)."""
        if not odoo_id:
            return True  # nothing was synced to Odoo; nothing to delete
        model = "hr.attendance" if entity_type == "punch" else "account.analytic.line"
        await self.client.unlink(model, int(odoo_id))
        log.info(f"[Sync] Deleted {model} {odoo_id} in Odoo.")
        return True

    # ---- restores (Odoo-side delete rejected; Discord is truth) -----------

    async def _restore_punch(self, punch_id: int):
        """Re-create a locally-still-present punch's hr.attendance in Odoo."""
        punch = await self.db.fetchone(
            "SELECT employeeID, punchInTime, punchOutTime FROM punch_clock WHERE id = ?", (punch_id,)
        )
        if punch is None or not punch["punchInTime"]:
            return True
        emp_odoo = await self._employee_odoo_id(punch["employeeID"])
        if not emp_odoo:
            return "retry"
        att_id = await self.client.attendance_create(emp_odoo, local_str_to_utc_str(punch["punchInTime"]))
        if not att_id:
            return "retry"
        await self.db.execute("UPDATE punch_clock SET odooId = ? WHERE id = ?", (att_id, punch_id))
        if punch["punchOutTime"]:
            await self.client.attendance_write(att_id, check_out_utc=local_str_to_utc_str(punch["punchOutTime"]))
        # Re-create the worktime lines under the restored attendance.
        for wt in await self.db.fetchall(
            "SELECT id FROM work_time WHERE punchID = ? AND timeSpent > 0", (punch_id,)
        ):
            await self.db.execute("UPDATE work_time SET odooId = NULL WHERE id = ?", (wt["id"],))
            await enqueue(self.db, "worktime", wt["id"], "restore")
        log.info(f"[Sync] Restored punch {punch_id} as hr.attendance {att_id} in Odoo.")
        return True

    async def _restore_worktime(self, worktime_id: int):
        """Re-create a locally-still-present worktime's analytic line in Odoo."""
        # Clearing the stale odooId lets the normal create path re-post it.
        await self.db.execute("UPDATE work_time SET odooId = NULL WHERE id = ?", (worktime_id,))
        return await self._sync_worktime(worktime_id)

    # ---- handlers ----------------------------------------------------------

    async def _sync_customer(self, customer_id: int):
        row = await self.db.fetchone("SELECT name, odooId FROM customer WHERE id = ?", (customer_id,))
        if row is None or row["odooId"] is not None:
            return True
        partner = await self.client.create_partner(row["name"])
        odoo_id = partner[0] if isinstance(partner, (list, tuple)) else partner.get("id") if isinstance(partner, dict) else partner
        if odoo_id:
            await self.db.execute("UPDATE customer SET odooId = ? WHERE id = ?", (odoo_id, customer_id))
        return True

    async def _employee_odoo_id(self, employee_id: int):
        row = await self.db.fetchone("SELECT odooId FROM employee WHERE id = ?", (employee_id,))
        return row["odooId"] if row else None

    async def _sync_punch_in(self, punch_id: int):
        punch = await self.db.fetchone(
            "SELECT employeeID, punchInTime, odooId, legacy FROM punch_clock WHERE id = ?", (punch_id,)
        )
        if punch is None or punch["legacy"] or punch["odooId"] is not None:
            return True  # legacy (never sync) or already synced
        emp_odoo = await self._employee_odoo_id(punch["employeeID"])
        if not emp_odoo or not punch["punchInTime"]:
            return "retry"  # employee not linked to Odoo yet
        att_id = await self.client.attendance_create(
            emp_odoo, local_str_to_utc_str(punch["punchInTime"])
        )
        if att_id:
            await self.db.execute("UPDATE punch_clock SET odooId = ? WHERE id = ?", (att_id, punch_id))
        return True

    async def _sync_punch_out(self, punch_id: int):
        punch = await self.db.fetchone(
            "SELECT punchOutTime, odooId, legacy FROM punch_clock WHERE id = ?", (punch_id,)
        )
        if punch is None or punch["legacy"]:
            return True
        if punch["odooId"] is None:
            return "retry"  # wait for the check-in to sync first
        if not punch["punchOutTime"]:
            return True
        await self.client.attendance_write(
            punch["odooId"], local_str_to_utc_str(punch["punchOutTime"])
        )
        return True

    async def _sync_punch_edit(self, punch_id: int):
        """Push an admin time correction to Odoo. Creates the attendance if it
        wasn't synced yet, otherwise rewrites check-in/check-out."""
        punch = await self.db.fetchone(
            "SELECT employeeID, punchInTime, punchOutTime, odooId, legacy FROM punch_clock WHERE id = ?",
            (punch_id,),
        )
        if punch is None or punch["legacy"] or not punch["punchInTime"]:
            return True
        emp_odoo = await self._employee_odoo_id(punch["employeeID"])
        if not emp_odoo:
            return "retry"
        check_in = local_str_to_utc_str(punch["punchInTime"])
        check_out = local_str_to_utc_str(punch["punchOutTime"]) if punch["punchOutTime"] else None
        if punch["odooId"] is None:
            att_id = await self.client.attendance_create(emp_odoo, check_in)
            if not att_id:
                return "retry"
            await self.db.execute("UPDATE punch_clock SET odooId = ? WHERE id = ?", (att_id, punch_id))
            if check_out:
                await self.client.attendance_write(att_id, check_out_utc=check_out)
        else:
            await self.client.attendance_write(
                punch["odooId"], check_out_utc=check_out, check_in_utc=check_in
            )
        return True

    async def _sync_worktime(self, worktime_id: int):
        wt = await self.db.fetchone(
            "SELECT punchID, punchType, timeSpent, timeStarted, odooId, odooTaskId, odooProjectId "
            "FROM work_time WHERE id = ?",
            (worktime_id,),
        )
        if wt is None or wt["odooId"] is not None:
            return True  # gone or already timesheeted
        punch = await self.db.fetchone(
            "SELECT employeeID, odooId, legacy FROM punch_clock WHERE id = ?", (wt["punchID"],)
        )
        if punch is None or punch["legacy"]:
            return True  # legacy worktime (historical): never sync
        if not wt["odooProjectId"]:
            # No Odoo work item linked (e.g. created while Odoo was offline).
            # Local record stays authoritative; nothing to post.
            return False
        if not wt["timeSpent"]:
            return True  # still open / zero hours -- nothing to post yet

        emp_odoo = await self._employee_odoo_id(punch["employeeID"])
        if not emp_odoo:
            return "retry"  # employee not linked to Odoo yet
        # Wait for the parent attendance to sync so we can set the shift link.
        if punch["odooId"] is None:
            return "retry"

        hours = wt["timeSpent"] / 60
        work_date = str(wt["timeStarted"])[:10]
        description = f"{wt['punchType']} work (Discord timecard)"
        line_id = await self.client.add_timesheet(
            project_id=wt["odooProjectId"],
            date=work_date,
            employee_odoo_id=emp_odoo,
            description=description,
            hours=hours,
            task_id=wt["odooTaskId"],
            shift_attendance_id=punch["odooId"],
        )
        if line_id:
            await self.db.execute("UPDATE work_time SET odooId = ? WHERE id = ?", (line_id, worktime_id))
        return True
