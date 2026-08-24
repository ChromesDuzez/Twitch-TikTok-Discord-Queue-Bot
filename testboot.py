"""Phase 2 of the test workflow: bootstrapping a fresh test database.

When the bot starts in TESTING mode and no test database exists, the startup
flow (in main.py) prompts — in the timecard-admin channel and the console — to
either pull production data or start from an empty baseline (5 min with no answer
=> scratch). This module holds the non-interactive machinery:

* detecting whether a test db is present + locating the prod db to copy,
* copying + sanitizing prod into the test db,
* building the ``timecards`` category with one clock channel per **active**
  employee (one who had a clock set in prod and isn't archived), and
* the Discord Yes/No view used by the prompt.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import unicodedata

import discord

import config
from botlog import log
from cogs.timetracking.db import (TARGET_VERSION, Database, db_dir, db_filename,
                                  resolve_db_path)
from cogs.timetracking.perms import is_timecard_admin_member

# Stripped from a pulled prod copy so no prod channel/message/Odoo ids or queued
# sync work leak into the test bot. (Mirrors cogs/testing.py's SANITIZE_SQL.)
_SANITIZE_SQL = (
    "UPDATE employee    SET clockChannelId = NULL, clockMessageId = NULL, odooId = NULL",
    "UPDATE punch_clock SET checkChannelId = NULL, checkMessageId = NULL, odooId = NULL",
    "UPDATE customer    SET odooId = NULL",
    "UPDATE work_time   SET odooId = NULL, odooTaskId = NULL, odooProjectId = NULL",
    "DELETE FROM odoo_outbox",
    "DELETE FROM odoo_inbox",
)

_CATEGORY_KEY = "TESTING_TIMECARD_CATEGORY_ID"
_CATEGORY_NAME = "timecards"


# ---- detection / paths -----------------------------------------------------

def test_db_present(base: str | None = None) -> bool:
    """True if a test database already exists (so no bootstrap is needed)."""
    base = base or os.getcwd()
    target, source = resolve_db_path(base, "timetracker.test")
    return source is not None or os.path.exists(target)


def prod_db_file(base: str | None = None) -> str | None:
    """Path to the production database file to copy from, or None if there isn't one."""
    base = base or os.getcwd()
    target, source = resolve_db_path(base, "timetracker")
    if os.path.exists(target):
        return target
    return source


# ---- channel naming --------------------------------------------------------

def _slug(name: str) -> str:
    # Fold accents to ASCII (Díaz -> Diaz) so channel names stay clean.
    ascii_name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return s[:90] or "employee"


def channel_names(employees) -> dict[int, str]:
    """Map employee (id, name) pairs to unique channel names. The first use of a
    name is clean (``al-mason``); each later duplicate gets the employee's last-4
    id (``al-mason-3096``)."""
    used: set[str] = set()
    out: dict[int, str] = {}
    for eid, ename in employees:
        base = _slug(ename)
        name = base if base not in used else f"{base}-{str(eid)[-4:]}"
        while name in used:  # extremely rare secondary clash
            name = f"{name}-x"
        used.add(name)
        out[eid] = name
    return out


# ---- pull prod -> test -----------------------------------------------------

async def _prepare_sanitized_copy(src: str) -> tuple[str, list[int], int, int]:
    """Copy the prod db to a temp file, migrate it, capture the active-employee
    ids (clock set + not archived) BEFORE sanitizing, sanitize, and return
    (tmp_path, active_ids, employee_count, punch_count)."""
    tmp = os.path.join(tempfile.gettempdir(), f"testpull_{os.getpid()}.db")
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    shutil.copy2(src, tmp)
    for suffix in ("-wal", "-shm"):  # copy sidecars so no committed data is missed
        if os.path.exists(src + suffix):
            shutil.copy2(src + suffix, tmp + suffix)

    sdb = await Database(tmp).setup()  # migrate the copy up to the current schema
    emps = (await sdb.fetchone("SELECT count(*) c FROM employee"))["c"]
    punches = (await sdb.fetchone("SELECT count(*) c FROM punch_clock"))["c"]
    active = [r["id"] for r in await sdb.fetchall(
        "SELECT id FROM employee WHERE clockChannelId IS NOT NULL "
        "AND clockMessageId IS NOT NULL AND (archived IS NULL OR archived = 0)")]
    for stmt in _SANITIZE_SQL:
        await sdb.execute(stmt)
    await sdb.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # fold WAL back so a plain move is complete
    await sdb.close()
    return tmp, active, emps, punches


async def _swap_in_test_db(tt, tmp: str) -> None:
    """Close the test db, replace its file (+ sidecars) with ``tmp``, reopen."""
    base = os.getcwd()
    target = os.path.join(db_dir(base), db_filename(TARGET_VERSION, "timetracker.test"))
    await tt.close_db()
    os.makedirs(db_dir(base), exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(target + suffix):
            os.remove(target + suffix)
    shutil.move(tmp, target)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            shutil.move(tmp + suffix, target + suffix)
    await tt._ensure_db()


async def import_source_into_test(tt, src: str) -> tuple[list[int], int, int]:
    """Sanitize any db file at ``src`` and swap it in as the test db. Returns
    (active_employee_ids, employee_count, punch_count). Shared by the prod pull
    and /testimport (uploaded snapshot)."""
    if not src or not os.path.exists(src):
        raise FileNotFoundError("No source database file found to import.")
    tmp, active, emps, punches = await _prepare_sanitized_copy(src)
    await _swap_in_test_db(tt, tmp)
    log.warning("[Setup] Imported into the test db: %d employees, %d punches (%d active).",
                emps, punches, len(active))
    return active, emps, punches


async def pull_prod_into_test(tt) -> tuple[list[int], int, int]:
    """Copy + sanitize the prod db and swap it in as the test db. Raises if no
    prod db is present."""
    src = prod_db_file(os.getcwd())
    if not src or not os.path.exists(src):
        raise FileNotFoundError("No production database file found to copy from.")
    return await import_source_into_test(tt, src)


# ---- build the category + per-employee clocks ------------------------------

async def _resolve_category(guild):
    cid = os.getenv(_CATEGORY_KEY)
    category = guild.get_channel(int(cid)) if cid and cid.isdigit() else None
    if category is None:
        category = await guild.create_category(_CATEGORY_NAME)
        os.environ[_CATEGORY_KEY] = str(category.id)
        try:
            config.set_env(_CATEGORY_KEY, str(category.id))
        except Exception as e:  # noqa: BLE001 - persistence is best-effort
            log.warning("[Setup] Couldn't persist %s (%s); category id kept for this session.", _CATEGORY_KEY, e)
        log.warning("[Setup] Created the '%s' category (%s).", _CATEGORY_NAME, category.id)
    return category


async def build_employee_clocks(tt, guild, employee_ids, skip_existing: bool = True) -> tuple[int, list[str]]:
    """Create/reuse the ``timecards`` category and build one clock channel per
    employee in ``employee_ids``. When ``skip_existing`` is set, employees who
    already have a clock pointer are left alone (so it's safe to re-run). Returns
    (made, skipped_names)."""
    db = await tt._ensure_db()
    if not employee_ids:
        return 0, []
    category = await _resolve_category(guild)
    placeholders = ",".join("?" for _ in employee_ids)
    rows = await db.fetchall(
        f"SELECT id, name, clockChannelId, clockMessageId FROM employee "
        f"WHERE id IN ({placeholders}) ORDER BY name",
        tuple(employee_ids))
    todo = [r for r in rows
            if not (skip_existing and r["clockChannelId"] and r["clockMessageId"])]
    names = channel_names([(r["id"], r["name"]) for r in todo])
    made, skipped = 0, []
    for r in todo:
        try:
            ch = await guild.create_text_channel(names[r["id"]], category=category)
            await tt.make_clock(r["id"], ch)
            made += 1
        except Exception as e:  # noqa: BLE001
            log.warning("[Setup] Could not build clock for %s: %s", r["name"], e)
            skipped.append(r["name"])
    return made, skipped


# ---- orchestration ---------------------------------------------------------

async def apply_decision(bot, pull: bool, guild) -> None:
    """Execute the bootstrap decision: pull prod (+ build clocks) or keep scratch."""
    tt = bot.get_cog("TimeTracking")
    if tt is None:
        log.warning("[Setup] Timetracking cog not loaded; skipping test bootstrap.")
        return
    if not pull:
        log.warning("[Setup] Test bootstrap: keeping the empty baseline db (scratch). "
                    "Load prod data later with /testimport.")
        return
    try:
        active, emps, punches = await pull_prod_into_test(tt)
    except Exception:
        log.exception("[Setup] Pull-from-prod failed; staying on the empty test db.")
        return
    made, skipped = 0, []
    if guild is not None:
        try:
            made, skipped = await build_employee_clocks(tt, guild, active)
        except Exception:
            log.exception("[Setup] Building employee clocks failed.")
    log.warning("[Setup] Test bootstrap done: %d employees / %d punches pulled, %d clock(s) built%s.",
                emps, punches, made, f"; skipped {', '.join(skipped)}" if skipped else "")


# ---- Discord prompt view ---------------------------------------------------

class TestBootstrapView(discord.ui.View):
    """Yes/No prompt posted to the timecard-admin channel. Resolves a shared
    Future that the console prompt also races (whichever answers first wins)."""

    def __init__(self, decision: asyncio.Future):
        super().__init__(timeout=300)
        self.decision = decision

    async def _choose(self, interaction: discord.Interaction, pull: bool):
        if not is_timecard_admin_member(interaction.user):
            await interaction.response.send_message("This isn't for you.", ephemeral=True)
            return
        if not self.decision.done():
            self.decision.set_result(pull)
        self.disable_all_items()
        await interaction.response.edit_message(
            content=("Test bootstrap: **pulling from production…**" if pull
                     else "Test bootstrap: **starting from scratch.**"),
            view=self)
        self.stop()

    @discord.ui.button(label="Pull from prod", style=discord.ButtonStyle.green)
    async def pull(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._choose(interaction, True)

    @discord.ui.button(label="Start from scratch", style=discord.ButtonStyle.secondary)
    async def scratch(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self._choose(interaction, False)
