-- Reference schema for the timetracking database (schema version 3).
-- The bot creates/migrates this automatically at startup via cogs/timetracking/db.py;
-- this file is documentation only.

CREATE TABLE schema_version (
    version INTEGER NOT NULL
);

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

-- id is an INTEGER rowid alias so inserts auto-assign atomically
-- (the old UNSIGNED BIG INT + MAX(id)+1 pattern was race-prone).
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

-- odooTaskId / odooProjectId link a worktime punch to an Odoo work item:
--   Service      -> a Field Service task (odooTaskId + its odooProjectId)
--   Construction -> the customer's project (odooProjectId, no task)
--   Office       -> the dedicated Office project (odooProjectId, no task)
-- odooId holds the resulting account.analytic.line id after sync.
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

-- Offline-safe outbound Odoo sync queue. Rows are drained by the SyncWorker;
-- nothing is lost when Odoo is unreachable.
CREATE TABLE odoo_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT    NOT NULL,      -- 'punch' | 'worktime' | 'customer'
    entity_id   INTEGER NOT NULL,      -- local row id
    op          TEXT    NOT NULL,      -- 'in' | 'out' | 'create'
    payload     TEXT    NOT NULL DEFAULT '{}',
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|done|skipped|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT    NULL,
    created_at  DATETIME NOT NULL
);
