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
from datetime import date, timedelta

import requests

DEFAULT_TIMEOUT = 15


class OdooClient:
    def __init__(self, url: str | None, db: str | None, username: str | None, api_key: str | None):
        self.url = url
        self.db = db
        self.username = username
        self.api_key = api_key
        self.loaded = bool(url and db and username and api_key)

    # ---- core --------------------------------------------------------------

    def _call_sync(self, endpoint: str, data: dict):
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
                print("[Odoo] API Error Details:")
                for key, value in error_data.items():
                    print(f"  {key}: {value}")
            except json.JSONDecodeError:
                print(f"[Odoo] Response not JSON. Raw: {response.text}")
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
            print(f"[Odoo] Verification failed: {e}")
            return False

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

    async def create_partner(self, name: str, block_duplicate: bool = True):
        if block_duplicate:
            existing = await self.search_partners_by_name(name)
            if existing:
                print(f"[Odoo] Partner '{name}' already exists; skipping create.")
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

    async def attendance_write(self, attendance_id: int, check_out_utc: str):
        """Set the check-out time on an existing attendance record."""
        return await self.call(
            "/hr.attendance/write",
            {"ids": [attendance_id], "vals": {"check_out": check_out_utc}},
        )

    # ---- work-item search (task / project) --------------------------------

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
        hours: float, task_id: int | None = None,
    ):
        """Create one account.analytic.line (timesheet). ``company_id`` is read
        from the task when given, otherwise from the project.

        Used for Service (task_id set), Construction and Office (project only).
        Returns the new analytic-line id.
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
        result = await self.call("/account.analytic.line/create", {"vals_list": [vals]})
        if isinstance(result, list) and result:
            return result[0]
        return result
