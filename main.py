## Import statements
from discord.ext import commands
from dataclasses import dataclass, field
import discord
import os # default module
from dotenv import load_dotenv

load_dotenv() 

cogs_list = [
    'moderation',
    'fun',
    'functionality'
]

#Starting the discord bot
bot = discord.Bot(command_prefix="$", help_command=commands.DefaultHelpCommand())

for cog in cogs_list:
    print(f"Loading Cog {cog}")
    bot.load_extension(f'cogs.{cog}')

@bot.event
async def on_ready():
    await synced()
    print("Hello! Chromes Py-Bot is ready!")
    channel = bot.get_channel(int(os.getenv('BOT_LOG_ID')))
    await channel.send("Hello! Chromes Py-Bot is ready!")
    


async def synced():
    if bot.auto_sync_commands:
        await bot.sync_commands()
    print(f"{bot.user.name} connected.")
    for cmd in bot.commands:
        print(f"Syncing: {cmd}")
        await bot.process_application_commands(cmd)



bot.run(os.getenv('BOT_TOKEN'))