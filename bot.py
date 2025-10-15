import os
import discord
from discord.ext import commands
from logic import MatchManager
from discord_commands import wire_commands

# --- Token loading ---
def load_token() -> str:
    """
    Load the Discord bot token.
    Priority:
      1. Environment variable DISCORD_TOKEN
      2. 'bot_token.txt' file (ignored in git)
    """

#unused
#    token = os.getenv("DISCORD_TOKEN")
#    if token:
#        return token.strip()

    token_path = "1bot_token.txt"
    if os.path.exists(token_path):
        with open(token_path, "r", encoding="utf-8") as f:
            line = f.readline().strip()
            if line:
                return line

    raise RuntimeError(
        "❌ Discord token not found. Set DISCORD_TOKEN environment variable "
        "or create a 'bot_token.txt' file containing your token."
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