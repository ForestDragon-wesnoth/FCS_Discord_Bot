## discord_commands.py (thin Discord adapter)

# discord_commands.py
from typing import Any, List
from discord.ext import commands
from logic import MatchManager
from vtt_commands import registry
import shlex

class DiscordCtxWrapper:
    def __init__(self, ctx):
        self._ctx = ctx
        gid = getattr(ctx.guild, "id", "DM")
        self.channel_key = f"{gid}:{ctx.channel.id}"
    async def send(self, message: str):
        await self._ctx.send(message)

# Attach a single dynamic dispatcher to the bot.
# Users still type commands like: !match ..., !ent ..., etc.


#TODO: when I add more commands to vtt_commands.py, add them here too!!!

#CURRENTLY THE COMMAND LIST IS OUT-OF-DATE WITH vtt_commands.py!!! UPDATE THIS LATER!!!

def wire_commands(bot: commands.Bot, mgr: MatchManager):
    async def _dispatch(ctx, root: str):
        """
        Re-parse the raw argument tail with shlex to:
        - support quoted args consistently
        - catch malformed quotes and return a friendly error
        """
        # Full message content, e.g. "!ent add sirrobert \"Sir Robert\" 50 5 5 2"
        content = ctx.message.content
        # Example prefix "!"; ctx.invoked_with e.g. "ent"
        prefix = getattr(ctx, "prefix", "")
        invoked = getattr(ctx, "invoked_with", "")
        # Find the start of the args tail right after "<prefix><invoked>"
        # Use first occurrence to be robust to extra spaces after the command
        sig = f"{prefix}{invoked}"
        start = content.find(sig)
        tail = content[start + len(sig):].strip() if start != -1 else ""
        try:
            args = shlex.split(tail)
        except ValueError as e:
            return await ctx.send(f"❌ Parse error: {e}")
        await registry.run(root, args, DiscordCtxWrapper(ctx), mgr)

    # Register top-level commands that forward into the registry
    @bot.command(name="match")
    async def match(ctx, *args):
        await _dispatch(ctx, "match")

    @bot.command(name="ent")
    async def ent(ctx, *args):
        await _dispatch(ctx, "ent")

    @bot.command(name="turn")
    async def turn(ctx, *args):
        await _dispatch(ctx, "turn")

    @bot.command(name="state")
    async def state(ctx, *args):
        await _dispatch(ctx, "state")

    @bot.command(name="store")
    async def store(ctx, *args):
        await _dispatch(ctx, "store")