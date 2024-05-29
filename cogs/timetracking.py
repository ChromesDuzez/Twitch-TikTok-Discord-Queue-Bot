import discord
from discord.ext import commands
import sqlite3
import os

class Confirm(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__()
        self.user = user
        self.value = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.green)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        self.value = True
        self.stop()
        await interaction.response.send_message("You clicked Yes!", ephemeral=True)

    @discord.ui.button(label="No", style=discord.ButtonStyle.red)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        self.value = False
        self.stop()
        await interaction.response.send_message("You clicked No!", ephemeral=True)

class Clock(discord.ui.View):
    def __init__(self, user: discord.User, message: discord.message, bot: discord.bot):
        super().__init__()
        self.bot = bot
        self.user = user
        self.message = message
        self.value = None

    @discord.ui.button(label="Clock-In", style=discord.ButtonStyle.green)
    async def clockin(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        if self.value == True:
            await interaction.response.send_message("You are already clocked in!", ephemeral=True)
            return
        self.value = True
        embeds = self.message.embeds
        embeds[0].color = discord.Colour.brand_green()
        embeds[0].title = "You ARE currently clocked in."
        await self.message.edit(embeds=embeds)
        print(f"{self.user} just clocked in.")
        await interaction.response.send_message("You clocked in.", ephemeral=True)

    @discord.ui.button(label="Clock-Out", style=discord.ButtonStyle.red)
    async def clockout(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        if self.value == False:
            await interaction.response.send_message("You are already clocked out!", ephemeral=True)
            return
        self.value = False
        embeds = self.message.embeds
        embeds[0].color = discord.Colour.brand_red()
        embeds[0].title = "You are currently NOT clocked in."
        await self.message.edit(embeds=embeds)
        print(f"{self.user} just clocked out.")
        await interaction.response.send_message("You clocked out.", ephemeral=True)


class TimeTracking(commands.Cog): # create a class for our cog that inherits from commands.Cog
    # this class is used to create a cog, which is a module that can be added to the bot

    def __init__(self, bot): # this is a special method that is called when the cog is loaded
        self.cwd = os.getcwd()
        self.db = self.cwd + "\\timetracker.db"
        self.dbSetup(self.db)
        self.bot = bot

                    ## Employee Type Methods
        ### Less important methods ###
    #add type(name, rate, id=-1) -  admin command
    #edit type(id, name="", rate="") -  admin command
    #remove type(id) - admin command


                    ## Employee Methods
    # add employee(name, phonenumber, addressline1, city, state, zip, addressline2="",user="") - admin command   *updating needed
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
                newid = int(user.strip()[2:-1:])
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
                cursor.executemany('INSERT INTO employee (id, name, phoneNumber, addressLine1, addressLine2, addressCity, addressState, addressZip) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', value)
                conn.commit()
                conn.close()
                await ctx.respond(f"Added new employee {value[0][1]} (aka <@{value[0][0]}>)")
                print(f"Added new employee {value[0][1]} (aka <@{value[0][0]}>)")
            except:
                await ctx.respond(f"Error adding new employee {value[0][1]} (aka <@{value[0][0]}>)")
                print(f"Error adding new employee {value[0][1]} (aka <@{value[0][0]}>) by {ctx.author}")
    # Error handling for missing permissions
    @addemployee.error
    async def addemployee_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.respond(f"{ctx.author}, you do not have the necessary permissions to use this command.", ephemeral=True)
            print(f"{ctx.author}, you do not have the necessary permissions to use this command.")
    
    # create clock(user, channelid="") - admin command
    @discord.slash_command(name="createclock", description="Create a time clock embed for a user.")
    @commands.has_permissions(administrator=True)
    async def createclock(self, 
                          ctx: discord.ApplicationContext,
                          user: discord.Option(str, description="The discord user you wish to make a time clock for."),   # type: ignore
                          channel: discord.Option(str, default=None, description="Choose a different channel to create the time clock.")   # type: ignore
                          ):
        ## check that the values given were in the correct format
        try:
            _ = int(user[2:-1:])
            if channel is not None:
                _ = int(user[2:-1:])
        except:
            if channel is None:
                await ctx.respond(f"The parameters submitted were not in the correct format for {user}: {user[2:-1:]}", ephemeral=True)
            else:
                await ctx.respond(f"The parameters submitted were not in the correct format for {user}: {user[2:-1:]} or {channel[2:-1:]}", ephemeral=True)
            return
        # connect to db
        conn = sqlite3.connect(self.db)
        cursor = conn.cursor()
        ## use the data in the db to check that we aren't overwriting good data
        cursor.execute(f"SELECT clockChannelId, clockMessageId FROM employee WHERE id = {user[2:-1:]}")
        employees = cursor.fetchall()
        if len(employees) == 0:
            await ctx.respond(f"Cannot edit employee {user} because they don't exist in the database.", ephemeral=True)
            print(f"Cannot edit employee {user} because they don't exist in the database.")
            conn.commit()
            conn.close()
            return
        elif len(employees) > 1:
            await ctx.respond(f"There is a significant bug in the code that <@336231886198276096> needs to look into.", ephemeral=True)
            print("How did we get multiple employees back with the same id?!!!!!")
            conn.commit()
            conn.close()
            return
        elif employees[0][1] is not None:
            view = Confirm(user=ctx.user)
            await ctx.response.send_message("This emplyee already has a time clock, do you want to proceed?", view=view, ephemeral=True)
            await view.wait()
            if view.value:
                oldMessageID = employees[0][1]
                oldChannelID = employees[0][0]
                print(f"Override Confirmed by {view.user}... Trying to delete old time clock message.")
                try:
                    # Fetch the channel by ID
                    valid = True
                    delchannel = self.bot.get_channel(oldChannelID)
                    if delchannel is None:
                        print(f"Channel with ID {oldChannelID} not found.")
                        valid = False
                    # Fetch the message by ID
                    if valid:
                        message = await delchannel.fetch_message(oldMessageID)
                        # Delete the message
                        await message.delete()
                        print(f"Message with ID {oldMessageID} deleted from channel {oldChannelID}.")
                except discord.NotFound:
                    print("Message or channel not found.")
                except discord.Forbidden:
                    print("I do not have permission to delete this message.")
                except discord.HTTPException as e:
                    print(f"Failed to delete message: {e}")
            else:
                await ctx.followup.send("You chose not to proceed or did not respond in time.", ephemeral=True)
                return
        ## go to channel and send the message
        channelObj = ctx.channel
        if channel is not None:
            channelObj = self.bot.get_channel(int(channel[2:-1:]))
        userObj = ctx.guild.get_member(int(user[2:-1:]))
        embed = discord.Embed(
            title="You are currently NOT clocked in.",
            color=discord.Colour.brand_red(), # Pycord provides a class with default colors you can choose from
        )
        embed.add_field(name="Wondering how to clock in?", value="Click the green clock-in button and watch the field turn green. And to clock out, hit the red clock-out button. Simple as that!")
        embed.set_footer(text=f"User: {user}")
        embed.set_author(name=f"{userObj} Time Clock", icon_url="https://media.discordapp.net/attachments/1224574847213109330/1244848933675728978/clkfbambooblack600x600-bgf8f8f8.png?ex=66569b69&is=665549e9&hm=61d63652381160c0339fe9fb51cceb8d6971c1878e01b9cb0a181d43e5d97546&=&format=webp&quality=lossless")
        message = await channelObj.send(embed=embed)
        view = Clock(user=userObj, message=message, bot=self.bot)
        await message.edit(view=view)
        ## store the message id and channel id in the db
        cursor.execute(f"UPDATE employee SET clockChannelId = {message.channel.id}, clockMessageId = {message.id} WHERE id = {user[2:-1:]}")
        ## wrap it all up with a nice clean bow
        conn.commit()
        conn.close()
        await ctx.respond(f"Clock created successfully for {user}", ephemeral=True)
        print(f"{ctx.author} - Clock created successfully for {user}")

        
    # delete clock(user) - admin command


        ### Less important methods ###
    #edit employee(user, name="", phonenumber="", addressline1="", addressline2="", city="", state="",, zip="") - admin command
    #remove employee(user) - admin command


                ## Punch Clock Methods
    # display punches(user="")

    # punch in(user="")

    # punch out(user="")

    # update clock(user) - private method 

        ### Less important methods ###
    #edit punch(id) - admin command - trigger via button?


                ## Work Time Methods
    # display work time(user)

    #add work time(customer, punchtype, timespent, punchid=-1)

        ### Less important methods ###
    #edit work time(id, punchid=-1, customer="", punchtype="", timespent=0) - admin perms to edit someone else's - trigger via button?
    #remove work time(id) - admin command? - trigger via button?


    ## Database setup if it doesn't already exist
    def dbSetup(self, db):
        if not os.path.exists(db):
            f = open(db, "w")
            f.close()
            conn = sqlite3.connect(db)
            cursor = conn.cursor()
            #sets up employee type table
            cursor.execute('''
                CREATE TABLE employee_type (
	                id             INTEGER            PRIMARY KEY,
	                name           TEXT               NOT NULL,
	                rate           DECIMAL(10,5)      NOT NULL
                )
            ''')
            employeeTypesDefaultData = [
                ('Clerical', 1.5),
                ('Construction', 1.7)
            ]
            cursor.executemany('INSERT INTO employee_type (name, rate) VALUES (?, ?)', employeeTypesDefaultData)
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
                    addressZip     TEXT                NOT NULL,
	                payrate        DECIMAL(10,2)       NOT NULL DEFAULT 16.00,
	                employeeTypeID INTEGER             NOT NULL DEFAULT 2,
                    clockChannelId UNSIGNED BIG INT    NULL DEFAULT NULL,
                    clockMessageId UNSIGNED BIG INT    NULL DEFAULT NULL,
                    FOREIGN KEY (employeeTypeID) REFERENCES employee_type(id)
                )
            ''')
            #sets up punch clock table
            cursor.execute('''
                CREATE TABLE punch_clock (
                    id               UNSIGNED BIG INT    PRIMARY KEY,
                    employeeID       UNSIGNED BIG INT    NOT NULL,
                    punchInTime      DATETIME            NULL DEFAULT NULL,
                    punchInApproval  BOOLEAN             NOT NULL DEFAULT "TRUE",
                    punchOutTime     DATETIME            NULL DEFAULT NULL,
                    punchOutApproval BOOLEAN             NOT NULL DEFAULT "TRUE",
                    checkChannelId   UNSIGNED BIG INT    NULL DEFAULT NULL,
                    checkMessageId   UNSIGNED BIG INT    NULL DEFAULT NULL,
                    FOREIGN KEY (employeeID) REFERENCES employee(id)
                )
            ''')
            #sets up work time table
            cursor.execute('''
                CREATE TABLE work_time (
                    id          UNSIGNED BIG INT                                                             PRIMARY KEY,
                    punchID     UNSIGNED BIG INT                                                             NOT NULL,
                    customer    TEXT                                                                         NOT NULL,
                    punchType   TEXT CHECK( punchType IN ('POOL','SERVICE') )                                NOT NULL,
                    timeSpent   INTEGER CHECK( timeSpent > 0 AND timeSpent <= 1440 AND timeSpent % 15 = 0)   NOT NULL,
                    FOREIGN KEY (punchID) REFERENCES punch_clock(id)
                )
            ''')
            conn.commit()
            conn.close()


def setup(bot): # this is called by Pycord to setup the cog
    bot.add_cog(TimeTracking(bot)) # add the cog to the bot