import discord
from discord.ext import commands
from dataclasses import dataclass, field

#Dataclass of a User which we will store in a queue
@dataclass
class User:
    social_name: str = ""
    username: str = ""
    discord_user_id: int = 0

@dataclass
class Queue:
    name: str = ""
    rate: int = 1
    vc_optional: bool = True
    queue_voice_channel: discord.VoiceChannel = None
    active_voice_channel: discord.VoiceChannel = None
    users_at_bat: list[User] = field(default_factory=list)
    user_list: list[User] = field(default_factory=list)
    locked: bool = False


class Functionality(commands.Cog): # create a class for our cog that inherits from commands.Cog
    # this class is used to create a cog, which is a module that can be added to the bot
    def __init__(self, bot):  # this is a special method that is called when the cog is loaded
        self.bot = bot
        self.ACTIVE_QUEUES = list()

    def getActiveQueuesList(self):
        return [q.name for q in self.ACTIVE_QUEUES]


    def getQueuesString(self):
        queues = "<Type: None>"
        if len(self.ACTIVE_QUEUES) > 0:
            queues = self.ACTIVE_QUEUES[0].name
        if len(self.ACTIVE_QUEUES) > 1:
            for q in self.ACTIVE_QUEUES[1::]:
                queues.append(", " + q.name)
        return queues

    def insertUserIntoQueue(self, queueName, user):
        success = False
        for q in self.ACTIVE_QUEUES:
            if q.name == queueName:
                success = True
                q.user_list.append(user)
        return success


    async def get_active_queues(self, ctx: discord.AutocompleteContext):
        if len(self.ACTIVE_QUEUES) > 0:
            return [q.name for q in self.ACTIVE_QUEUES]
        return ["No active queues"]


    async def get_voice_channels(self, ctx: discord.AutocompleteContext):
        return [channel for channel in self.bot.get_all_channels() if type(channel) == discord.VoiceChannel]



    @discord.slash_command(name="getqueues", description="Get the queues locked or not")
    async def getQueues(self,
                        ctx: discord.ApplicationContext
                        ):
        await ctx.respond(f"There are {len(self.ACTIVE_QUEUES)} active queues: {self.getQueuesString()}")


    @discord.slash_command(name="createqueue", description="it's a little on the nose ik")
    async def createQueue(self,
                          ctx: discord.ApplicationContext,
                          name: discord.Option(str, description="Name the darn queue. Must be unique."),  # type: ignore
                          rate: discord.Option(int, default=1, description="The default rate that we move people through the queue."),  # type: ignore
                          voice_channel: discord.Option(discord.VoiceChannel, default=None, autocomplete=discord.utils.basic_autocomplete(get_voice_channels), description="The voice channel that is the waiting room for the active users."),   # type: ignore
                          active_voice_channel: discord.Option(discord.VoiceChannel, default=None, autocomplete=discord.utils.basic_autocomplete(get_voice_channels), description="The voice channel that users that are at bat go to."),   # type: ignore
                          vc_optional: discord.Option(bool, default=True, choices=[True, False], description="Is joining the vc is optional for the queue. Not compatible twitch and tik tok queue integration.")  # type: ignore
                          ):
        if name in self.getActiveQueuesList():
            await ctx.respond(f"Failed to create queue {name} because a queue already exists with that name.")
            return
        queue = Queue(name, rate, vc_optional, voice_channel, active_voice_channel)
        self.ACTIVE_QUEUES.append(queue)
        await ctx.respond(f"Created queue: {queue.name}")


    @discord.slash_command(name="deletequeue", description="deletes the queue based on the name you put in")
    async def deleteQueue(self,
                          ctx: discord.ApplicationContext,
                          name: discord.Option(str, autocomplete=discord.utils.basic_autocomplete(get_active_queues), description="Name of the queue that you wish to delete.")   # type: ignore
                          ):
        for q in self.ACTIVE_QUEUES[::-1]:
            if q.name == name:
                self.ACTIVE_QUEUES.remove(q)
        await ctx.respond(f"There are now {len(self.ACTIVE_QUEUES)} active queues: {self.getQueuesString()}")


    @discord.slash_command(name="joinqueue", description="joins the author into the specified queue")
    async def joinQueue(self,
                        ctx: discord.ApplicationContext, 
                        queue: discord.Option(str, autocomplete=discord.utils.basic_autocomplete(get_active_queues), description="Which queue to join?"),   # type: ignore
                        username: discord.Option(str, default="", description="Alternate username to join the queue as.")   # type: ignore
                        ):
        author = ctx.author
        soc_name = author.display_name
        if author.nick is not None:
            soc_name = author.nick
        user = User(soc_name, username, author.id)
        if self.insertUserIntoQueue(queue, user):
            await ctx.respond(str(user) + " added to the queue: " + queue)
        else:
            await ctx.respond(str(user) + " failed to be added to the queue: " + queue)

    @discord.slash_command(name="queuelist", description="Displays the specified queue.")
    async def queuelist(self,
                        ctx: discord.ApplicationContext #,
                        #queue: discord.Option(str, autocomplete=discord.utils.basic_autocomplete(get_active_queues), description="The Queue to display") #type: ignore
                        ):
        
        embed = discord.Embed(
            title="Currently Waiting in <Queue Name>",
            description="A list of all of the people in the current queue.",
            color=discord.Colour.blurple(), # Pycord provides a class with default colors you can choose from
        )
        embed.add_field(name="list", value="No one is in the queue!")
        embed.set_footer(text="A ChromesDuzez Discord Bot.")
        embed.set_author(name="Chromes Queue Bot", icon_url="https://images-ext-1.discordapp.net/external/u8AGQg7qA9kEMKt9DaPeDqi_Dv411zgLjIUBqIcI2_E/%3Fsize%3D1024/https/cdn.discordapp.com/guilds/1223328147827720366/users/336231886198276096/avatars/84394ecd081eea0edea003a664d018ed.png")
        await ctx.respond(embed=embed) # Send the embed with some text
        
        


def setup(bot): # this is called by Pycord to setup the cog
    bot.add_cog(Functionality(bot)) # add the cog to the bot