"""/resync _queue_unpushed: sweep + target local-only items, respecting punch legacy."""
import asyncio, os, sys, tempfile
sys.path.insert(0, os.getcwd())
os.environ["LOG_FILE"] = os.path.join(tempfile.mkdtemp(), "b.log")
from cogs.timetracking.db import Database, db_filename, TARGET_VERSION
from cogs.timetracking.cog import TimeTracking
EMP = "phoneNumber,addressLine1,addressCity,addressState,addressZip"

# _queue_unpushed never touches self, so an unbound call with a dummy self is fine.
queue = TimeTracking._queue_unpushed

async def new_db():
    db = await Database(os.path.join(tempfile.mkdtemp(), db_filename(TARGET_VERSION, "timetracker.test"))).setup(company_name="Acme")
    await db.execute(f"INSERT INTO employee (id,name,employeeTypeID,{EMP}) VALUES (1,'Al',2,'5','1','T','TX','0')")
    return db

async def punch(db, pid, odoo=None, legacy=0, pin="2026-09-14 08:00:00"):
    await db.execute("INSERT INTO punch_clock (id,employeeID,punchInTime,punchInApproval,odooId,legacy) "
                     "VALUES (?,?,?,1,?,?)", (pid, 1, pin, odoo, legacy))
async def wtime(db, wid, pid, odoo=None, mins=120, proj=29):
    await db.execute("INSERT INTO work_time (id,punchID,customerID,punchType,timeSpent,timeStarted,detached,odooId,odooProjectId) "
                     "VALUES (?,?,0,'Construction',?,?,0,?,?)", (wid, pid, mins, "2026-09-14 08:00:00", odoo, proj))

async def outbox(db):
    rows = await db.fetchall("SELECT entity_type,entity_id,op FROM odoo_outbox ORDER BY entity_type,entity_id")
    return {(r["entity_type"], r["entity_id"], r["op"]) for r in rows}

async def seed(db):
    await punch(db, 100, odoo=5000, legacy=0)   # synced parent
    await punch(db, 101, odoo=None, legacy=0)   # unsynced non-legacy standalone + parent of W6
    await punch(db, 102, odoo=None, legacy=1)   # legacy parent
    await wtime(db, 200, 100)                    # eligible (parent synced)
    await wtime(db, 201, 100, mins=0)            # zero hours -> skip
    await wtime(db, 202, 100, proj=None)         # no project -> skip
    await wtime(db, 203, 102)                    # parent legacy -> skip
    await wtime(db, 204, 100, odoo=9000)         # already synced -> skip
    await wtime(db, 205, 101)                    # eligible; parent unsynced -> also queue punch 101

async def main():
    # CASE 1 — full sweep
    db = await new_db(); await seed(db)
    p, w = await queue(object(), db)
    assert (p, w) == (1, 2), (p, w)
    assert await outbox(db) == {("punch", 101, "edit"), ("worktime", 200, "create"), ("worktime", 205, "create")}
    # idempotent second run
    assert await queue(object(), db) == (0, 0)
    print("OK: full sweep queues unsynced punch + 2 eligible worktimes; dedups; skips legacy/zero/no-project/synced")

    # CASE 2 — target a worktime whose parent punch is unsynced -> queues the punch too
    db = await new_db(); await seed(db)
    p, w = await queue(object(), db, only_worktime=205)
    assert (p, w) == (1, 1), (p, w)
    assert await outbox(db) == {("punch", 101, "edit"), ("worktime", 205, "create")}
    print("OK: targeting a worktime also queues its unsynced parent punch")

    # CASE 3 — target a worktime on a legacy punch -> nothing
    db = await new_db(); await seed(db)
    assert await queue(object(), db, only_worktime=203) == (0, 0)
    assert await outbox(db) == set()
    print("OK: targeting a legacy-punch worktime queues nothing")

    # CASE 4 — target a specific punch
    db = await new_db(); await seed(db)
    assert await queue(object(), db, only_punch=101) == (1, 0)
    assert await outbox(db) == {("punch", 101, "edit")}
    # legacy punch target -> nothing
    assert await queue(object(), db, only_punch=102) == (0, 0)
    print("OK: targeting a punch queues just it; legacy punch refused")

    print("\nQUEUE-UNPUSHED VERIFICATION PASSED")

asyncio.run(main())
sys.stdout.flush()
os._exit(0)  # skip joining non-daemon aiosqlite connection threads at interpreter shutdown
