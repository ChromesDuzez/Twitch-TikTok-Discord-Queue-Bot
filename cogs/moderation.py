import discord
from discord.ext import commands


class Moderation(commands.Cog): # create a class for our cog that inherits from commands.Cog
    # this class is used to create a cog, which is a module that can be added to the bot

    def __init__(self, bot): # this is a special method that is called when the cog is loaded
        self.bot = bot

    @discord.slash_command(name="ping", description="Ping command")
    async def ping(self, 
                   ctx: discord.ApplicationContext
                   ):
        await ctx.respond(f"Pong! Latency is {self.bot.latency}")


def setup(bot): # this is called by Pycord to setup the cog
    bot.add_cog(Moderation(bot)) # add the cog to the bot