import discord
from discord.ext import commands
import random


class Fun(commands.Cog): # create a class for our cog that inherits from commands.Cog
    # this class is used to create a cog, which is a module that can be added to the bot

    def __init__(self, bot): # this is a special method that is called when the cog is loaded
        self.bot = bot

    @discord.slash_command(name="helpme", description="not gonna help ya btw")
    async def helpme(self, 
                     ctx: discord.ApplicationContext
                     ):
        await ctx.respond("no")


    @discord.slash_command(name="freebird", description="can ya?")
    async def freebird(self, 
                       ctx: discord.ApplicationContext, 
                       name: discord.Option(str, default="", description="Name of the person that you wish to ask.") # type: ignore
                       ):
        print(f"{ctx.author.display_name} initiated freebird command.")
        if name == "":
            name = f"<@{ctx.author.id}>"
        if name[:2:] == "<@":
            await ctx.respond(f"{name}, can you play freebird? \nhttps://youtu.be/0LwcvjNJTuM?si=pRsa-7PUoPI1T1lC")
        else:
            await ctx.respond(f"@{name}, can you play freebird? \nhttps://youtu.be/0LwcvjNJTuM?si=pRsa-7PUoPI1T1lC")

    @discord.slash_command(name="doodob", description="yeah cuh")
    async def doodob(self,
                     ctx: discord.ApplicationContext
                     ):
        await ctx.respond(f"Welcome {ctx.author.display_name} to the Doodob Jedi Temple Discord.")
    
    @discord.slash_command(name="shadow", description="bye guyyysss")
    async def shadow(self,
                     ctx: discord.ApplicationContext
                     ):
        responses = ["Hi cheddar my best friend", "Sorry, guys are gonna go.", "Hi \nEverybody ", "Hi", "<@289752347842707456>  Do you wanna play?"]
        await ctx.respond(random.choice(responses))
    
    @discord.slash_command(name="cheddar", description="Cheddarrrr")
    async def cheddar(self,
                     ctx: discord.ApplicationContext
                     ):
        responses = ["What the heck Cheddar?", "You're so funny Cheddar", "Why is our fill teammate a dictator?", "guys are gonna go", "Vote for Cheddar in the Roles Channel", 
                     "Don't let me find a shockwave grenade"]
        await ctx.respond(random.choice(responses))


def setup(bot): # this is called by Pycord to setup the cog
    bot.add_cog(Fun(bot)) # add the cog to the bot