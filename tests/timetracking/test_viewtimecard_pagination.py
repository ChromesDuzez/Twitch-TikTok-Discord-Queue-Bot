"""In-week pagination for /viewtimecard: punch blocks stay together; only an oversized
punch splits (with a re-stated `(con't)` header); never cut mid-link."""
import types
from datetime import datetime

from cogs.timetracking.views import build_timecard_embed, _paginate_blocks, TimecardWeekView
from cogs.timetracking.odoo.client import OdooClient
from _helpers import asynctest, db_ctx, add_punch, add_worktime

WEEK_END = datetime(2026, 9, 12)   # window 09-06 .. 09-13


def _cog(db):
    return types.SimpleNamespace(db=db, client=OdooClient("https://x.odoo.com/json/2", "db", "u", "k"))


def _block(label, items, item_len=200):
    return [f"**▸ {label}**"] + [f"　• {label}.{j} " + "y" * item_len for j in range(items)]


# ---- the block paginator ----------------------------------------------------

def test_small_blocks_share_a_page():
    assert _paginate_blocks([["a", "b"], ["c"]], "H") == ["a\nb\nc"]
    assert _paginate_blocks([], "H") == [""]


def test_block_moves_whole_to_next_page_when_it_wont_fit():
    b1, b2 = _block("A", 12), _block("B", 12)   # each ~2.5k chars; two won't share a page
    pages = _paginate_blocks([b1, b2], "H")
    assert len(pages) == 2
    assert pages[0].split("\n") == b1           # block A intact on page 1
    assert pages[1].split("\n") == b2           # block B intact on page 2 (not split)


def test_oversized_block_splits_with_cont_header():
    big = _block("X", 60)                        # far more than one page holds
    pages = _paginate_blocks([big], "H")
    assert len(pages) > 1
    head = big[0]
    assert pages[0].split("\n")[0] == head
    for p in pages[1:]:
        assert p.split("\n")[0] == head + " _(con't)_"   # continuation restates the header
    # every worktime item appears exactly once, in order, across the pages
    seen = [ln for p in pages for ln in p.split("\n") if ln not in (head, head + " _(con't)_")]
    assert seen == big[1:]


# ---- end-to-end through the embed builder -----------------------------------

async def _seed_many_punches(db):
    """Five day-punches, each with 8 hyperlinked worktimes — enough to need pages."""
    days = [(100, "2026-09-07"), (101, "2026-09-08"), (102, "2026-09-09"),
            (103, "2026-09-10"), (104, "2026-09-11")]
    for pk, d in days:
        await add_punch(db, pk, odoo=5000 + pk, pin=f"{d} 08:00:00", pout=f"{d} 17:00:00")
        for j in range(8):
            wid = pk * 100 + j
            await add_worktime(db, wid, pk, odoo=70000 + wid, mins=30, ptype="Service",
                               project=2, task=6000 + wid, customer=7)
    return [pk for pk, _ in days]


@asynctest
async def test_each_punch_block_stays_on_one_page():
    async with db_ctx() as db:
        pks = await _seed_many_punches(db)
        _, total = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=0)
        assert total > 1
        texts = []
        for pg in range(total):
            e, _t = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=pg)
            assert len(e.description) <= 4096
            body = e.description.split("\n")[1:]        # drop the Σ header line
            assert body[0].startswith("**▸ #")          # a page never starts with an orphaned worktime

        # map each punch + worktime id to the page(s) it appears on
        descs = [(await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=pg))[0].description
                 for pg in range(total)]
        for pk in pks:
            on = [i for i, d in enumerate(descs) if f"#{pk} " in d]
            assert len(on) == 1, (pk, on)               # punch header on exactly one page
            pg = on[0]
            for j in range(8):
                wid = pk * 100 + j
                assert [i for i, d in enumerate(descs) if f"#{wid} " in d] == [pg], (pk, wid)


@asynctest
async def test_oversized_single_punch_week_keeps_all_entries():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000, pin="2026-09-11 08:00:00", pout="2026-09-11 16:30:00")
        ids = list(range(200, 245))                     # 45 worktimes on ONE punch
        for i in ids:
            await add_worktime(db, i, 100, odoo=7000 + i, mins=15, ptype="Service",
                               project=2, task=6900 + i, customer=7)
        _, total = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=0)
        assert total > 1
        seen = ""
        for pg in range(total):
            e, _t = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=pg)
            assert len(e.description) <= 4096
            assert e.description.split("\n")[1].startswith("**▸ #100")   # header (or con't) leads
            for line in e.description.split("\n"):
                if "](" in line:
                    assert line.rstrip().endswith(")"), line               # never cut mid-link
            seen += e.description
        for i in ids:
            assert f"#{i} " in seen, f"worktime #{i} fell off every page"


@asynctest
async def test_page_out_of_range_clamps():
    async with db_ctx() as db:
        await _seed_many_punches(db)
        _, total = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=0)
        embed, _t = await build_timecard_embed(_cog(db), 1, "Tester", WEEK_END, page=999)
        assert f"page {total}/{total}" in embed.title


# ---- the view wiring (page buttons) -----------------------------------------

@asynctest
async def test_view_tracks_pages_and_button_states():
    async with db_ctx() as db:
        await _seed_many_punches(db)
        view = TimecardWeekView(_cog(db), 1, "Tester", WEEK_END, 999)
        await view.render_embed()
        assert view.total_pages > 1
        assert view._page_btns["pageprev"].disabled is True      # first page
        assert view._page_btns["pagenext"].disabled is False
        view.page = view.total_pages - 1
        await view.render_embed()
        assert view._page_btns["pageprev"].disabled is False     # last page
        assert view._page_btns["pagenext"].disabled is True
        view.stop()


@asynctest
async def test_single_page_week_disables_both_page_buttons():
    async with db_ctx() as db:
        await add_punch(db, 100, odoo=5000, pin="2026-09-11 08:00:00", pout="2026-09-11 16:30:00")
        await add_worktime(db, 200, 100, odoo=7001, mins=60, ptype="Service", project=2, task=6950, customer=7)
        view = TimecardWeekView(_cog(db), 1, "Tester", WEEK_END, 999)
        await view.render_embed()
        assert view.total_pages == 1
        assert view._page_btns["pageprev"].disabled and view._page_btns["pagenext"].disabled
        view.stop()
