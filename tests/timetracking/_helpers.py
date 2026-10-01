"""Shared helpers for the timetracking tests.

Tests are plain pytest functions wrapped with ``@asynctest`` (so no pytest-asyncio
plugin is needed). Each gets a fresh throwaway SQLite DB from ``db_ctx``, which is
closed on exit so aiosqlite's connection thread stops and the interpreter doesn't
hang at shutdown. All seed data here is synthetic.
"""
import asyncio
import functools
import os
import tempfile
from contextlib import asynccontextmanager

from cogs.timetracking.db import Database, db_filename, TARGET_VERSION

_EMP_COLS = "phoneNumber,addressLine1,addressCity,addressState,addressZip"


def asynctest(fn):
    """Turn an ``async def`` test into a sync pytest test via ``asyncio.run``."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))
    return wrapper


async def new_db(*, with_customer=True, employee_odoo=50):
    """A migrated, empty timecard DB in a temp dir, seeded with one employee
    (local id 1, Odoo id ``employee_odoo``) and optionally one customer
    (local id 7, Odoo partner 555)."""
    db = await Database(
        os.path.join(tempfile.mkdtemp(), db_filename(TARGET_VERSION, "test"))
    ).setup(company_name="TestCo")
    await db.execute(
        f"INSERT INTO employee (id,name,employeeTypeID,odooId,{_EMP_COLS}) "
        "VALUES (1,'Tester',2,?,'5','1','Town','TX','0')", (employee_odoo,))
    if with_customer:
        await db.execute("INSERT INTO customer (id,name,odooId) VALUES (7,'TestCust',555)")
    return db


@asynccontextmanager
async def db_ctx(**kwargs):
    db = await new_db(**kwargs)
    try:
        yield db
    finally:
        await db.close()


async def add_punch(db, pid, *, odoo=None, legacy=0, pin="2026-09-14 08:00:00", pout=None):
    await db.execute(
        "INSERT INTO punch_clock (id,employeeID,punchInTime,punchOutTime,punchInApproval,odooId,legacy) "
        "VALUES (?,1,?,?,1,?,?)", (pid, pin, pout, odoo, legacy))


async def add_worktime(db, wid, pid, *, odoo=None, mins=0, project=29, task=None,
                       ptype="Construction", started="2026-09-14 08:00:00", customer=0):
    await db.execute(
        "INSERT INTO work_time (id,punchID,customerID,punchType,timeSpent,timeStarted,detached,"
        "odooId,odooProjectId,odooTaskId) VALUES (?,?,?,?,?,?,0,?,?,?)",
        (wid, pid, customer, ptype, mins, started, odoo, project, task))


async def outbox(db, entity_type=None):
    """The outbox as a set of (entity_type, entity_id, op) tuples."""
    if entity_type:
        rows = await db.fetchall(
            "SELECT entity_type,entity_id,op FROM odoo_outbox WHERE entity_type=?", (entity_type,))
    else:
        rows = await db.fetchall("SELECT entity_type,entity_id,op FROM odoo_outbox")
    return {(r["entity_type"], r["entity_id"], r["op"]) for r in rows}
