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
    
    @discord.slash_command(name="sand", description="Bigger Brain Bigger IQ")
    async def sand(self,
                   ctx: discord.ApplicationContext
                   ):
        print(f"{ctx.author.display_name} initiated sand command.")
        try:
            file = discord.File("media/yumm.gif")
            await ctx.respond(file=file)
        except:
            print("Exception caught in sand command.")
            await ctx.respond("https://media.discordapp.net/attachments/1223351898073989190/1229146773050884166/yumm.gif?ex=662e9f23&is=661c2a23&hm=0db6c19f391899878c80be8c2e0bf83e0a61ce9dab072df5c506bb75c2a96b18&=")
    

    @discord.slash_command(name="cheese", description="Cheese War")
    async def cheese(self,
                     ctx: discord.ApplicationContext
                     ):
        print(f"{ctx.author.display_name} initiated cheese command.")
        try:
            file = discord.File("media/cheese.mov")
            await ctx.respond(file=file)
        except:
            print("Exception caught in cheese command.")
            await ctx.respond("https://media.discordapp.net/attachments/1223351898073989190/1227490539742691349/v0f044gc0000cnns4snog65v1qa2uka0.mov?ex=662898a6&is=661623a6&hm=c5826cf45ad5c73ad9b38c19fc6f77ed62eca688ed38c50bad7983ae57b1213e&=")


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

    @discord.slash_command(name="sqwalla", description="Forever online R.I.P.")
    async def sqwalla(self,
                      ctx: discord.ApplicationContext
                      ):
        responses = [("media/little_rat_whip.gif","https://media.discordapp.net/attachments/1224574847213109330/1229649553276276756/little_rat_whip.gif?ex=66307363&is=661dfe63&hm=853f4b284ec1602a00c6431d3a126e1f7406e89f60b151093e69833e183c9203&="),
                     ("media/rat_pelvic_thrust.gif","https://media.discordapp.net/attachments/1224574847213109330/1229649495818506300/rat_pelvic_thrust.gif?ex=66307355&is=661dfe55&hm=549cb4558ae82eb6d473a1e204875bf6121260646d4b46069f922ae35be1f97e&="),
                     ("media/little_rat_whip.mov","https://media.discordapp.net/attachments/1224574847213109330/1229650482931433512/little_rat_whip.mov?ex=66307440&is=661dff40&hm=a3e20e03e9477ec89a3e1bb666919889088046efb3850a63b44578d2a4583ef2&"),
                     ("media/rat_pelvic_thrust.mov","https://media.discordapp.net/attachments/1224574847213109330/1229650482394304523/rat_pelvic_thrust.mov?ex=66307440&is=661dff40&hm=4b15bfbce821428e736ed69f889404b2af4ed252d5a34819c207c718818e2715&"),
                     ("media/rat_griddy.mp4","https://cdn.discordapp.com/attachments/1223351898073989190/1233216980480430090/rat_griddy.mp4?ex=662c4a50&is=662af8d0&hm=88f9d6a37045fd7316f40477c4cd11ad37242fc81cdcf0b2e392f7c935817d24&"),
                     ("media/rat_griddy.gif","https://media.discordapp.net/attachments/1223351898073989190/1233216971571466240/rat_griddy.gif?ex=662c4a4e&is=662af8ce&hm=e7ffd62fff59f683865375856cd8203bdf0fb6a2e469aa3a675e5628abfe6d37&=")
                     ]
        print(f"{ctx.author.display_name} initiated sqwalla command.")
        response = random.choice(responses)
        try:
            file = discord.File(response[0])
            await ctx.respond("<@438846432703807488>", file=file)
        except:
            print("Exception caught in sqwalla command.")
            await ctx.respond("<@438846432703807488>" + str(response))


def setup(bot): # this is called by Pycord to setup the cog
    bot.add_cog(Fun(bot)) # add the cog to the bot