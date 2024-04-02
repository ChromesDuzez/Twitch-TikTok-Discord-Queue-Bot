## Import statements
from discord.ext import commands
import typing
from dataclasses import dataclass, field
import discord
import os # default module
from dotenv import load_dotenv

load_dotenv() # load all the variables from the env file

#Dataclass of a User which we will store in a queue
@dataclass
class User:
    social_name: str = ""
    alt_name: str = ""
    username: str = ""
    discord_user_id: int = 0

@dataclass
class Queue:
    name: str = ""
    rate: int = 1
    vc_optional: bool = True
    vc_id: int = 0
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
#bot = discord.Bot(command_prefix=".", intents=discord.Intents.all())
bot = discord.Bot()

@bot.slash_command()
async def sync(ctx):
    print("sync command")
    if ctx.channel == bot.get_channel(int(os.getenv('BOT_LOG_ID'))):
        await bot.tree.sync()
        await ctx.respond('Command tree synced.')
    else:
        await ctx.respond('You must be the owner to use this command!')


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
    await ctx.respond("pong!")



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
async def createQueue(ctx, name: str, rate: typing.Optional[int] = 1, vc_id: typing.Optional[int] = 0, vc_optional: typing.Optional[bool] = True):
    if name in getActiveQueuesList():
        await ctx.respond(f"Failed to create queue {name} because a queue already exists with that name.")
        return
    queue = Queue(name, rate, vc_optional, vc_id)
    ACTIVE_QUEUES.append(queue)
    await ctx.respond(f"Created queue: {queue.name}")



@bot.slash_command(
        name="deletequeue",
        description="deletes the queue based on the name you put in"
)
async def deleteQueue(ctx, name):
    for q in ACTIVE_QUEUES[::-1]:
        if q.name == name:
            ACTIVE_QUEUES.remove(q)
    await ctx.respond(f"There are now {len(ACTIVE_QUEUES)} active queues: {getQueuesString()}")



@bot.slash_command(
        name="joinqueue",
        description="joins the author into the specified queue"
)
async def joinQueue(ctx, queue: str, username: typing.Optional[str] = ""):
    author = ctx.author
    alt_name = ""
    if author.nick is not None:
        alt_name = author.nick
    user = User(author.display_name, alt_name, username, author.id)
    if insertUserIntoQueue(queue, user):
        await ctx.respond(str(user) + " added to the queue: " + queue)
    else:
        await ctx.respond(str(user) + " failed to be added to the queue: " + queue)



bot.run(os.getenv('BOT_TOKEN'))