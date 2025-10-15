## discord_commands.py (thin Discord adapter)

# discord_commands.py
from typing import Any, List
from discord.ext import commands
from logic import MatchManager
from vtt_commands import registry
import shlex

DEBUG_CMDS = True         # console logging
DEBUG_CMDS_CHAT = False   # set True to also echo minimal info into Discord

def _dbg(ctx, **kv):
    if not DEBUG_CMDS:
        return
    # Safe console log (avoids spamming channels)
    try:
        who = f"g={getattr(getattr(ctx, 'guild', None), 'id', 'DM')} ch={getattr(getattr(ctx, 'channel', None), 'id', '?')}"
        print("[CMDDBG]", who, {k: v for k, v in kv.items()})
    except Exception:
        pass

async def _dbg_chat(ctx, text: str):
    if DEBUG_CMDS_CHAT:
        try:
            await ctx.send(f"DBG: {text}")
        except Exception:
            pass

# Max commands to run from a single paste to avoid accidental spam
BATCH_MAX_LINES = 200

def _is_comment_or_blank(line: str) -> bool:
    s = (line or "").strip()
    return not s or s.startswith("#")

def _strip_prefix(line: str, prefix: str) -> str:
    s = line.lstrip()
    if prefix and s.startswith(prefix):
        s = s[len(prefix):].lstrip()
    return s

async def _parse_and_run_single_line(ctx, line: str, mgr, known_roots) -> bool:
    """
    Returns True if it executed something, False if skipped.
    """
    try:
        prefix = getattr(ctx, "prefix", "")
        s = _strip_prefix(line, prefix)

        # If user omitted '!' but started with a known root, allow it.
        # Otherwise, if they included '!', the prefix strip already handled it.
        # After this, s should be "root arg1 arg2 ..."
        parts = shlex.split(s)
        if not parts:
            _dbg(ctx, batch_skip="empty_after_strip", line=line)
            return False

        root = parts[0]
        # If the line didn't have '!' and the first token isn't a known root, skip
        if not line.strip().startswith(prefix) and root not in known_roots:
            _dbg(ctx, batch_skip="not_a_root", line=line, first_token=root)
            return False

        args = parts[1:]
        _dbg(ctx, batch_exec_line=line, parsed_root=root, parsed_args=args)
        await registry.run(root, args, DiscordCtxWrapper(ctx), mgr)
        return True
    except ValueError as e:
        _dbg(ctx, batch_parse_error=str(e), line=line)
        await ctx.send(f"❌ Parse error in line: `{line}`\n→ {e}")
        return False
    except Exception as e:
        _dbg(ctx, batch_unexpected_error=str(e), line=line)
        await ctx.send(f"❌ Error executing line: `{line}`\n→ {e}")
        return False


class DiscordCtxWrapper:
    def __init__(self, ctx):
        self._ctx = ctx
        gid = getattr(ctx.guild, "id", "DM")
        self.channel_key = f"{gid}:{ctx.channel.id}"
    async def send(self, message: str):
        await self._ctx.send(message)

#now supports-multiple commands in one message - one command per line
def wire_commands(bot: commands.Bot, mgr: MatchManager):
    async def _dispatch(ctx, bound_root: str):
        content = ctx.message.content or ""
        s = content.lstrip()
    
        # --- BATCH MODE: multiple non-empty lines pasted in one message ---
        lines = [ln for ln in content.splitlines() if not _is_comment_or_blank(ln)]
        if len(lines) > 1:
            known_roots = set(registry._handlers.keys())
            _dbg(ctx, batch_detected=True, line_count=len(lines))
    
            if len(lines) > BATCH_MAX_LINES:
                await ctx.send(f"⚠️ Paste has {len(lines)} lines; max allowed is {BATCH_MAX_LINES}. Aborting.")
                return
    
            executed = 0
            for i, line in enumerate(lines, 1):
                ok = await _parse_and_run_single_line(ctx, line, mgr, known_roots)
                executed += int(ok)
    
            _dbg(ctx, batch_done=True, executed=executed, total=len(lines))
            # Optional: summarize; individual commands will have already sent their outputs
            await _dbg_chat(ctx, f"batch executed {executed}/{len(lines)} lines")
            return
        # --- END BATCH MODE ---
    
        # (keep your existing single-line logic below)
        _dbg(
            ctx,
            content=content,
            prefix=getattr(ctx, "prefix", None),
            invoked_with=getattr(ctx, "invoked_with", None),
            command=getattr(getattr(ctx, "command", None), "name", None),
            bound_root=bound_root,
        )
    
        prefix = getattr(ctx, "prefix", "")
        if prefix and s.startswith(prefix):
            s = s[len(prefix):].lstrip()
        _dbg(ctx, after_prefix=s)
    
        if s[:len(bound_root)].lower() == bound_root.lower():
            s = s[len(bound_root):].lstrip()
            _dbg(ctx, root_stripped_by="bound_root", after_root=s)
        else:
            parts = s.split(maxsplit=1)
            s = parts[1] if len(parts) > 1 else ""
            _dbg(ctx, root_stripped_by="fallback_first_token", after_root=s)
    
        try:
            args = shlex.split(s)
        except ValueError as e:
            _dbg(ctx, parse_error=str(e), raw_tail=s)
            await _dbg_chat(ctx, f"parse error: {e}")
            return await ctx.send(f"❌ Parse error: {e}")
    
        _dbg(ctx, final_args=args)
        await _dbg_chat(ctx, f"root={bound_root} args={args}")
        await registry.run(bound_root, args, DiscordCtxWrapper(ctx), mgr)

    # Make sure discord.py’s default help is gone (your registry defines its own help)
    if bot.get_command("help"):
        bot.remove_command("help")

    # --- factory to avoid late-binding bugs in a loop ---
    def register_one(root: str):
        async def _cmd(ctx):
            _dbg(ctx, entry="_cmd", bound_root=root)
            await _dispatch(ctx, root)
        # apply the decorator explicitly at runtime
        bot.command(name=root, ignore_extra=True)(_cmd)

    # Remove any pre-existing commands with the same names, then register
    for root in list(registry._handlers.keys()):
        if bot.get_command(root):
            bot.remove_command(root)
        register_one(root)
