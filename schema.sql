CREATE TABLE employee_type (
	                id             INTEGER            PRIMARY KEY AUTOINCREMENT,
	                name           TEXT               NOT NULL,
	                rate           DECIMAL(10,5)      NOT NULL,
                    construction   BOOLEAN            NOT NULL DEFAULT "TRUE",
                    service        BOOLEAN            NOT NULL DEFAULT "TRUE", 
                    office         BOOLEAN            NOT NULL DEFAULT "FALSE"
                );
CREATE TABLE sqlite_sequence(name,seq);
CREATE TABLE employee (
                    id             UNSIGNED BIG INT    PRIMARY KEY,
                    name           TEXT                NOT NULL,
                    phoneNumber    TEXT                NOT NULL,
                    addressLine1   TEXT                NOT NULL,
                    addressLine2   TEXT                NOT NULL DEFAULT '',
                    addressCity    TEXT                NOT NULL,
                    addressState   TEXT                NOT NULL,
                    addressZip     TEXT                NOT NULL,
	                payrate        DECIMAL(10,2)       NOT NULL DEFAULT 16.00,
	                employeeTypeID INTEGER             NOT NULL DEFAULT 2,
	                lunchSkipable  BOOLEAN             NOT NULL DEFAULT "FALSE",
                    clockChannelId UNSIGNED BIG INT    NULL DEFAULT NULL,
                    clockMessageId UNSIGNED BIG INT    NULL DEFAULT NULL,
                    FOREIGN KEY (employeeTypeID) REFERENCES employee_type(id)
                );
CREATE TABLE punch_clock (
                    id               UNSIGNED BIG INT    PRIMARY KEY,
                    employeeID       UNSIGNED BIG INT    NOT NULL,
                    punchInTime      DATETIME            NULL DEFAULT NULL,
                    punchInApproval  BOOLEAN             NOT NULL DEFAULT "TRUE",
                    punchOutTime     DATETIME            NULL DEFAULT NULL,
                    punchOutApproval BOOLEAN             NOT NULL DEFAULT "TRUE",
                    ignoreLunchBreak BOOLEAN             NOT NULL DEFAULT "FALSE",
                    checkChannelId   UNSIGNED BIG INT    NULL DEFAULT NULL,
                    checkMessageId   UNSIGNED BIG INT    NULL DEFAULT NULL,
                    FOREIGN KEY (employeeID) REFERENCES employee(id)
                );
CREATE TABLE customer (
                    id          INTEGER          PRIMARY KEY AUTOINCREMENT,
                    name        TEXT             NOT NULL
                );
CREATE TABLE work_time (
                    id          UNSIGNED BIG INT                                                             PRIMARY KEY,
                    punchID     UNSIGNED BIG INT                                                             NOT NULL,
                    customerID  INT                                                                          NOT NULL DEFAULT 0,
                    punchType   TEXT CHECK( punchType IN ('Construction','Service', 'Office') )              NOT NULL,
                    timeSpent   INTEGER CHECK( timeSpent >= 0 AND timeSpent <= 1440 AND timeSpent % 15 = 0)  NOT NULL DEFAULT 0,
                    timeStarted DATETIME                                                                     NOT NULL,
                    FOREIGN KEY (punchID) REFERENCES punch_clock(id),
                    FOREIGN KEY (customerID) REFERENCES customer(id)
                );
CREATE TABLE employee_group (
                    id          INTEGER          PRIMARY KEY AUTOINCREMENT,
                    name        TEXT             NOT NULL
                );
CREATE TABLE group_member (
                    employeeID     UNSIGNED BIG INT    NOT NULL,
                    groupID        INTEGER             NOT NULL,
                    FOREIGN KEY (employeeID) REFERENCES employee(id),
                    FOREIGN KEY (groupID) REFERENCES employee_group(id)
                );
