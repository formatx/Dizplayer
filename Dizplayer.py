import os

import discord
import nest_asyncio
from discord.ext import commands
from dotenv import load_dotenv

from Listener import ListenerCog
from Music import MusicPlayerCog

nest_asyncio.apply()
load_dotenv()
# Get the API token from the .env file.
DISCORD_TOKEN = os.getenv("discord_token")
print(DISCORD_TOKEN)

initial_extensions = ['cogs.listener',
                      'cogs.music']

intents = discord.Intents().all()
bot = commands.Bot(command_prefix='!', intents=intents)

if __name__ == "__main__":
    bot.add_cog(MusicPlayerCog(bot))
    bot.add_cog(ListenerCog(bot))
    bot.run(DISCORD_TOKEN)
