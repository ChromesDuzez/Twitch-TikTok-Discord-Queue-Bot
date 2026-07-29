-- Reference schema for the timetracking database (schema version 2).
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
    -- archived (terminated) employees keep their history but lose their clock and
    -- can't be given a new one; set when the linked hr.employee is archived in Odoo.
    archived       BOOLEAN            NOT NULL DEFAULT 0,
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
    -- legacy = migrated pre-v2 historical data: reportable but NEVER synced to Odoo,
    -- and ignored when finding the "current open punch" (so it can't leak on clock-out).
    -- The v1->v2 upgrade sets this to 1 for every migrated punch; new punches are 0.
    legacy           BOOLEAN          NOT NULL DEFAULT 0,
    FOREIGN KEY (employeeID) REFERENCES employee(id)
);

-- archived customers are hidden from the worktime search but kept for reports
-- (set when a customer is deleted/archived in Odoo but still referenced locally).
CREATE TABLE customer (
    id          INTEGER          PRIMARY KEY AUTOINCREMENT,
    name        TEXT             NOT NULL,
    odooId      UNSIGNED BIG INT NULL DEFAULT NULL,
    archived    BOOLEAN          NOT NULL DEFAULT 0
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

-- Inbound Odoo change queue (pull-based reconcile). The webhook enqueues a
-- pointer {model, odoo_id}; the InboxWorker pulls the record and reconciles it
-- idempotently into SQLite. De-duped by (model, odoo_id) while pending.
CREATE TABLE odoo_inbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    model       TEXT    NOT NULL,      -- 'res.partner' | 'hr.attendance' | 'account.analytic.line'
    odoo_id     INTEGER NOT NULL,      -- Odoo record id to pull
    action      TEXT    NULL,          -- 'create' | 'write' | 'unlink' (informational)
    write_uid   INTEGER NULL,          -- Odoo user who made the change (echo filter)
    status      TEXT    NOT NULL DEFAULT 'pending',  -- pending|done|failed
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT    NULL,
    created_at  DATETIME NOT NULL
);

-- Admin approvals awaiting a decision (e.g. an Odoo-side deletion the admin must
-- approve). Persisted so the Approve/Reject view survives a bot restart.
CREATE TABLE pending_action (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT    NOT NULL,      -- e.g. 'delete'
    model       TEXT    NOT NULL,      -- Odoo model the action concerns
    odoo_id     INTEGER NULL,          -- Odoo record id
    local_kind  TEXT    NULL,          -- 'punch' | 'worktime'
    local_id    INTEGER NULL,          -- local row id
    channel_id  UNSIGNED BIG INT NULL, -- admin approval message location
    message_id  UNSIGNED BIG INT NULL,
    created_at  DATETIME NOT NULL
);
