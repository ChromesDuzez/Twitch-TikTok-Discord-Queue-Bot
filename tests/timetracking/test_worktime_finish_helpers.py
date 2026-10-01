"""Shared worktime-finish helpers: round_quarter_hours + finalize_worktime."""
from cogs.timetracking.views import round_quarter_hours, finalize_worktime
from _helpers import asynctest, db_ctx, add_punch, add_worktime, outbox


def _old_formula(h):  # what End-Work-Now used before the refactor
    return (round(h * 4) / 4) or 0.25


def test_round_quarter_matches_old_for_nonnegative():
    for h in [0, 0.01, 0.12, 0.13, 0.24, 0.25, 0.3, 1.0, 3.2167, 7.9, 23.9, 30.0]:
        assert round_quarter_hours(h) == _old_formula(h), h


def test_round_quarter_floors_negatives():
    assert round_quarter_hours(-1.0) == 0.25
    assert round_quarter_hours(-0.1) == 0.25


@asynctest
async def test_finalize_writes_minutes_and_enqueues():
    async with db_ctx() as db:
        await add_punch(db, 1, odoo=5000)
        await add_worktime(db, 10, 1, mins=0)
        assert await finalize_worktime(db, 10, 3.25) == 195
        row = await db.fetchone("SELECT timeSpent FROM work_time WHERE id=10")
        assert row["timeSpent"] == 195
        assert ("worktime", 10, "create") in await outbox(db)


@asynctest
async def test_finalize_caps_at_24h():
    async with db_ctx() as db:
        await add_punch(db, 1, odoo=5000)
        await add_worktime(db, 11, 1, mins=0)
        assert await finalize_worktime(db, 11, 30.0) == 1440
        row = await db.fetchone("SELECT timeSpent FROM work_time WHERE id=11")
        assert row["timeSpent"] == 1440
