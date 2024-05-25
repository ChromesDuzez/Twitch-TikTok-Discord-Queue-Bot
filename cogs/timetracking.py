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
    @commands.has_permissions(administrator=True)
    async def addemployee(self, 
                   ctx: discord.ApplicationContext,
                   name: discord.Option(str, description="Full Name of Employee"),   # type: ignore
                   phonenumber: discord.Option(str, description="Phone Number of Employee"),   # type: ignore
                   addressline1: discord.Option(str, description="Address Line 1 of Employee"),   # type: ignore
                   city: discord.Option(str, description="Address City of Employee"),   # type: ignore
                   state: discord.Option(str, description="Address State of Employee"),   # type: ignore
                   zip: discord.Option(str, description="Address Zip of Employee"),   # type: ignore
                   addressline2: discord.Option(str, default="", description="Address Line 2 of Employee"),   # type: ignore
                   user: discord.Option(str, default=None, description="Add a different user to the employee table"),   # type: ignore
                   ):
        conn = sqlite3.connect(self.db)
        cursor = conn.cursor()
        id = ctx.author.id
        valid = True
        if user is not None:
            try:
                newid = int(user[2:-1:])
                id = newid
            except:
                valid = False
                await ctx.respond("User Override Attribute was improperly formatted.")
                print(ctx.author + "User Override Attribute was improperly formatted.")
        
        cursor.execute(f"SELECT id FROM employee WHERE id = {id}")
        employees = cursor.fetchall()
        if len(employees) > 0:
            valid = False
            await ctx.respond(f"Cannot add new employee {name} (aka <@{id}>) because they already exist in the database.")
            print(f"Cannot add new employee {name} (aka <@{id}>) because they already exist in the database.")
        if valid:
            value = [(id, name, phonenumber, addressline1, addressline2, city, state, zip)]
            try:
                cursor.executemany('INSERT INTO employee VALUES (?, ?, ?, ?, ?, ?, ?, ?)', value)
                conn.commit()
                conn.close()
                await ctx.respond(f"Added new employee {value[0][1]} (aka <@{value[0][0]}>)")
                print(f"Added new employee {value[0][1]} (aka <@{value[0][0]}>)")
            except:
                await ctx.respond(f"Error adding new employee {value[0][1]} (aka <@{value[0][0]}>)")
                print(f"Error adding new employee {value[0][1]} (aka <@{value[0][0]}>)")
    

    # Error handling for missing permissions
    @addemployee.error
    async def addemployee_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.respond(f"{ctx.author}, you do not have the necessary permissions to use this command.")
            print(f"{ctx.author}, you do not have the necessary permissions to use this command.")
    

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
                    id             UNSIGNED BIG INT    PRIMARY KEY,
                    name           TEXT                NOT NULL,
                    phoneNumber    TEXT                NOT NULL,
                    addressLine1   TEXT                NOT NULL,
                    addressLine2   TEXT                NOT NULL DEFAULT '',
                    addressCity    TEXT                NOT NULL,
                    addressState   TEXT                NOT NULL,
                    addressZip     TEXT                NOT NULL
                )
            ''')
            #sets up punch clock table
            cursor.execute('''
                CREATE TABLE punch_clock (
                    id           UNSIGNED BIG INT    PRIMARY KEY,
                    employeeID   UNSIGNED BIG INT    NOT NULL,
                    punchInTime  DATETIME            NULL DEFAULT NULL,
                    punchOutTime DATETIME            NULL DEFAULT NULL,
                    FOREIGN KEY (employeeID) REFERENCES employee(id)
                )
            ''')
            #sets up customer table
            cursor.execute('''
                CREATE TABLE customer (
                    id             UNSIGNED BIG INT    PRIMARY KEY,
                    name           TEXT                NOT NULL,
                    phoneNumber    TEXT                NULL DEFAULT NULL,
                    addressLine1   TEXT                NULL DEFAULT NULL,
                    addressLine2   TEXT                NULL DEFAULT NULL,
                    addressCity    TEXT                NULL DEFAULT NULL,
                    addressState   TEXT                NULL DEFAULT NULL,
                    addressZip     TEXT                NULL DEFAULT NULL
                )
            ''')
            #sets up work time table
            cursor.execute('''
                CREATE TABLE users (
                    id          UNSIGNED BIG INT                                      PRIMARY KEY,
                    punchID     UNSIGNED BIG INT                                      NOT NULL,
                    customerID  UNSIGNED BIG INT                                      NOT NULL,
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