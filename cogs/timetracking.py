import discord
from discord.ext import commands
import sqlite3
import os


class TimeTracking(commands.Cog): # create a class for our cog that inherits from commands.Cog
    # this class is used to create a cog, which is a module that can be added to the bot

    def __init__(self, bot): # this is a special method that is called when the cog is loaded
        self.db = os.getcwd() + "\\timetracker.db"
        self.verifyDatabase(self.db)
        self.bot = bot

    @discord.slash_command(name="addemployee", description="Add a new Employee to from discord to the system.")
    async def addEmployee(self, 
                   ctx: discord.ApplicationContext, 
                   user: discord.Option(str, default="", description="Add a different user to the employee table"),   # type: ignore
                   ):
        await ctx.respond(f"Pong! Latency is {self.bot.latency}")
    

    ## Database setup if it doesn't already exist
    def verifyDatabase(self, db):
        if not os.path.exists(db):
            f = open(db, "w")
            f.close()
            conn = sqlite3.connect(db)
            cursor = conn.cursor()
            #sets up employee table
            cursor.execute('''
                CREATE TABLE employee (
                    id             INTEGER    PRIMARY KEY,
                    discordId      TEXT       NOT NULL,
                    name           TEXT       NOT NULL,
                    phoneNumber    TEXT       NOT NULL,
                    addressLine1   TEXT       NOT NULL,
                    addressLine2   TEXT       NOT NULL,
                    addressCity    TEXT       NOT NULL,
                    addressState   TEXT       NOT NULL,
                    addressZip     TEXT       NOT NULL
                )
            ''')
            #sets up punch clock table
            cursor.execute('''
                CREATE TABLE punch_clock (
                    id           INTEGER    PRIMARY KEY,
                    employeeID   INTEGER    NOT NULL,
                    punchInTime  DATETIME   NULL DEFAULT NULL,
                    punchOutTime DATETIME   NULL DEFAULT NULL,
                    FOREIGN KEY (employeeID) REFERENCES employee(id)
                )
            ''')
            #sets up customer table
            cursor.execute('''
                CREATE TABLE customer (
                    id             INTEGER    PRIMARY KEY,
                	name           TEXT       NOT NULL,
                	phoneNumber    TEXT       NULL DEFAULT NULL,
                    addressLine1   TEXT       NULL DEFAULT NULL,
                    addressLine2   TEXT       NULL DEFAULT NULL,
                    addressCity    TEXT       NULL DEFAULT NULL,
                    addressState   TEXT       NULL DEFAULT NULL,
                    addressZip     TEXT       NULL DEFAULT NULL
                )
            ''')
            #sets up work time table
            cursor.execute('''
                CREATE TABLE users (
                    id          INTEGER                                               PRIMARY KEY,
                    punchID     INTEGER                                               NOT NULL,
                    customerID  TEXT                                                  NOT NULL,
                    punchType   TEXT CHECK( punchType IN ('POOL','SERVICE') )         NOT NULL,
                    timeSpent   INTEGER CHECK( timeSpent > 0 AND timeSpent <= 1440)   NOT NULL,
                    FOREIGN KEY (punchID) REFERENCES punch_clock(id),
                    FOREIGN KEY (customerID) REFERENCES customer(id)
                )
            ''')
            conn.commit()
            conn.close()


def setup(bot): # this is called by Pycord to setup the cog
    bot.add_cog(TimeTracking(bot)) # add the cog to the bot