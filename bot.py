import os
import discord
from discord.ext import commands
from logic import MatchManager
from discord_commands import wire_commands

# --- Token loading ---
TOKEN_FILE = "1bot_token.txt"


def load_token() -> str:
    """
    Load the Discord bot token from the first line of TOKEN_FILE (ignored in
    git). The DISCORD_TOKEN environment-variable path below is disabled.
    """

#unused
#    token = os.getenv("DISCORD_TOKEN")
#    if token:
#        return token.strip()

    token_path = TOKEN_FILE
    if os.path.exists(token_path):
        with open(token_path, "r", encoding="utf-8") as f:
            line = f.readline().strip()
            if line:
                return line

    raise RuntimeError(
        f"❌ Discord token not found. Create a '{TOKEN_FILE}' file in the folder "
        f"you run bot.py from, with your token on its first line."
    )

# --- Discord setup ---
TOKEN = load_token()
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
_mgr = MatchManager()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

wire_commands(bot, _mgr)

bot.run(TOKEN)