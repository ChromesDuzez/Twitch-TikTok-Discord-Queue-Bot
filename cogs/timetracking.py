import discord
from discord.ext import commands
from datetime import datetime
import sqlite3
import os

class Confirm(discord.ui.View):
    def __init__(self, user: discord.User, timeout: int = None):
        super().__init__(timeout=timeout)
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

class ApprovePunch(discord.ui.View):
    def __init__(self, punch: int, message: discord.message, bot: discord.bot, db:str):
        print(f"Created <ApprovePunch: Object> with values: [{punch}, {message}, {db}]")
        super().__init__(timeout=None)
        self.bot = bot
        self.db = db
        self.punch = punch
        self.message = message

        # Dynamically add buttons based on the booleans
        conn = sqlite3.connect(self.db)
        cursor = conn.cursor()
        cursor.execute(f"SELECT punchInApproval, punchOutApproval FROM punch_clock WHERE id = {punch}")
        result = cursor.fetchone()
        #print(result)#debugging
        self.punchInApproval = result[0]
        self.punchOutApproval = result[1]
        conn.commit()
        conn.close()
        if not result[0]:
            self.add_item(self.ConfirmButton("clock-in"))
            self.add_item(self.EditPunch("clock-in"))
        if not result[1]:
            self.add_item(self.ConfirmButton("clock-out"))
            self.add_item(self.EditPunch("clock-out"))
    
    class ConfirmButton(discord.ui.Button):
        def __init__(self, label: str):
            self.CustomLabel = label
            color = discord.ButtonStyle.green
            if label == "clock-out":
                color = discord.ButtonStyle.red
            super().__init__(label=f"Approve {label}", style=color)

        async def callback(self, interaction: discord.Interaction):
            view: ApprovePunch = self.view
            role = None
            if os.getenv('TIMECARD_ADMIN_ROLES'):
                role = discord.utils.get(interaction.user.roles, id=int(os.getenv('TIMECARD_ADMIN_ROLE')))
            if (not role) and (not interaction.user.guild_permissions.administrator):
                await interaction.response.send_message("This is not for you!", ephemeral=True)
                return
            if self.CustomLabel == "clock-in":
                view.punchInApproval = True
            else:
                view.punchOutApproval = True
            conn = sqlite3.connect(view.db)
            cursor = conn.cursor()
            cursor.execute(f'UPDATE punch_clock SET punchInApproval = {view.punchInApproval}, punchOutApproval = {view.punchOutApproval} WHERE id = {view.punch}')
            messageContent = view.message.content
            approvaltxt = f"**You approved this {self.CustomLabel} attempt.**"
            #print(messageContent) #debugging
            if messageContent[-37::] == "Do you approve of this login attempt?":
                messageContent = messageContent[:-37:] + f"{approvaltxt}"
            else:
                messageContent = messageContent + f"\n{approvaltxt}"
            if view.punchInApproval and view.punchOutApproval:
                await interaction.message.edit(content=messageContent, view=None)
                cursor.execute(f'UPDATE punch_clock SET checkChannelId = NULL, checkMessageId = NULL WHERE id = {view.punch}')
                conn.commit()
                conn.close()
            else:
                conn.commit()
                conn.close()
                await interaction.message.edit(content=messageContent, view=ApprovePunch(punch=view.punch, message=view.message, bot=view.bot, db=view.db))
            await interaction.response.send_message(content=approvaltxt, ephemeral=True)
            return

    class EditPunch(discord.ui.Button):
        def __init__(self, label: str):
            self.CustomLabel = label
            color = discord.ButtonStyle.secondary #colors: [primary, secondary, green, red, link]
            # if label == "clock-out":
            #     color = discord.ButtonStyle.red
            super().__init__(label=f"Edit {label}", style=color)

        async def callback(self, interaction: discord.Interaction):
            view: ApprovePunch = self.view
            if interaction.user != view.user:
                await interaction.response.send_message("This is not for you!", ephemeral=True)
                return
            view.value = False
            await interaction.response.send_message("You clicked No!", ephemeral=True)
            message = await view.fetch_message()
            await message.edit(content="You did NOT approve this login attempt.", view=None)
            self.disabled = True

class Clock(discord.ui.View):
    def __init__(self, user: discord.User, message: discord.Message, bot: discord.Bot, db:str, value:bool = False): #, currpunch: int = None
        super().__init__(timeout=None)
        self.bot: discord.Bot = bot
        self.db: str = db
        self.user: discord.User = user
        self.message: discord.Message = message
        self.value: bool = value
        #self.currentpunch: int = currpunch
    
    def get_next_id(self, cursor):
        cursor.execute('SELECT MAX(id) FROM punch_clock')
        result = cursor.fetchone()[0]
        if result is None:
            return 1
        else:
            return result + 1
    
    async def obtain_message(self, bot: discord.bot, channel_id: int, message_id: int):
        cnl = bot.get_channel(channel_id)
        if cnl is None:
            # Fetch the channel if not found in cache
            cnl = await bot.fetch_channel(channel_id)
        return await cnl.fetch_message(message_id)
    
    
    # punch in(user="")
    @discord.ui.button(label="Clock-In", style=discord.ButtonStyle.green)
    async def clockin(self, button: discord.ui.Button, interaction: discord.Interaction):
        user = interaction.user
        role = None
        passedChecks = True
        if os.getenv('TIMECARD_TIMECLOCK_ROLE_ID'):
            role = discord.utils.get(user.roles, id=int(os.getenv('TIMECARD_TIMECLOCK_ROLE_ID')))
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            passedChecks = False
        if self.value == True:
            await interaction.response.send_message("You are already clocked in!", ephemeral=True)
            passedChecks = False
        if passedChecks:
            #connect to the db
            conn = sqlite3.connect(self.db)
            cursor = conn.cursor()
            #set the values for clocking in
            now = datetime.now()
            now_str = now.strftime('%Y-%m-%d %H:%M:%S')
            #check if the person who has pressed the button is allowed to clock in w/o approval
            punchInApproval = True
            approvalMessage: discord.Message = None
            nextid = self.get_next_id(cursor)
            values = (nextid, user.id, now_str, punchInApproval, None, None)
            if role is None:
                punchInApproval = False
                approvalMessage = await self.bot.get_channel(int(os.getenv('TIMECARD_ADMIN_CHANNEL_ID'))).send(f"<@{self.user.id}> attempted to login today at {now_str} in a non-standard way.\nDo you approve of this login attempt?")
                #values = (id, employeeID, punchInTime, punchInApproval, checkChannelId, checkMessageId) for reference
                values = (nextid,user.id, now_str, punchInApproval, approvalMessage.channel.id, approvalMessage.id)
            ## store the clock-in in the db
            cursor.execute(f'INSERT INTO punch_clock (id, employeeID, punchInTime, punchInApproval, checkChannelId, checkMessageId) VALUES (?, ?, ?, ?, ?, ?)', values)
            ## wrap it all up with a nice clean bow
            conn.commit()
            conn.close()
            if not punchInApproval:
                newview = ApprovePunch(punch=nextid, message=approvalMessage, bot=self.bot, db=self.db)
                await approvalMessage.edit(view=newview)
            #update the embed
            self.value = True
            embeds = self.message.embeds
            embeds[0].color = discord.Colour.brand_green()
            embeds[0].title = "You ARE currently clocked in."
            await self.message.edit(embeds=embeds)
            print(f"{self.user} just clocked in.")
            await interaction.response.send_message("You clocked in.", ephemeral=True)


    # punch out(user="")
    @discord.ui.button(label="Clock-Out", style=discord.ButtonStyle.red)
    async def clockout(self, button: discord.ui.Button, interaction: discord.Interaction):
        user = interaction.user
        role = None
        passedChecks = True
        if os.getenv('TIMECARD_TIMECLOCK_ROLE_ID'):
            role = discord.utils.get(user.roles, id=int(os.getenv('TIMECARD_TIMECLOCK_ROLE_ID')))
        if interaction.user != self.user:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            passedChecks = False
        if self.value == False:
            await interaction.response.send_message("You are already clocked out!", ephemeral=True)
            passedChecks = False
        if passedChecks:
            #variables used throughout
            punchOutApproval = True
            approvalMessage: discord.Message = None
            content: str = None
            #connect to the db
            conn = sqlite3.connect(self.db)
            cursor = conn.cursor()
            cursor.execute(f'SELECT MAX(id) FROM punch_clock WHERE employeeID = {self.user.id} AND punchOutTime is NULL ')
            currPunch = cursor.fetchone()[0]
            if currPunch is None:
                print("Critical error: I dont think we shouldve been able to clock out if the employee was never clocked in!")
            cursor.execute(f'SELECT checkChannelId, checkMessageId FROM punch_clock WHERE id = {currPunch}')
            result = cursor.fetchone()
            if not(result[0] is None and result[1] is None):
                approvalMessage = await self.obtain_message(bot=self.bot, channel_id=int(result[0]), message_id=int(result[1]))
            #set the values for clocking in
            now = datetime.now()
            now_str = now.strftime('%Y-%m-%d %H:%M:%S')
            #check if the person who has pressed the button is allowed to clock in w/o approval
            if role is None:
                punchOutApproval = False
                cursor.execute(f'SELECT checkChannelId, checkMessageId FROM punch_clock WHERE id = {currPunch}')
                result = cursor.fetchone()
                if result[0] is None and result[1] is None:
                    approvalMessage = await self.bot.get_channel(int(os.getenv('TIMECARD_ADMIN_CHANNEL_ID'))).send(f"<@{self.user.id}> attempted to logout today at {now_str} in a non-standard way.\nDo you approve of this login attempt?")
                else:
                    content = approvalMessage.content
                    content = content[:-37:] + f"<@{self.user.id}> attempted to logout today at {now_str} in a non-standard way.\n" + content[-37::]
            ## store the clock-out in the db
            if approvalMessage:
                print(f'EXECUTING COMMAND:\nUPDATE punch_clock SET punchOutTime = "{now_str}", punchOutApproval = {punchOutApproval}, checkChannelId = {approvalMessage.channel.id}, checkMessageId = {approvalMessage.id} WHERE id = {currPunch}')#debugging
                cursor.execute(f'UPDATE punch_clock SET punchOutTime = "{now_str}", punchOutApproval = {punchOutApproval}, checkChannelId = {approvalMessage.channel.id}, checkMessageId = {approvalMessage.id} WHERE id = {currPunch}')
            else:
                print(f'EXECUTING COMMAND:\nUPDATE punch_clock SET punchOutTime = "{now_str}", punchOutApproval = {punchOutApproval}, checkChannelId = NULL, checkMessageId = NULL WHERE id = {currPunch}')#debugging
                cursor.execute(f'UPDATE punch_clock SET punchOutTime = "{now_str}", punchOutApproval = {punchOutApproval}, checkChannelId = NULL, checkMessageId = NULL WHERE id = {currPunch}')
            ## wrap it all up with a nice clean bow
            conn.commit()
            conn.close()
            if approvalMessage:
                view = ApprovePunch(punch=currPunch, message=approvalMessage, bot=self.bot, db=self.db)
                if content:
                    await approvalMessage.edit(content=content, view=view)
                else:
                    await approvalMessage.edit(view=view)
            #update the embed
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
        
    #edit employee(user, name="", phonenumber="", addressline1="", addressline2="", city="", state="",, zip="") - admin command
    #remove employee(user) - admin command

                ## Punch Clock Methods
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
            view = Confirm(user=ctx.user, timeout=180)
            await ctx.response.send_message("This emplyee already has a time clock, do you want to proceed?", view=view, ephemeral=True)
            await view.wait()
            if view.value:
                oldMessageID = employees[0][1]
                oldChannelID = employees[0][0]
                print(f"Override Confirmed by {view.user}... Trying to delete old time clock message.")
                await self.deleteclock(ctx, user, oldChannelID, oldMessageID)
            else:
                await ctx.followup.send("You chose not to proceed or did not respond in time.", ephemeral=True)
                return
        ##figure out if the user is clocked in already
        clockedIn = False
        cursor.execute(f"SELECT id, employeeID, punchOutTime FROM punch_clock WHERE employeeID = {user[2:-1:]} ORDER BY id DESC LIMIT 1")
        punches = cursor.fetchall()
        if len(punches) > 0:
            if not punches[0][2]:
                clockedIn = True
        ## go to channel and send the message
        channelObj = ctx.channel
        if channel is not None:
            channelObj = self.bot.get_channel(int(channel[2:-1:]))
        userObj = ctx.guild.get_member(int(user[2:-1:]))
        ##create the embed
        embed = discord.Embed(
            title="You are currently NOT clocked in.",
            color=discord.Colour.brand_red(), # Pycord provides a class with default colors you can choose from
        )
        embed.add_field(name="Wondering how to clock in?", value="Click the green clock-in button and watch the field turn green. And to clock out, hit the red clock-out button. Simple as that!")
        embed.set_footer(text=f"User: {user}")
        embed.set_author(name=f"{userObj} Time Clock", icon_url="https://media.discordapp.net/attachments/1224574847213109330/1244848933675728978/clkfbambooblack600x600-bgf8f8f8.png?ex=66569b69&is=665549e9&hm=61d63652381160c0339fe9fb51cceb8d6971c1878e01b9cb0a181d43e5d97546&=&format=webp&quality=lossless")
        message = await channelObj.send(embed=embed)
        #if already clocked in (in the database) then we gotta make sure this newly created punch clock starts on the right value
        if clockedIn:
            embeds = message.embeds
            embeds[0].color = discord.Colour.brand_green()
            embeds[0].title = "You ARE currently clocked in."
            await message.edit(embeds=embeds)
        view = Clock(user=userObj, message=message, bot=self.bot, db=self.db, value=clockedIn)
        await message.edit(view=view)
        ## store the message id and channel id in the db
        cursor.execute(f"UPDATE employee SET clockChannelId = {message.channel.id}, clockMessageId = {message.id} WHERE id = {user[2:-1:]}")
        ## wrap it all up with a nice clean bow
        conn.commit()
        conn.close()
        await ctx.respond(f"Clock created successfully for {user}", ephemeral=True)
        print(f"{ctx.author} - Clock created successfully for {user}")

        
    # delete clock(user) - admin command
    @discord.slash_command(name="deleteclock", description="Delete a clock embed for a user.")
    @commands.has_permissions(administrator=True)
    async def deleteclock(self,
                          ctx: discord.ApplicationContext,
                          user: discord.Option(str, description="The discord user you wish to delete their time clock for"), # type: ignore
                          channelid: discord.Option(int, default=None, description="The discord channelId where the msg you wish to delete is"), # type: ignore
                          messageid: discord.Option(int, default=None, description="The discord msgId where the msg you wish to delete is"), # type: ignore
                          ):
        ctxInitiated = True
        successful = True
        employees = None
        oldChannelID = channelid
        oldMessageID = messageid
        if oldChannelID and oldMessageID:
            ctxInitiated = False
        if ctxInitiated:
            # connect to db
            conn = sqlite3.connect(self.db)
            cursor = conn.cursor()
            ## use the data in the db to check that we aren't overwriting good data
            cursor.execute(f"SELECT clockChannelId, clockMessageId FROM employee WHERE id = {user[2:-1:]}")
            employees = cursor.fetchall()
            oldMessageID = employees[0][1]
            oldChannelID = employees[0][0]
        try:
            # Fetch the channel by ID
            delchannel = self.bot.get_channel(oldChannelID)
            if delchannel is not None:
                message = await delchannel.fetch_message(oldMessageID)
                # Delete the message
                await message.delete()
                print(f"Message with ID {oldMessageID} deleted from channel {oldChannelID}.")
            else:
                print(f"Channel with ID {oldChannelID} not found.")
        except discord.NotFound:
            print("Message or channel not found.")
            successful = False
        except discord.Forbidden:
            print("I do not have permission to delete this message.")
            successful = False
        except discord.HTTPException as e:
            print(f"Failed to delete message: {e}")
            successful = False
        if ctxInitiated and successful:
            ctx.respond(f"Clock deleted successfully for user: {user}!")


        ### Less important methods ###
    
    # display punches(user="")
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