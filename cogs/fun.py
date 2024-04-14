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
        responses = ["no", "absolutely not", "negative", "If you were to say \"don't help me\" I'd say \"affirmative\".", "negatory"]
        await ctx.respond(random.choice(responses))


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
                     "Don't let me find a shockwave grenade", "nuh uh", "👋  hi chromessss"]
        await ctx.respond(random.choice(responses))
    
    @discord.slash_command(name="cheese", description="Cheese War")
    async def cheese(self,
                     ctx: discord.ApplicationContext
                     ):
        print(f"{ctx.author.display_name} initiated cheese command.")
        try:
            file = discord.File("media/cheese.mov")
            await ctx.respond(file=file)
        except:
            await ctx.respond("https://media.discordapp.net/attachments/1223351898073989190/1227490539742691349/v0f044gc0000cnns4snog65v1qa2uka0.mov?ex=662898a6&is=661623a6&hm=c5826cf45ad5c73ad9b38c19fc6f77ed62eca688ed38c50bad7983ae57b1213e&")
    
    @discord.slash_command(name="chromes", description="Chromes lines")
    async def chromes(self,
                     ctx: discord.ApplicationContext
                     ):
        print(f"{ctx.author.display_name} initiated chromes command.")
        responses = ["👋 Hi Cheddarrrrr", "indubitably good sir", "yes yes good showwww", "hahahaha", "heheheheha", "great success", "alrighty then"]
        await ctx.respond(random.choice(responses))

    @discord.slash_command(name="wave", description="hiiiii")
    async def wave(self,
                   ctx: discord.ApplicationContext,
                   name: discord.Option(str, description="Name of the person that you wish to say hi to."), # type: ignore
                   length: discord.Option(int, default=5, description="How many letters to add onto the end. (Default is 5. Max is 1000.)") # type:ignore
                   ):
        if length > 1000:
            length = length % 1000
        print(f"{ctx.author.display_name} initiated wave command.")
        if name[:2:] == "<@":
            await ctx.respond(f"👋 Hi {name}")
        else:
            await ctx.respond(f"👋 Hi {name + (name[-1] * length)}")


def setup(bot): # this is called by Pycord to setup the cog
    bot.add_cog(Fun(bot)) # add the cog to the bot