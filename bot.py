## bot.py (tiny, unchanged except wiring the adapter)

# bot.py
import os
import discord
from discord.ext import commands
from logic import MatchManager
from discord_commands import wire_commands

TOKEN = os.getenv("DISCORD_TOKEN") or "YOUR_BOT_TOKEN_HERE"
intents = discord.Intents.default(); intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

_mgr = MatchManager()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

wire_commands(bot, _mgr)

bot.run(TOKEN)