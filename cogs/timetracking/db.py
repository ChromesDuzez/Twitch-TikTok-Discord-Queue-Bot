"""Async SQLite data layer for the timetracking cog.

SQLite is the authoritative store. Every query here is parameterized (no
f-string SQL), inserts rely on SQLite rowid autoincrement instead of the old
race-prone ``MAX(id)+1`` pattern, and schema changes are applied through the
idempotent migration runner in :meth:`Database.setup`.

The whole module is async (``aiosqlite``) so nothing blocks the Discord event
loop the way the previous synchronous ``sqlite3`` calls did.
"""

from __future__ import annotations

import glob
import os
import shutil
from datetime import datetime

import aiosqlite

from botlog import log

# Human-facing release version (bumped on major rewrites or schema epochs).
RELEASE_VERSION = "2.0"

# Internal schema/migration counter, kept in lock-step with the .env version.
# This whole V2.0 refactor is ONE version: the pre-refactor database is
# version 1, the refactored schema is version 2. Drives upgrades and stamps the
# live db filename (timetracker.v{N}.db). Bump (both here and ENV in config.py)
# on a future schema change.
TARGET_VERSION = 2

# Canonical worktime categories the employee can start. NOTE: "Shop" is NOT one
# of these -- shop time is a calculated remainder in reports, never a punch.
PUNCH_TYPES = ("Construction", "Service", "Office")


class Database:
    """A thin async wrapper around a single shared aiosqlite connection."""

    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    # ---- lifecycle ---------------------------------------------------------

    async def setup(self, company_name: str | None = None, debug: bool = False):
        """Open the connection, create/migrate the schema, return self."""
        # An absent OR zero-byte file is "fresh" — a 0-byte file has no schema, so
        # migrating it would ALTER tables that don't exist (and hang the driver).
        fresh = (not os.path.exists(self.path)) or os.path.getsize(self.path) == 0
        # Autocommit mode: we manage transactions explicitly where needed (the
        # id-rebuild migration) and avoid Python's implicit-BEGIN pitfalls.
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

        if fresh:
            log.info("[DB] Timecard database not found, creating a new one...")
            await self._create_fresh(company_name, debug)
        await self._run_migrations()
        return self

    async def close(self):
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- low-level helpers (all parameterized) -----------------------------

    async def execute(self, sql: str, params: tuple = ()):
        """Run a write statement, commit, and return the last inserted rowid."""
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        lastrowid = cur.lastrowid
        await cur.close()
        return lastrowid

    async def executemany(self, sql: str, seq_of_params):
        cur = await self._conn.executemany(sql, seq_of_params)
        await self._conn.commit()
        await cur.close()

    async def fetchone(self, sql: str, params: tuple = ()):
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: tuple = ()):
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    # ---- schema creation ---------------------------------------------------

    async def _create_fresh(self, company_name: str | None, debug: bool):
        c = self._conn
        await c.executescript(
            """
            CREATE TABLE employee_type (
                id             INTEGER            PRIMARY KEY AUTOINCREMENT,
                name           TEXT               NOT NULL,
                rate           DECIMAL(10,5)      NOT NULL,
                construction   BOOLEAN            NOT NULL DEFAULT 1,
                service        BOOLEAN            NOT NULL DEFAULT 1,
                office         BOOLEAN            NOT NULL DEFAULT 0
            );
            CREATE TABLE employee (
                id             UNSIGNED BIG INT   PRIMARY KEY,
                name           TEXT               NOT NULL,
                phoneNumber    TEXT               NOT NULL,
                addressLine1   TEXT               NOT NULL,
                addressLine2   TEXT               NOT NULL DEFAULT '',
                addressCity    TEXT               NOT NULL,
                addressState   TEXT               NOT NULL,
                addressZip     TEXT               NOT NULL,
                payrate        DECIMAL(10,2)      NOT NULL DEFAULT 16.00,
                employeeTypeID INTEGER            NOT NULL DEFAULT 2,
                lunchSkipable  BOOLEAN            NOT NULL DEFAULT 0,
                clockChannelId UNSIGNED BIG INT   NULL DEFAULT NULL,
                clockMessageId UNSIGNED BIG INT   NULL DEFAULT NULL,
                odooId         UNSIGNED BIG INT   NULL DEFAULT NULL,
                archived       BOOLEAN            NOT NULL DEFAULT 0,
                FOREIGN KEY (employeeTypeID) REFERENCES employee_type(id)
            );
            CREATE TABLE punch_clock (
                id               INTEGER          PRIMARY KEY,
                employeeID       UNSIGNED BIG INT NOT NULL,
                punchInTime      DATETIME         NULL DEFAULT NULL,
                punchInApproval  BOOLEAN          NOT NULL DEFAULT 1,
                punchOutTime     DATETIME         NULL DEFAULT NULL,
                punchOutApproval BOOLEAN          NOT NULL DEFAULT 1,
                ignoreLunchBreak BOOLEAN          NOT NULL DEFAULT 0,
                checkChannelId   UNSIGNED BIG INT NULL DEFAULT NULL,
                checkMessageId   UNSIGNED BIG INT NULL DEFAULT NULL,
                odooId           UNSIGNED BIG INT NULL DEFAULT NULL,
                legacy           BOOLEAN          NOT NULL DEFAULT 0,
                FOREIGN KEY (employeeID) REFERENCES employee(id)
            );
            CREATE TABLE customer (
                id          INTEGER          PRIMARY KEY AUTOINCREMENT,
                name        TEXT             NOT NULL,
                odooId      UNSIGNED BIG INT NULL DEFAULT NULL,
                archived    BOOLEAN          NOT NULL DEFAULT 0
            );
            CREATE TABLE work_time (
                id            INTEGER          PRIMARY KEY,
                punchID       UNSIGNED BIG INT NOT NULL,
                customerID    INTEGER          NOT NULL DEFAULT 0,
                punchType     TEXT CHECK( punchType IN ('Construction','Service','Office') ) NOT NULL,
                timeSpent     INTEGER CHECK( timeSpent >= 0 AND timeSpent <= 1440 AND timeSpent % 15 = 0 ) NOT NULL DEFAULT 0,
                timeStarted   DATETIME         NOT NULL,
                odooId        UNSIGNED BIG INT NULL DEFAULT NULL,
                odooTaskId    UNSIGNED BIG INT NULL DEFAULT NULL,
                odooProjectId UNSIGNED BIG INT NULL DEFAULT NULL,
                FOREIGN KEY (punchID) REFERENCES punch_clock(id),
                FOREIGN KEY (customerID) REFERENCES customer(id)
            );
            CREATE TABLE employee_group (
                id          INTEGER          PRIMARY KEY AUTOINCREMENT,
                name        TEXT             NOT NULL
            );
            CREATE TABLE group_member (
                employeeID     UNSIGNED BIG INT NOT NULL,
                groupID        INTEGER          NOT NULL,
                FOREIGN KEY (employeeID) REFERENCES employee(id),
                FOREIGN KEY (groupID) REFERENCES employee_group(id)
            );
            CREATE TABLE odoo_outbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT    NOT NULL,
                entity_id   INTEGER NOT NULL,
                op          TEXT    NOT NULL,
                payload     TEXT    NOT NULL DEFAULT '{}',
                status      TEXT    NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT    NULL,
                created_at  DATETIME NOT NULL
            );
            CREATE TABLE odoo_inbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model       TEXT    NOT NULL,
                odoo_id     INTEGER NOT NULL,
                action      TEXT    NULL,
                write_uid   INTEGER NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT    NULL,
                created_at  DATETIME NOT NULL
            );
            CREATE TABLE pending_action (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                action      TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                odoo_id     INTEGER NULL,
                local_kind  TEXT    NULL,
                local_id    INTEGER NULL,
                channel_id  UNSIGNED BIG INT NULL,
                message_id  UNSIGNED BIG INT NULL,
                created_at  DATETIME NOT NULL
            );
            """
        )
        await c.executemany(
            "INSERT INTO employee_type (name, rate, construction, service, office) VALUES (?, ?, ?, ?, ?)",
            [
                ("Clerical", 1.5, False, False, True),
                ("Construction", 1.7, True, True, False),
                ("Salaried", 0.0, True, True, True),
            ],
        )
        await c.execute("INSERT INTO customer (id, name) VALUES (?, ?)", (0, company_name or "Company"))
        if debug:
            await c.executemany(
                "INSERT INTO customer (name) VALUES (?)",
                [("Bond, James",), ("Holmes, Sherlock",)],
            )
        await c.executemany(
            "INSERT INTO employee_group (id, name) VALUES (?, ?)",
            [(0, "Active Employees"), (1, company_name or "Company")],
        )
        await c.commit()

    # ---- migrations --------------------------------------------------------

    async def _run_migrations(self):
        c = self._conn
        await c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        await c.commit()
        row = await self.fetchone("SELECT version FROM schema_version LIMIT 1")
        if row is None:
            # A pre-existing (pre-refactor) database with no version marker is
            # version 1; a freshly created database is already at TARGET_VERSION.
            current = TARGET_VERSION if await self._is_fresh_target() else 1
            await c.execute("INSERT INTO schema_version (version) VALUES (?)", (current,))
            await c.commit()
        else:
            current = row["version"]

        # One collapsed upgrade brings a pre-refactor db up to the full schema.
        # (Databases stamped with a higher dev-era number already have the full
        # schema, so they just get re-stamped to TARGET below.)
        if current < 2:
            await self._migrate_to_v2()

        await c.execute("UPDATE schema_version SET version = ?", (TARGET_VERSION,))
        await c.commit()
        # Self-heal: add any expected column that's missing. Handles a database that
        # was stamped at the current version BEFORE a column was later introduced
        # (e.g. a dev/test db created before `legacy` existed) without a recreate.
        await self._ensure_columns()
        log.info(f"[DB] Schema at version {TARGET_VERSION}.")

    # Columns that must exist on the current schema. Kept idempotent so a db
    # stamped before a column was added self-heals on the next startup.
    _EXPECTED_COLUMNS = (
        ("employee", "odooId", "UNSIGNED BIG INT NULL DEFAULT NULL"),
        ("employee", "archived", "BOOLEAN NOT NULL DEFAULT 0"),
        ("punch_clock", "odooId", "UNSIGNED BIG INT NULL DEFAULT NULL"),
        ("punch_clock", "legacy", "BOOLEAN NOT NULL DEFAULT 0"),
        ("customer", "odooId", "UNSIGNED BIG INT NULL DEFAULT NULL"),
        ("customer", "archived", "BOOLEAN NOT NULL DEFAULT 0"),
        ("work_time", "odooId", "UNSIGNED BIG INT NULL DEFAULT NULL"),
        ("work_time", "odooTaskId", "UNSIGNED BIG INT NULL DEFAULT NULL"),
        ("work_time", "odooProjectId", "UNSIGNED BIG INT NULL DEFAULT NULL"),
    )

    async def _ensure_columns(self):
        """Idempotently add any missing expected column. New columns default to
        their neutral value (e.g. legacy=0) -- data already at this version is NOT
        historical, so it is correctly left non-legacy and still syncs."""
        c = self._conn
        added = False
        for table, col, decl in self._EXPECTED_COLUMNS:
            if not await self._column_exists(table, col):
                await c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                log.warning(f"[DB] Added missing column {table}.{col} (self-heal).")
                added = True
        if added:
            # Requeue any sync rows that only failed because of the now-added column.
            for tbl in ("odoo_outbox", "odoo_inbox"):
                await c.execute(
                    f"UPDATE {tbl} SET status = 'pending', attempts = 0, last_error = NULL "
                    f"WHERE status = 'failed' AND last_error LIKE '%no such column%'"
                )
        await c.commit()

    async def _is_fresh_target(self) -> bool:
        """True when punch_clock already uses an INTEGER rowid PK (fresh create)."""
        for col in await self.fetchall("PRAGMA table_info(punch_clock)"):
            if col["name"] == "id":
                return col["type"].upper() == "INTEGER"
        return False

    async def _column_exists(self, table: str, column: str) -> bool:
        for col in await self.fetchall(f"PRAGMA table_info({table})"):
            if col["name"] == column:
                return True
        return False

    async def _migrate_to_v2(self):
        """Pre-refactor (v1) -> v2: the full V2.0 schema, in one idempotent step.

        Adds Odoo id columns, the outbox/inbox queues, worktime task/project
        links, customer archiving, the pending-action queue, and rebuilds the
        punch_clock / work_time ids to atomic INTEGER rowids (preserving ids).
        Every step is guarded so it is safe to re-run.
        """
        log.info("[DB] Upgrading pre-refactor database to v2...")
        c = self._conn

        # 1. Odoo id + link columns (older builds lacked them).
        for table in ("employee", "punch_clock", "customer", "work_time"):
            if not await self._column_exists(table, "odooId"):
                await c.execute(f"ALTER TABLE {table} ADD COLUMN odooId UNSIGNED BIG INT NULL DEFAULT NULL")
        for column in ("odooTaskId", "odooProjectId"):
            if not await self._column_exists("work_time", column):
                await c.execute(f"ALTER TABLE work_time ADD COLUMN {column} UNSIGNED BIG INT NULL DEFAULT NULL")
        if not await self._column_exists("customer", "archived"):
            await c.execute("ALTER TABLE customer ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0")
        if not await self._column_exists("employee", "archived"):
            await c.execute("ALTER TABLE employee ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0")
        # Everything present at the v1->v2 upgrade is historical: flag it 'legacy' so
        # it stays reportable but is NEVER synced to Odoo (and is ignored as an open
        # punch, so employees start clocked-out and stale v1 punches can't leak).
        if not await self._column_exists("punch_clock", "legacy"):
            await c.execute("ALTER TABLE punch_clock ADD COLUMN legacy BOOLEAN NOT NULL DEFAULT 0")
            await c.execute("UPDATE punch_clock SET legacy = 1")
        await c.commit()

        # 2. Sync queues + admin-approval queue.
        await c.executescript(
            """
            CREATE TABLE IF NOT EXISTS odoo_outbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type TEXT    NOT NULL,
                entity_id   INTEGER NOT NULL,
                op          TEXT    NOT NULL,
                payload     TEXT    NOT NULL DEFAULT '{}',
                status      TEXT    NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT    NULL,
                created_at  DATETIME NOT NULL
            );
            CREATE TABLE IF NOT EXISTS odoo_inbox (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                model       TEXT    NOT NULL,
                odoo_id     INTEGER NOT NULL,
                action      TEXT    NULL,
                write_uid   INTEGER NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                attempts    INTEGER NOT NULL DEFAULT 0,
                last_error  TEXT    NULL,
                created_at  DATETIME NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_action (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                action      TEXT    NOT NULL,
                model       TEXT    NOT NULL,
                odoo_id     INTEGER NULL,
                local_kind  TEXT    NULL,
                local_id    INTEGER NULL,
                channel_id  UNSIGNED BIG INT NULL,
                message_id  UNSIGNED BIG INT NULL,
                created_at  DATETIME NOT NULL
            );
            """
        )
        await c.commit()

        # 3. Rebuild punch_clock / work_time so `id` is an INTEGER rowid alias
        #    (atomic autoincrement). Existing ids are preserved verbatim; the
        #    columns added above come along. Skipped when already rebuilt.
        await self._rebuild_id_as_rowid(
            "punch_clock",
            """
            CREATE TABLE punch_clock_new (
                id               INTEGER          PRIMARY KEY,
                employeeID       UNSIGNED BIG INT NOT NULL,
                punchInTime      DATETIME         NULL DEFAULT NULL,
                punchInApproval  BOOLEAN          NOT NULL DEFAULT 1,
                punchOutTime     DATETIME         NULL DEFAULT NULL,
                punchOutApproval BOOLEAN          NOT NULL DEFAULT 1,
                ignoreLunchBreak BOOLEAN          NOT NULL DEFAULT 0,
                checkChannelId   UNSIGNED BIG INT NULL DEFAULT NULL,
                checkMessageId   UNSIGNED BIG INT NULL DEFAULT NULL,
                odooId           UNSIGNED BIG INT NULL DEFAULT NULL,
                legacy           BOOLEAN          NOT NULL DEFAULT 0,
                FOREIGN KEY (employeeID) REFERENCES employee(id)
            )
            """,
            "id, employeeID, punchInTime, punchInApproval, punchOutTime, "
            "punchOutApproval, ignoreLunchBreak, checkChannelId, checkMessageId, odooId, legacy",
        )
        await self._rebuild_id_as_rowid(
            "work_time",
            """
            CREATE TABLE work_time_new (
                id            INTEGER          PRIMARY KEY,
                punchID       UNSIGNED BIG INT NOT NULL,
                customerID    INTEGER          NOT NULL DEFAULT 0,
                punchType     TEXT CHECK( punchType IN ('Construction','Service','Office') ) NOT NULL,
                timeSpent     INTEGER CHECK( timeSpent >= 0 AND timeSpent <= 1440 AND timeSpent % 15 = 0 ) NOT NULL DEFAULT 0,
                timeStarted   DATETIME         NOT NULL,
                odooId        UNSIGNED BIG INT NULL DEFAULT NULL,
                odooTaskId    UNSIGNED BIG INT NULL DEFAULT NULL,
                odooProjectId UNSIGNED BIG INT NULL DEFAULT NULL,
                FOREIGN KEY (punchID) REFERENCES punch_clock(id),
                FOREIGN KEY (customerID) REFERENCES customer(id)
            )
            """,
            "id, punchID, customerID, punchType, timeSpent, timeStarted, odooId, odooTaskId, odooProjectId",
        )
        log.info("[DB] Upgrade to v2 complete.")

    async def _rebuild_id_as_rowid(self, table: str, create_new_sql: str, columns: str):
        """Rebuild `table` from a `<table>_new` definition, copying all columns.

        Skipped when the column is already an INTEGER rowid alias so the
        migration is safe to re-run.
        """
        already_ok = False
        for col in await self.fetchall(f"PRAGMA table_info({table})"):
            if col["name"] == "id" and col["type"].upper() == "INTEGER":
                already_ok = True
        if already_ok:
            return

        c = self._conn
        # PRAGMA foreign_keys cannot be toggled inside a transaction.
        await c.execute("PRAGMA foreign_keys=OFF")
        await c.execute("BEGIN")
        try:
            await c.execute(create_new_sql)
            await c.execute(
                f"INSERT INTO {table}_new ({columns}) SELECT {columns} FROM {table}"
            )
            await c.execute(f"DROP TABLE {table}")
            await c.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            await c.execute("COMMIT")
        except Exception:
            await c.execute("ROLLBACK")
            raise
        finally:
            await c.execute("PRAGMA foreign_keys=ON")
        await c.commit()


# All timetracking db files live under <run-dir>/database/ so it's obvious which
# file is live; superseded files + backups go in the archive subfolder. Paths are
# relative to a base_dir arg so tests can point them at a temp directory.
DB_SUBDIR = "database"
DB_ARCHIVE_SUBDIR = "archive"


def db_dir(base_dir: str) -> str:
    return os.path.join(base_dir, DB_SUBDIR)


def db_archive_dir(base_dir: str) -> str:
    return os.path.join(base_dir, DB_SUBDIR, DB_ARCHIVE_SUBDIR)


def db_filename(version: int = TARGET_VERSION, prefix: str = "timetracker") -> str:
    """The live db filename for a given schema version.

    ``prefix`` separates environments: prod uses ``timetracker`` and the test
    bot uses ``timetracker.test`` so a test run can never open the prod file.
    """
    return f"{prefix}.v{version}.db"


def move_db_file(src: str, dst: str):
    """Move a db file and any of its WAL sidecars together (keeps them consistent)."""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    for suffix in ("", "-wal", "-shm", "-journal"):
        if os.path.exists(src + suffix):
            shutil.move(src + suffix, dst + suffix)


def backup_database(path: str, archive_dir: str) -> str | None:
    """Timestamped copy of the db into the archive folder before a migration."""
    if not os.path.exists(path):
        return None
    os.makedirs(archive_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(archive_dir, f"{os.path.basename(path)}.backup_{stamp}")
    shutil.copy2(path, dest)
    log.info(f"[DB] Backed up database to {dest}")
    return dest


def resolve_db_path(base_dir: str, prefix: str = "timetracker") -> tuple[str, str | None]:
    """Work out which db file to use and whether one needs upgrading/relocating.

    The live file lives at ``<base_dir>/database/{prefix}.v{N}.db``. Returns
    ``(target_path, source_or_None)``:

    * target already present               -> (target, None)
    * an older/legacy file, or a same-version file still in the run-dir root
      -> (target, that_path)   [caller backs it up if it's a real upgrade, then
                                moves it to target]
    * nothing yet                          -> (target, None)  [fresh create]

    Sources are searched in the database folder first, then the legacy run-dir
    root, so files from older installs are pulled in automatically.
    """
    os.makedirs(db_archive_dir(base_dir), exist_ok=True)  # creates database/ + archive/
    ddir = db_dir(base_dir)
    target = os.path.join(ddir, db_filename(TARGET_VERSION, prefix))
    if os.path.exists(target):
        return target, None
    best_ver, best_path = None, None
    for d in (ddir, base_dir):  # database/ first, then legacy root
        for path in glob.glob(os.path.join(d, f"{prefix}.v*.db")):
            name = os.path.basename(path)
            try:
                ver = int(name[len(prefix) + 2:-3])  # strip "{prefix}.v" and ".db"
            except ValueError:
                continue
            if best_ver is None or ver > best_ver:
                best_ver, best_path = ver, path
    if best_path:
        return target, best_path
    for d in (ddir, base_dir):
        legacy = os.path.join(d, f"{prefix}.db")  # pre-versioning name
        if os.path.exists(legacy):
            return target, legacy
    return target, None


def archive_stale_dbs(base_dir: str, prefix: str, keep_path: str) -> list[str]:
    """Move superseded db files + backups for ``prefix`` into database/archive/,
    leaving only the live file (``keep_path``) and its sidecars. Prefix-scoped so
    it never touches the other bot's (test vs prod) files. Returns moved paths."""
    adir = db_archive_dir(base_dir)
    os.makedirs(adir, exist_ok=True)
    keep = os.path.abspath(keep_path)
    live = {keep, keep + "-wal", keep + "-shm", keep + "-journal"}
    patterns = (
        f"{prefix}.db", f"{prefix}.v*.db",
        f"{prefix}.db.backup_*", f"{prefix}.v*.db.backup_*",
        f"{prefix}.db-wal", f"{prefix}.db-shm", f"{prefix}.db-journal",
        f"{prefix}.v*.db-wal", f"{prefix}.v*.db-shm", f"{prefix}.v*.db-journal",
    )
    moved = []
    for d in (base_dir, db_dir(base_dir)):  # root + database/ (not the archive itself)
        for pat in patterns:
            for path in glob.glob(os.path.join(d, pat)):
                ap = os.path.abspath(path)
                if ap in live:
                    continue
                dest = os.path.join(adir, os.path.basename(path))
                if os.path.exists(dest):  # avoid clobber
                    dest += f".{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                shutil.move(path, dest)
                moved.append(dest)
    if moved:
        log.info(f"[DB] Archived {len(moved)} old db file(s) to {adir}/.")
    return moved
