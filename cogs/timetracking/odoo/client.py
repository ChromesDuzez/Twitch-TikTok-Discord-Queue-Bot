"""Odoo JSON-2 external API client.

Ported from the old ``test env/OdooExternalAPI.py`` prototype. Two important
differences from the prototype:

* **No import-time side effects.** The prototype ran live API calls and prints
  at module import; this class does nothing until you call it.
* **Non-blocking.** Every HTTP call goes through ``asyncio.to_thread`` so the
  synchronous ``requests`` call never blocks the Discord event loop, and every
  request has a timeout.

SQLite remains authoritative. This client is best-effort: when Odoo is not
configured (``loaded is False``) or a call fails, the caller degrades
gracefully rather than breaking the bot.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date, timedelta

import requests

import config
from botlog import log

DEFAULT_TIMEOUT = 15


def shift_field() -> str:
    """The Many2one field on account.analytic.line linking to hr.attendance."""
    return os.getenv("ODOO_SHIFT_FIELD", "x_studio_shift")


class OdooClient:
    def __init__(self, url: str | None, db: str | None, username: str | None, api_key: str | None):
        self.url = url
        self.db = db
        self.username = username
        self.api_key = api_key
        self.loaded = bool(url and db and username and api_key)
        # Set by check_shift_field(): whether the Studio shift field exists.
        # Deletion support is gated on this (see services/webhook.py).
        self.shift_field_available = False

    # ---- core --------------------------------------------------------------

    def _call_sync(self, endpoint: str, data: dict):
        # Keep the webhook IP allowlist current with the host we actually reach.
        config.record_odoo_ips(self.url)
        data = dict(data)
        data["context"] = {"lang": "en_US"}
        response = requests.post(
            f"{self.url}{endpoint}",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "X-Odoo-Database": f"{self.db}",
            },
            json=data,
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code == 500:
            try:
                error_data = response.json()
                log.error("[Odoo] API Error Details:")
                for key, value in error_data.items():
                    log.error(f"  {key}: {value}")
            except json.JSONDecodeError:
                log.error(f"[Odoo] Response not JSON. Raw: {response.text}")
            raise RuntimeError(f"Odoo API request to {endpoint} failed (500).")
        response.raise_for_status()
        return response.json()

    async def call(self, endpoint: str, data: dict):
        """Async, non-blocking API call. Returns None when Odoo isn't configured."""
        if not self.loaded:
            return None
        return await asyncio.to_thread(self._call_sync, endpoint, data)

    async def verify(self) -> bool:
        """Lightweight connectivity check used at startup (not for auth)."""
        if not self.loaded:
            return False
        try:
            await self.call(
                "/res.partner/search_read",
                {"domain": [["id", "=", 1]], "fields": ["id"], "limit": 1},
            )
            return True
        except Exception as e:  # noqa: BLE001 - best-effort probe
            log.warning(f"[Odoo] Verification failed: {e}")
            return False

    async def field_exists(self, model: str, field: str) -> bool:
        """True if a field is defined on a model (e.g. a Studio custom field)."""
        try:
            rows = await self.call(
                "/ir.model.fields/search_read",
                {"domain": [["model", "=", model], ["name", "=", field]], "fields": ["id"], "limit": 1},
            )
            return bool(rows)
        except Exception as e:  # noqa: BLE001 - best-effort probe
            log.warning(f"[Odoo] field_exists probe failed: {e}")
            return False

    async def check_shift_field(self) -> bool:
        """Probe for the shift Studio field and cache the result. Gates deletions."""
        if not self.loaded:
            self.shift_field_available = False
            return False
        available = await self.field_exists("account.analytic.line", shift_field())
        if available != self.shift_field_available:
            log.warning("[Odoo] Shift field '%s' %s — deletion support %s.",
                        shift_field(), "found" if available else "missing",
                        "enabled" if available else "DISABLED")
        self.shift_field_available = available
        return available

    async def unlink(self, model: str, odoo_id: int):
        """Delete a record in Odoo by id."""
        return await self.call(f"/{model}/unlink", {"ids": [odoo_id]})

    async def read_record(self, model: str, odoo_id: int, fields: list):
        """Fetch a single record's fields by id (used by inbound reconcile).

        Returns the record dict, or None if it no longer exists (e.g. deleted).
        """
        data = await self.call(
            f"/{model}/search_read",
            {"domain": [["id", "=", odoo_id]], "fields": fields, "limit": 1},
        )
        return data[0] if data else None

    # ---- partners / customers ---------------------------------------------

    async def search_partners_by_name(self, name: str, limit: int = 10):
        return await self.call(
            "/res.partner/search_read",
            {
                "domain": [["display_name", "ilike", name]],
                "fields": ["id", "company_type", "display_name"],
                "limit": limit,
            },
        )

    async def get_customer_list(self):
        """All Odoo customers (res.partner with customer_rank > 0), for linking."""
        return await self.call(
            "/res.partner/search_read",
            {
                "domain": [["customer_rank", ">", 0], ["active", "=", True]],
                "fields": ["id", "display_name"],
                # display_name is a non-stored computed field in Odoo 19, so it
                # can't be used in ORDER BY (SQL). Sort by the stored name.
                "order": "name asc",
            },
        )

    async def create_partner(self, name: str, block_duplicate: bool = True):
        if block_duplicate:
            existing = await self.search_partners_by_name(name)
            if existing:
                log.info(f"[Odoo] Partner '{name}' already exists; skipping create.")
                return existing[0]
        return await self.call("/res.partner/name_create", {"name": name})

    # ---- employees ---------------------------------------------------------

    async def get_employee_by_id(self, employee_id: int):
        data = await self.call(
            "/hr.employee/search_read",
            {
                "domain": [["active", "=", True], ["id", "=", employee_id]],
                "fields": ["id", "display_name"],
            },
        )
        return data[0] if data else None

    async def get_employee_list(self):
        return await self.call(
            "/hr.employee/search_read",
            {"domain": [["active", "=", True]], "fields": ["id", "display_name"], "order": "id asc"},
        )

    # ---- attendance (clock in/out) ----------------------------------------

    async def attendance_create(self, employee_odoo_id: int, check_in_utc: str):
        """Create an hr.attendance check-in. Returns the new attendance id."""
        result = await self.call(
            "/hr.attendance/create",
            {"vals_list": [{"employee_id": employee_odoo_id, "check_in": check_in_utc}]},
        )
        if isinstance(result, list) and result:
            return result[0]
        return result

    async def attendance_write(self, attendance_id: int, check_out_utc: str | None = None,
                               check_in_utc: str | None = None):
        """Update an existing attendance's check-in and/or check-out time."""
        vals = {}
        if check_in_utc is not None:
            vals["check_in"] = check_in_utc
        if check_out_utc is not None:
            vals["check_out"] = check_out_utc
        if not vals:
            return None
        return await self.call(
            "/hr.attendance/write", {"ids": [attendance_id], "vals": vals}
        )

    # ---- work-item search (task / project) --------------------------------

    async def search_tasks_for_partner(self, partner_id: int, name: str = "", limit: int = 40):
        """Open project tasks belonging to a customer (res.partner), for linking
        a manually-added worktime to Odoo.

        Returns rows with id, display_name, project_id, planned_date_begin. The
        caller ranks them by planned-start proximity to the punch time.
        """
        domain = [["partner_id", "=", partner_id], ["is_closed", "=", False]]
        if name:
            domain.append(["name", "ilike", name])  # stored field (display_name isn't)
        return await self.call(
            "/project.task/search_read",
            {
                "domain": domain,
                "fields": ["id", "display_name", "project_id", "planned_date_begin"],
                "order": "planned_date_begin asc",
                "limit": limit,
            },
        )

    async def get_task_project(self, task_id: int):
        """The project_id (int) a task belongs to, or None."""
        rec = await self.read_record("project.task", task_id, ["project_id"])
        if rec and rec.get("project_id"):
            pid = rec["project_id"]
            return pid[0] if isinstance(pid, (list, tuple)) else pid
        return None

    async def search_service_tasks(self, name: str, project_id: int, months: int = 6, limit: int = 15):
        """Open Field Service tasks for a customer whose deadline is within
        ``months`` months (past or future) of today.

        Returns rows with id, display_name, partner_id, project_id, date_deadline.
        Tasks without a deadline are excluded by the date window.
        """
        today = date.today()
        window = timedelta(days=months * 31)
        low = (today - window).strftime("%Y-%m-%d")
        high = (today + window).strftime("%Y-%m-%d")
        return await self.call(
            "/project.task/search_read",
            {
                "domain": [
                    ["partner_id.display_name", "ilike", name],
                    ["project_id", "=", project_id],
                    ["is_closed", "=", False],
                    ["date_deadline", ">=", low],
                    ["date_deadline", "<=", high],
                ],
                "fields": ["id", "display_name", "partner_id", "project_id", "date_deadline"],
                "order": "date_deadline asc",
                "limit": limit,
            },
        )

    async def search_construction_projects(self, name: str, exclude_ids=(), limit: int = 15):
        """Active projects assigned to a customer (the construction 'jobs'),
        excluding the Field Service / Office project ids.

        Returns rows with id, display_name, partner_id.
        """
        domain = [
            ["active", "=", True],
            ["partner_id.display_name", "ilike", name],
        ]
        exclude_ids = [i for i in exclude_ids if i]
        if exclude_ids:
            domain.append(["id", "not in", exclude_ids])
        return await self.call(
            "/project.project/search_read",
            {
                "domain": domain,
                "fields": ["id", "display_name", "partner_id"],
                "order": "id desc",
                "limit": limit,
            },
        )

    # ---- timesheets --------------------------------------------------------

    async def add_timesheet(
        self, project_id: int, date: str, employee_odoo_id: int, description: str,
        hours: float, task_id: int | None = None, shift_attendance_id: int | None = None,
    ):
        """Create one account.analytic.line (timesheet). ``company_id`` is read
        from the task when given, otherwise from the project.

        ``shift_attendance_id`` sets the Studio shift link (only when the field
        exists in this Odoo). Used for Service (task_id set), Construction and
        Office (project only). Returns the new analytic-line id.
        """
        company_id = False
        if task_id:
            task = await self.call(
                "/project.task/search_read",
                {"domain": [["id", "=", task_id]], "fields": ["company_id", "project_id"], "limit": 1},
            )
            if not task:
                raise ValueError(f"No Odoo task found with id {task_id}.")
            company_id = task[0]["company_id"]
            if not project_id and task[0]["project_id"]:
                project_id = task[0]["project_id"][0]
        else:
            proj = await self.call(
                "/project.project/search_read",
                {"domain": [["id", "=", project_id]], "fields": ["company_id"], "limit": 1},
            )
            if not proj:
                raise ValueError(f"No Odoo project found with id {project_id}.")
            company_id = proj[0]["company_id"]

        vals = {
            "name": description,
            "date": date,
            "unit_amount": hours,
            "product_uom_id": 4,  # Hours
            "employee_id": employee_odoo_id,
            "company_id": company_id[0] if isinstance(company_id, (list, tuple)) else company_id,
            "validated_status": "draft",
            "project_id": project_id,
        }
        if task_id:
            vals["task_id"] = task_id
        # Link the timesheet back to its attendance (shift) when the Studio
        # field is available, so it round-trips and supports deletions.
        if shift_attendance_id and self.shift_field_available:
            vals[shift_field()] = shift_attendance_id
        result = await self.call("/account.analytic.line/create", {"vals_list": [vals]})
        if isinstance(result, list) and result:
            return result[0]
        return result

    async def update_timesheet(self, line_id: int, hours=None, project_id=None,
                               task_id=None, description=None):
        """Update an existing account.analytic.line (timesheet). Only the passed
        fields are written; ``task_id=None`` leaves the task untouched. Returns
        the line id."""
        vals = {}
        if hours is not None:
            vals["unit_amount"] = hours
            vals["product_uom_id"] = 4  # Hours
        if project_id:
            vals["project_id"] = project_id
        if task_id is not None:
            vals["task_id"] = task_id or False
        if description is not None:
            vals["name"] = description
        if not vals:
            return line_id
        await self.call("/account.analytic.line/write", {"ids": [line_id], "vals": vals})
        return line_id

    async def set_timesheet_shift(self, line_id: int, attendance_id: int):
        """Point a timesheet line's Studio shift field at a different attendance
        (used when a worktime is reassigned to another punch). No-op when the
        shift field isn't available in this Odoo."""
        if not self.shift_field_available:
            return line_id
        await self.call(
            "/account.analytic.line/write",
            {"ids": [line_id], "vals": {shift_field(): attendance_id}},
        )
        return line_id
