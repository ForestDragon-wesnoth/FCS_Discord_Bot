import os
import discord
from discord.ext import commands
from storage import open_workspaces
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

# Replies echo text users and GMs typed (unit names, bad arguments, command
# names), so a reply could carry @everyone, a role or a <@user> mention and
# ping the server. The bot's messages never mean to ping anyone.
bot = commands.Bot(command_prefix="!", intents=intents,
                   allowed_mentions=discord.AllowedMentions.none())
# One workspace per Discord server (systems, matches, saves), loaded from the
# data/ folder at startup and written back after every command.
_workspaces = open_workspaces()

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

wire_commands(bot, _workspaces)

try:
    bot.run(TOKEN)
finally:
    # Every command already wrote its changes; this catches anything else
    # (a pan button's camera move that failed to write, ...).
    _workspaces.storage.commit_all(_workspaces)
