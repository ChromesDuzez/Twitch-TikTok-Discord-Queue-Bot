/* This sql script stores how the database should look upon it's first creation... 
I do have an sql script to populate it with data but it has real company data in it so I will not be sharing it for privacy/security reasons. 
Might make one with fake data at some point. */

		/* DEFAULT SCHEMA */
/* employee type table wich helps control functionality of the time clock buttons and how the system knows how to report them properly */
CREATE TABLE employee_type (
	id             INTEGER            PRIMARY KEY AUTOINCREMENT,
	name           TEXT               NOT NULL,
	rate           DECIMAL(10,5)      NOT NULL,
	construction   BOOLEAN            NOT NULL DEFAULT "TRUE",
	service        BOOLEAN            NOT NULL DEFAULT "TRUE", 
	office         BOOLEAN            NOT NULL DEFAULT "FALSE"
);

/* Employee data stored here */
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
	lunchSkipable  BOOLEAN            NOT NULL DEFAULT "FALSE",
    clockChannelId UNSIGNED BIG INT   NULL DEFAULT NULL,
    clockMessageId UNSIGNED BIG INT   NULL DEFAULT NULL,
    FOREIGN KEY (employeeTypeID) REFERENCES employee_type(id)
);

/* This is the main clock in and out table all punches go through here */
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

/* Customer table for where work_time punches get applied to... for reporting sake so you can see how much time was spent at each jobsite */
CREATE TABLE customer (
	id          INTEGER          PRIMARY KEY AUTOINCREMENT,
    name        TEXT             NOT NULL
);

/* this is a punch's subpunch into specific types of jobs ('Construction','Service', 'Office') and how much time was spent at each */
CREATE TABLE work_time (
	id          UNSIGNED BIG INT                                                             PRIMARY KEY,
    punchID     UNSIGNED BIG INT                                                             NOT NULL,
    customerID  INT                                                                          NOT NULL,
    punchType   TEXT CHECK( punchType IN ('Construction','Service', 'Office') )              NOT NULL,
    timeSpent   INTEGER CHECK( timeSpent >= 0 AND timeSpent <= 1440 AND timeSpent % 15 = 0)  NOT NULL DEFAULT 0,
    timeStarted DATETIME                                                                     NOT NULL,
    FOREIGN KEY (punchID) REFERENCES punch_clock(id),
    FOREIGN KEY (customerID) REFERENCES customer(id)
);

/* employee groups so that reports can be easily run on a section of the employees */
CREATE TABLE employee_group (
	id          INTEGER          PRIMARY KEY AUTOINCREMENT,
    name        TEXT             NOT NULL
);

/* employee_group and employee LINK table */
CREATE TABLE group_member (
	employeeID     UNSIGNED BIG INT    NOT NULL,
    groupID        INTEGER             NOT NULL,
    FOREIGN KEY (employeeID) REFERENCES employee(id),
    FOREIGN KEY (groupID) REFERENCES employee_group(id)
);

		/* DEFAULT SCHEMA DATA - [Replace "Fellowship of the Ping" with the company name of your choosing.]*/
/* this is the default customer data that must be here... office work_time all gets assigned to the company as the customer */
INSERT INTO customer (id, name)
VALUES 
	(0, "Fellowship of the Ping");

/* this is the default employee_group and it must have something here (not necessarily your company) but just something so that report method's autocomplete doesnt error out cause the table is empty */
INSERT INTO employee_group (id, name)
VALUES 
	(0, "Fellowship of the Ping");
	
/* these are the default employee types and what their rate multiplier is and what types of work_time punches they can do (1 = enabled) (0 = disabled)... DEFAULT = 2 */
INSERT INTO employee_type (id, name, rate, construction, service, office)
VALUES 
	(1, "Clerical", 1.5, 0, 0, 1),
	(2, "Construction", 1.7, 1, 1, 0),
	(3, "Salaried", 0, 1, 1, 1);