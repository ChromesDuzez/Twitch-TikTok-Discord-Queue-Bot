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


def quarter_hour_minutes(hours) -> int:
    """Round an hour amount to the quarter-hour grid and return whole minutes.

    The single source of truth for the 15-minute grid the schema enforces
    (``work_time.timeSpent % 15 == 0``). Callers layer their own floor/range
    policy on top -- a running worktime floors to 15 min, an inbound timesheet
    allows 0, a manual entry rejects out-of-range -- but the rounding lives here."""
    return int(round((hours or 0) * 4) / 4 * 60)


def worktime_description(punch_type, minutes) -> str:
    """The timesheet-line description pushed to Odoo. A zero-duration line (an
    'abandoned' worktime that was never ended before clock-out) keeps the normal
    label but is flagged so it's visibly a placeholder in Odoo, not a real 0h log."""
    base = f"{punch_type} work (Discord timecard)"
    if not minutes:
        return base + " — abandoned worktime (never ended before clock-out; 0h placeholder)"
    return base


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
                    # Detect the Odoo major version once (self-heals until it sticks).
                    if self.client.odoo_version is None:
                        await self.client.detect_version()
                    probe += 1
                    await self.drain()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep the loop alive
                log.error(f"[Outbox] Drain loop error: {e}")
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
            log.warning(f"[Outbox] Outbox #{row['id']} ({row['entity_type']}/{row['op']}) error: {e}")

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
            return await self._sync_worktime(entity_id, allow_zero=bool(payload.get("allow_zero")))
        if entity_type == "worktime" and op == "edit":
            return await self._sync_worktime_edit(entity_id)
        if entity_type == "worktime" and op == "reassign":
            return await self._reassign_worktime(entity_id)
        # deletions (Discord -> Odoo): odoo id is carried in the payload because
        # the local row is already gone by the time this drains.
        if entity_type in ("punch", "worktime") and op == "delete":
            return await self._delete_in_odoo(entity_type, payload.get("odoo_id"))
        # restores (reject an Odoo-side delete; Discord wins): re-create in Odoo.
        if entity_type == "punch" and op == "restore":
            return await self._restore_punch(entity_id)
        if entity_type == "worktime" and op == "restore":
            return await self._restore_worktime(entity_id)
        log.warning(f"[Outbox] Unknown outbox job: {entity_type}/{op}")
        return False

    # ---- deletions (Discord -> Odoo) --------------------------------------

    async def _delete_in_odoo(self, entity_type: str, odoo_id):
        """Unlink an hr.attendance (punch) or account.analytic.line (worktime)."""
        if not odoo_id:
            return True  # nothing was synced to Odoo; nothing to delete
        model = "hr.attendance" if entity_type == "punch" else "account.analytic.line"
        await self.client.unlink(model, int(odoo_id))
        log.info(f"[Outbox] Deleted {model} {odoo_id} in Odoo.")
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
        att_id = await self._ensure_attendance(emp_odoo, local_str_to_utc_str(punch["punchInTime"]))
        if not att_id:
            return "retry"
        await self.db.execute("UPDATE punch_clock SET odooId = ? WHERE id = ?", (att_id, punch_id))
        if punch["punchOutTime"]:
            await self.client.attendance_write(att_id, check_out_utc=local_str_to_utc_str(punch["punchOutTime"]))
        # Re-link the worktime lines to the restored attendance. A line that still
        # exists in Odoo (orphaned when its attendance was deleted) is re-pointed at
        # the new attendance's shift rather than duplicated; a never-synced line is
        # created. _reassign_worktime self-heals if the line was truly deleted.
        for wt in await self.db.fetchall(
            "SELECT id, odooId FROM work_time WHERE punchID = ? AND timeSpent > 0", (punch_id,)
        ):
            await enqueue(self.db, "worktime", wt["id"], "reassign" if wt["odooId"] else "create")
        log.info(f"[Outbox] Restored punch {punch_id} as hr.attendance {att_id} in Odoo.")
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
            log.info(f"[Outbox] Created res.partner {odoo_id} for customer {customer_id} ('{row['name']}').")
        return True

    async def _employee_odoo_id(self, employee_id: int):
        row = await self.db.fetchone("SELECT odooId FROM employee WHERE id = ?", (employee_id,))
        return row["odooId"] if row else None

    async def _ensure_attendance(self, emp_odoo: int, check_in_utc: str):
        """Idempotent attendance create: adopt an existing attendance for this
        (employee, check-in) if one exists, else create a new one. Prevents the
        duplicate-overlap 422 that occurs when a create is retried after a prior
        attempt already made the attendance (e.g. a crash before odooId was saved)."""
        existing = await self.client.find_attendance(emp_odoo, check_in_utc)
        if existing:
            return existing
        return await self.client.attendance_create(emp_odoo, check_in_utc)

    async def _sync_punch_in(self, punch_id: int):
        punch = await self.db.fetchone(
            "SELECT employeeID, punchInTime, odooId, legacy FROM punch_clock WHERE id = ?", (punch_id,)
        )
        if punch is None or punch["legacy"] or punch["odooId"] is not None:
            return True  # legacy (never sync) or already synced
        emp_odoo = await self._employee_odoo_id(punch["employeeID"])
        if not emp_odoo or not punch["punchInTime"]:
            return "retry"  # employee not linked to Odoo yet
        att_id = await self._ensure_attendance(emp_odoo, local_str_to_utc_str(punch["punchInTime"]))
        if att_id:
            await self.db.execute("UPDATE punch_clock SET odooId = ? WHERE id = ?", (att_id, punch_id))
            log.info(f"[Outbox] Clock-in: punch {punch_id} -> hr.attendance {att_id}.")
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
        log.info(f"[Outbox] Clock-out: punch {punch_id} (hr.attendance {punch['odooId']}).")
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
            att_id = await self._ensure_attendance(emp_odoo, check_in)
            if not att_id:
                return "retry"
            await self.db.execute("UPDATE punch_clock SET odooId = ? WHERE id = ?", (att_id, punch_id))
            if check_out:
                await self.client.attendance_write(att_id, check_out_utc=check_out)
            log.info(f"[Outbox] Pushed punch {punch_id} edit -> new hr.attendance {att_id}.")
        else:
            await self.client.attendance_write(
                punch["odooId"], check_out_utc=check_out, check_in_utc=check_in
            )
            log.info(f"[Outbox] Pushed punch {punch_id} edit -> hr.attendance {punch['odooId']}.")
        return True

    async def _reassign_worktime(self, worktime_id: int):
        """A worktime moved to a different punch: repoint its Odoo timesheet
        line's shift link at the new punch's attendance. (An unsynced worktime
        needs no action here -- the create path links it to the new attendance
        via its updated punchID.)"""
        wt = await self.db.fetchone(
            "SELECT punchID, odooId FROM work_time WHERE id = ?", (worktime_id,)
        )
        if wt is None or wt["odooId"] is None:
            return True
        punch = await self.db.fetchone(
            "SELECT odooId, legacy FROM punch_clock WHERE id = ?", (wt["punchID"],)
        )
        if punch is None or punch["legacy"]:
            return True
        if punch["odooId"] is None:
            return "retry"  # new attendance not synced yet -- link once it is
        # If the Odoo line was actually deleted (not just orphaned), re-create it
        # rather than repointing a ghost id (self-heals the restore/reassign path).
        if await self.client.read_record("account.analytic.line", wt["odooId"], ["id"]) is None:
            await self.db.execute("UPDATE work_time SET odooId = NULL WHERE id = ?", (worktime_id,))
            return await self._sync_worktime(worktime_id)
        await self.client.set_timesheet_shift(wt["odooId"], punch["odooId"])
        log.info(f"[Outbox] Repointed timesheet {wt['odooId']} shift link to attendance {punch['odooId']}.")
        return True

    async def _sync_worktime_edit(self, worktime_id: int):
        """Push an admin edit of a worktime to its Odoo timesheet line. If the
        line isn't in Odoo yet but now has a work item + hours, create it."""
        wt = await self.db.fetchone(
            "SELECT punchType, timeSpent, odooId, odooProjectId, odooTaskId FROM work_time WHERE id = ?",
            (worktime_id,),
        )
        if wt is None:
            return True
        if wt["odooId"] is None:
            # Not synced yet -- create it only once it has a work item AND hours. A 0h
            # "abandoned" worktime is pushed only by the explicit manual action, never as
            # a side effect of an unrelated edit.
            if wt["odooProjectId"] and wt["timeSpent"]:
                return await self._sync_worktime(worktime_id)
            return True  # local-only / still 0h: nothing to push here
        await self.client.update_timesheet(
            wt["odooId"],
            hours=(wt["timeSpent"] or 0) / 60,
            project_id=wt["odooProjectId"] or None,
            task_id=wt["odooTaskId"],
            description=worktime_description(wt["punchType"], wt["timeSpent"]),
        )
        log.info(f"[Outbox] Updated timesheet {wt['odooId']} (worktime {worktime_id}) in Odoo.")
        return True

    async def _sync_worktime(self, worktime_id: int, *, allow_zero: bool = False):
        wt = await self.db.fetchone(
            "SELECT punchID, punchType, timeSpent, timeStarted, odooId, odooTaskId, odooProjectId "
            "FROM work_time WHERE id = ?",
            (worktime_id,),
        )
        if wt is None or wt["odooId"] is not None:
            return True  # gone or already timesheeted
        punch = await self.db.fetchone(
            "SELECT employeeID, odooId, legacy, punchOutTime FROM punch_clock WHERE id = ?", (wt["punchID"],)
        )
        if punch is None or punch["legacy"]:
            return True  # legacy worktime (historical): never sync
        if not wt["odooProjectId"]:
            # No Odoo work item linked (e.g. created while Odoo was offline).
            # Local record stays authoritative; nothing to post.
            return False
        if not wt["timeSpent"]:
            # Normal paths never post a 0h line (open/in-progress, or an incidental
            # reassign/restore). Only the explicit manual push (allow_zero) posts one,
            # and only for a FINISHED shift -- a 0h "abandoned" placeholder.
            if not (allow_zero and punch["punchOutTime"]):
                return True

        emp_odoo = await self._employee_odoo_id(punch["employeeID"])
        if not emp_odoo:
            return "retry"  # employee not linked to Odoo yet
        # Wait for the parent attendance to sync so we can set the shift link.
        if punch["odooId"] is None:
            return "retry"

        hours = (wt["timeSpent"] or 0) / 60
        work_date = str(wt["timeStarted"])[:10]
        description = worktime_description(wt["punchType"], wt["timeSpent"])
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
            log.info(f"[Outbox] Posted timesheet line {line_id} for worktime {worktime_id} "
                     f"({wt['punchType']}, {hours:.2f}h).")
        return True
