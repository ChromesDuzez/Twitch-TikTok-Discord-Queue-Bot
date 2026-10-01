"""/editworktime: push gate (local-only non-legacy), customer block, task/project rules."""
import asyncio, os, sys, tempfile, types
sys.path.insert(0, os.getcwd())
os.environ["LOG_FILE"] = os.path.join(tempfile.mkdtemp(), "b.log")
from cogs.timetracking.db import Database, db_filename, TARGET_VERSION
from cogs.timetracking.cog import TimeTracking
EMP = "phoneNumber,addressLine1,addressCity,addressState,addressZip"

EDIT = TimeTracking.editworktime.callback  # raw coroutine behind the slash command

class FakeClient:
    def __init__(s, loaded=True): s.loaded = loaded
    async def read_record(s, model, odoo_id, fields):
        if model == "project.project":
            return {"id": odoo_id, "partner_id": [555, "Cust"]} if odoo_id in (29, 40) else None
        if model == "project.task":
            proj = {900: 29, 901: 40}.get(odoo_id)
            return None if proj is None else {"id": odoo_id, "project_id": [proj, "P"], "partner_id": [555, "Cust"]}
        return None

class FakeCtx:
    def __init__(s): s.responses = []; s.author = types.SimpleNamespace(__str__=lambda self: "admin", id=1)
    async def defer(s, **k): pass
    async def respond(s, msg, **k): s.responses.append(msg)

def mk_self(db, loaded=True):
    s = types.SimpleNamespace()
    s.client = FakeClient(loaded)
    async def _ensure_db(): return db
    s._ensure_db = _ensure_db
    s._eph = lambda ctx: False
    async def _refresh_clock(e): pass
    s._refresh_clock = _refresh_clock
    s._resolve_worktime_link = TimeTracking._resolve_worktime_link.__get__(s)
    return s

async def new_db():
    db = await Database(os.path.join(tempfile.mkdtemp(), db_filename(TARGET_VERSION, "timetracker.test"))).setup(company_name="Acme")
    await db.execute(f"INSERT INTO employee (id,name,employeeTypeID,{EMP}) VALUES (1,'Al',2,'5','1','T','TX','0')")
    await db.execute("INSERT INTO customer (id,name,odooId) VALUES (7,'Cust',555)")
    await db.execute("INSERT INTO punch_clock (id,employeeID,punchInTime,punchInApproval,odooId,legacy) VALUES (100,1,'2026-09-14 08:00:00',1,5000,0)")
    await db.execute("INSERT INTO punch_clock (id,employeeID,punchInTime,punchInApproval,odooId,legacy) VALUES (102,1,'2026-09-14 08:00:00',1,NULL,1)")
    return db

async def wt(db, wid, pid=100, proj=29, task=None, mins=0, cust=0):
    await db.execute("INSERT INTO work_time (id,punchID,customerID,punchType,timeSpent,timeStarted,detached,odooId,odooProjectId,odooTaskId) "
                     "VALUES (?,?,?,'Construction',?,?,0,NULL,?,?)", (wid, pid, cust, mins, "2026-09-14 08:00:00", proj, task))

async def row(db, wid): return await db.fetchone("SELECT timeSpent,customerID,odooProjectId,odooTaskId FROM work_time WHERE id=?", (wid,))
async def edits(db, wid): return (await db.fetchone("SELECT COUNT(*) c FROM odoo_outbox WHERE entity_type='worktime' AND entity_id=?", (wid,)))["c"]

async def call(db, loaded=True, **kw):
    ctx = FakeCtx()
    defaults = dict(worktype=None, hours=None, customer=None, task=None, project=None, punch=None)
    defaults.update(kw)
    await EDIT(mk_self(db, loaded), ctx, **defaults)
    return ctx

async def main():
    # (a) hours edit on a local-only, non-legacy worktime -> enqueues worktime/edit
    db = await new_db(); await wt(db, 200, pid=100, proj=29, mins=0)
    await call(db, worktime="200", hours=2.0)
    assert (await row(db, 200))["timeSpent"] == 120
    assert await edits(db, 200) == 1, "should enqueue worktime edit"
    print("OK: hours edit on local-only non-legacy worktime pushes (worktime/edit enqueued)")

    # (a2) same, but the parent punch is legacy -> no push
    db = await new_db(); await wt(db, 203, pid=102, proj=29, mins=0)
    await call(db, worktime="203", hours=2.0)
    assert (await row(db, 203))["timeSpent"] == 120, "local edit still applies"
    assert await edits(db, 203) == 0, "legacy-punch worktime must NOT push"
    print("OK: legacy-punch worktime edits locally but never enqueues")

    # (b) customer change while Odoo connected -> rejected, nothing changes
    db = await new_db(); await wt(db, 200, mins=60, cust=0)
    ctx = await call(db, worktime="200", customer="7")
    assert any("follows its Odoo task/project" in m for m in ctx.responses), ctx.responses
    assert (await row(db, 200))["customerID"] == 0 and await edits(db, 200) == 0
    print("OK: customer change blocked while Odoo connected")

    # (c) project only -> set project, clear task, customer follows project.partner_id
    db = await new_db(); await wt(db, 200, proj=29, task=900, mins=60, cust=0)
    await call(db, worktime="200", project="40")
    r = await row(db, 200)
    assert r["odooProjectId"] == 40 and r["odooTaskId"] is None and r["customerID"] == 7, dict(r)
    assert await edits(db, 200) == 1
    print("OK: project-only sets project, clears task, auto-sets customer")

    # (d) project+task mismatch -> reject; matching -> set both
    db = await new_db(); await wt(db, 200, proj=29, mins=60)
    ctx = await call(db, worktime="200", project="29", task="901")   # 901 is in project 40
    assert any("isn't in the selected project" in m for m in ctx.responses), ctx.responses
    assert (await row(db, 200))["odooTaskId"] is None
    db = await new_db(); await wt(db, 200, proj=29, mins=60)
    await call(db, worktime="200", project="29", task="900")         # 900 is in project 29
    r = await row(db, 200); assert r["odooProjectId"] == 29 and r["odooTaskId"] == 900, dict(r)
    print("OK: project+task validated against each other (mismatch rejected, match sets both)")

    # (e) task only -> validated against the worktime's current project
    db = await new_db(); await wt(db, 200, proj=29, mins=60)
    await call(db, worktime="200", task="900")                       # 900 in current project 29
    assert (await row(db, 200))["odooTaskId"] == 900
    db = await new_db(); await wt(db, 200, proj=29, mins=60)
    ctx = await call(db, worktime="200", task="901")                 # 901 not in project 29
    assert any("isn't in this worktime's project" in m for m in ctx.responses), ctx.responses
    print("OK: task-only validated against the current project")

    print("\nEDITWORKTIME-RULES VERIFICATION PASSED")

asyncio.run(main())
sys.stdout.flush()
os._exit(0)  # skip joining non-daemon aiosqlite connection threads at interpreter shutdown
