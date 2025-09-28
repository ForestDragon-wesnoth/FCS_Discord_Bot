## discord_commands.py (thin Discord adapter)

# discord_commands.py
from typing import Any, List
from discord.ext import commands
from logic import MatchManager
from vtt_commands import registry

class DiscordCtxWrapper:
    def __init__(self, ctx):
        self._ctx = ctx
        gid = getattr(ctx.guild, "id", "DM")
        self.channel_key = f"{gid}:{ctx.channel.id}"
    async def send(self, message: str):
        await self._ctx.send(message)

# Attach a single dynamic dispatcher to the bot.
# Users still type commands like: !match ..., !ent ..., etc.


#TODO: when I add more commands to vtt_commands.py, add them here too!

def wire_commands(bot: commands.Bot, mgr: MatchManager):
    async def _dispatch(ctx, root: str, *args: str):
        await registry.run(root, list(args), DiscordCtxWrapper(ctx), mgr)

    # Register top-level commands that forward into the registry
    @bot.command(name="match")
    async def match(ctx, *args):
        await _dispatch(ctx, "match", *args)

    @bot.command(name="ent")
    async def ent(ctx, *args):
        await _dispatch(ctx, "ent", *args)

    @bot.command(name="turn")
    async def turn(ctx, *args):
        await _dispatch(ctx, "turn", *args)

    @bot.command(name="state")
    async def state(ctx, *args):
        await _dispatch(ctx, "state", *args)

    @bot.command(name="store")
    async def store(ctx, *args):
        await _dispatch(ctx, "store", *args)