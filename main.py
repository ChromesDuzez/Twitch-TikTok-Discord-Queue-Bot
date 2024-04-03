## Import statements
from discord.ext import commands
import typing
from dataclasses import dataclass, field
import discord
import os # default module
from dotenv import load_dotenv

load_dotenv() 

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

ACTIVE_QUEUES = list()

def getActiveQueuesList():
    return [q.name for q in ACTIVE_QUEUES]


def getQueuesString():
    queues = "<Type: None>"
    if len(ACTIVE_QUEUES) > 0:
        queues = ACTIVE_QUEUES[0].name
    if len(ACTIVE_QUEUES) > 1:
        for q in ACTIVE_QUEUES[1::]:
            queues.append(", " + q.name)
    return queues

def insertUserIntoQueue(queueName, user):
    success = False
    for q in ACTIVE_QUEUES:
        if q.name == queueName:
            success = True
            q.user_list.append(user)
    return success

#Starting the discord bot
bot = discord.Bot(command_prefix="!", help_command=commands.DefaultHelpCommand())


async def get_active_queues(ctx: discord.AutocompleteContext):
    if len(ACTIVE_QUEUES) > 0:
        return [q.name for q in ACTIVE_QUEUES]
    return ["No active queues"]


async def get_voice_channels(ctx: discord.AutocompleteContext):
    return [channel for channel in bot.get_all_channels() if type(channel) == discord.VoiceChannel]


@bot.event
async def on_ready():
    print("Hello! Chromes Py-Bot is ready!")
    channel = bot.get_channel(int(os.getenv('BOT_LOG_ID')))
    await channel.send("Hello! Chromes Py-Bot is ready!")



@bot.slash_command(
        name="ping",
        description="Ping command"
)
async def ping(ctx):
    await ctx.respond(f"Pong! Latency is {bot.latency}")



@bot.slash_command(
        name="helpme",
        description="not gonna help ya btw"
)
async def helpme(ctx):
    await ctx.respond("no")



@bot.slash_command(
        name="getqueues",
        description="Get the queues locked or not"
)
async def getQueues(ctx):
    await ctx.respond(f"There are {len(ACTIVE_QUEUES)} active queues: {getQueuesString()}")


@bot.slash_command(
        name="createqueue",
        description="it's a little on the nose ik"
)
async def createQueue(ctx: discord.ApplicationContext, 
                      name: discord.Option(str, description="Name the darn queue. Must be unique."),  # type: ignore
                      rate: discord.Option(int, default=1, description="The default rate that we move people through the queue."),  # type: ignore
                      voice_channel: discord.Option(discord.VoiceChannel, default=None, autocomplete=discord.utils.basic_autocomplete(get_voice_channels), description="The voice channel that is the waiting room for the active users."),   # type: ignore
                      active_voice_channel: discord.Option(discord.VoiceChannel, default=None, autocomplete=discord.utils.basic_autocomplete(get_voice_channels), description="The voice channel that users that are at bat go to."),   # type: ignore
                      vc_optional: discord.Option(bool, default=True, choices=[True, False], description="Is joining the vc is optional for the queue. Not compatible twitch and tik tok queue integration.")  # type: ignore
                      ):
    if name in getActiveQueuesList():
        await ctx.respond(f"Failed to create queue {name} because a queue already exists with that name.")
        return
    queue = Queue(name, rate, vc_optional, voice_channel, active_voice_channel)
    ACTIVE_QUEUES.append(queue)
    await ctx.respond(f"Created queue: {queue.name}")


@bot.slash_command(
        name="deletequeue",
        description="deletes the queue based on the name you put in"
)
async def deleteQueue(ctx: discord.ApplicationContext, 
                      name: discord.Option(str, autocomplete=discord.utils.basic_autocomplete(get_active_queues))   # type: ignore
                      ):
    for q in ACTIVE_QUEUES[::-1]:
        if q.name == name:
            ACTIVE_QUEUES.remove(q)
    await ctx.respond(f"There are now {len(ACTIVE_QUEUES)} active queues: {getQueuesString()}")


@bot.slash_command(
        name="joinqueue",
        description="joins the author into the specified queue"
)
async def joinQueue(ctx: discord.ApplicationContext, 
                    queue: discord.Option(str, autocomplete=discord.utils.basic_autocomplete(get_active_queues)),   # type: ignore
                    username: typing.Optional[str] = ""
                    ):
    author = ctx.author
    soc_name = author.display_name
    if author.nick is not None:
        alt_name = author.nick
    user = User(soc_name, username, author.id)
    if insertUserIntoQueue(queue, user):
        await ctx.respond(str(user) + " added to the queue: " + queue)
    else:
        await ctx.respond(str(user) + " failed to be added to the queue: " + queue)



bot.run(os.getenv('BOT_TOKEN'))