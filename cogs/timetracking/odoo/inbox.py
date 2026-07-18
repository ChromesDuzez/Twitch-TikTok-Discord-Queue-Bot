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
from .client import OdooClient

SUPPORTED_MODELS = ("res.partner", "hr.attendance", "account.analytic.line")
DRAIN_INTERVAL = 5  # seconds; short so Odoo edits reflect in Discord quickly
MAX_ATTEMPTS = 5


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
            changed = await self._reconcile(row["model"], row["odoo_id"])
            await self.db.execute("UPDATE odoo_inbox SET status = 'done' WHERE id = ?", (row["id"],))
            if changed:
                log.info(f"[Inbox] Reconciled {row['model']}:{row['odoo_id']} (changed).")
            else:
                log.debug(f"[Inbox] {row['model']}:{row['odoo_id']} already up to date (echo/no-op).")
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

    # ---- per-model reconcilers (idempotent) --------------------------------

    async def _reconcile_partner(self, odoo_id: int) -> bool:
        rec = await self.client.read_record("res.partner", odoo_id, ["id", "display_name"])
        if rec is None:
            return False  # deleted in Odoo; leave local customer as-is
        name = rec["display_name"]
        row = await self.db.fetchone("SELECT id, name FROM customer WHERE odooId = ?", (odoo_id,))
        if row is None:
            await self.db.execute("INSERT INTO customer (name, odooId) VALUES (?, ?)", (name, odoo_id))
            return True
        if row["name"] != name:
            await self.db.execute("UPDATE customer SET name = ? WHERE id = ?", (name, row["id"]))
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

    async def _reconcile_analytic_line(self, odoo_id: int) -> bool:
        rec = await self.client.read_record(
            "account.analytic.line", odoo_id, ["id", "unit_amount"]
        )
        if rec is None:
            return False
        wt = await self.db.fetchone(
            "SELECT id, timeSpent FROM work_time WHERE odooId = ?", (odoo_id,)
        )
        if wt is None:
            log.debug(f"[Inbox] No local worktime for analytic.line {odoo_id}; skipping.")
            return False
        # Round hours to our quarter-hour minute grid.
        minutes = int(round((rec.get("unit_amount") or 0) * 4) / 4 * 60)
        if minutes == wt["timeSpent"]:
            return False
        await self.db.execute("UPDATE work_time SET timeSpent = ? WHERE id = ?", (minutes, wt["id"]))
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
