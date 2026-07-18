"""Async SQLite data layer for the timetracking cog.

SQLite is the authoritative store. Every query here is parameterized (no
f-string SQL), inserts rely on SQLite rowid autoincrement instead of the old
race-prone ``MAX(id)+1`` pattern, and schema changes are applied through the
idempotent migration runner in :meth:`Database.setup`.

The whole module is async (``aiosqlite``) so nothing blocks the Discord event
loop the way the previous synchronous ``sqlite3`` calls did.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime

import aiosqlite

# Bump this whenever a new migration is added below.
TARGET_VERSION = 3

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
        fresh = not os.path.exists(self.path)
        # Autocommit mode: we manage transactions explicitly where needed (the
        # id-rebuild migration) and avoid Python's implicit-BEGIN pitfalls.
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.commit()

        if fresh:
            print("[DB] Timecard database not found, creating a new one...")
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
                FOREIGN KEY (employeeID) REFERENCES employee(id)
            );
            CREATE TABLE customer (
                id          INTEGER          PRIMARY KEY AUTOINCREMENT,
                name        TEXT             NOT NULL,
                odooId      UNSIGNED BIG INT NULL DEFAULT NULL
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
            # A pre-existing legacy database with no version marker starts at 1.
            # A freshly created database is already at TARGET_VERSION.
            current = TARGET_VERSION if await self._is_fresh_target() else 1
            await c.execute("INSERT INTO schema_version (version) VALUES (?)", (current,))
            await c.commit()
        else:
            current = row["version"]

        if current < 2:
            await self._migrate_to_v2()
            current = 2

        if current < 3:
            await self._migrate_to_v3()
            current = 3

        await c.execute("UPDATE schema_version SET version = ?", (current,))
        await c.commit()
        print(f"[DB] Schema at version {current}.")

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
        """Legacy -> v2: add odooId columns, add outbox, make id columns atomic."""
        print("[DB] Migrating schema to v2...")
        c = self._conn

        # 1. Ensure odooId columns exist (older schema.sql builds lacked them).
        for table in ("employee", "punch_clock", "customer", "work_time"):
            if not await self._column_exists(table, "odooId"):
                await c.execute(f"ALTER TABLE {table} ADD COLUMN odooId UNSIGNED BIG INT NULL DEFAULT NULL")
        await c.commit()

        # 2. Offline-safe Odoo sync queue.
        await c.execute(
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
            )
            """
        )
        await c.commit()

        # 3. Rebuild punch_clock / work_time so `id` is an INTEGER rowid alias
        #    (atomic autoincrement). Existing ids are preserved verbatim.
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
                FOREIGN KEY (employeeID) REFERENCES employee(id)
            )
            """,
            "id, employeeID, punchInTime, punchInApproval, punchOutTime, "
            "punchOutApproval, ignoreLunchBreak, checkChannelId, checkMessageId, odooId",
        )
        await self._rebuild_id_as_rowid(
            "work_time",
            """
            CREATE TABLE work_time_new (
                id          INTEGER          PRIMARY KEY,
                punchID     UNSIGNED BIG INT NOT NULL,
                customerID  INTEGER          NOT NULL DEFAULT 0,
                punchType   TEXT CHECK( punchType IN ('Construction','Service','Office') ) NOT NULL,
                timeSpent   INTEGER CHECK( timeSpent >= 0 AND timeSpent <= 1440 AND timeSpent % 15 = 0 ) NOT NULL DEFAULT 0,
                timeStarted DATETIME         NOT NULL,
                odooId      UNSIGNED BIG INT NULL DEFAULT NULL,
                FOREIGN KEY (punchID) REFERENCES punch_clock(id),
                FOREIGN KEY (customerID) REFERENCES customer(id)
            )
            """,
            "id, punchID, customerID, punchType, timeSpent, timeStarted, odooId",
        )
        print("[DB] Migration to v2 complete.")

    async def _migrate_to_v3(self):
        """v2 -> v3: link worktime to an Odoo task/project for timesheet sync."""
        print("[DB] Migrating schema to v3...")
        c = self._conn
        for column in ("odooTaskId", "odooProjectId"):
            if not await self._column_exists("work_time", column):
                await c.execute(
                    f"ALTER TABLE work_time ADD COLUMN {column} UNSIGNED BIG INT NULL DEFAULT NULL"
                )
        await c.commit()
        print("[DB] Migration to v3 complete.")

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


def backup_database(path: str) -> str | None:
    """Timestamped copy of the db before migrations. Returns the backup path."""
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = f"{path}.backup_{stamp}"
    shutil.copy2(path, dest)
    print(f"[DB] Backed up database to {dest}")
    return dest
