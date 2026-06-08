## discord_commands.py (thin Discord adapter)

# discord_commands.py
from typing import Any, List
import discord
from discord.ext import commands
from logic import MatchManager
from vtt_commands import registry
import shlex

#DEBUG_CMDS = True         # console logging
DEBUG_CMDS = False         # console logging
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
            #_dbg(ctx, batch_skip="empty_after_strip", line=line)
            return False

        root = parts[0]
        # If the line didn't have '!' and the first token isn't a known root, skip
        if not line.strip().startswith(prefix) and root not in known_roots:
            _dbg(ctx, batch_skip="not_a_root", line=line, first_token=root)
            return False

        args = parts[1:]
        _dbg(ctx, batch_exec_line=line, parsed_root=root, parsed_args=args)
        await registry.run(root, args, DiscordCtxWrapper(ctx, mgr), mgr)
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
    def __init__(self, ctx, mgr=None):
        self._ctx = ctx
        self._mgr = mgr
        gid = getattr(ctx.guild, "id", "DM")
        self.channel_key = f"{gid}:{ctx.channel.id}"
        # Identity comes from the message author. user_id is the stable
        # Discord snowflake (as a string); user_name is for display.
        author = getattr(ctx, "author", None)
        self.user_id = str(getattr(author, "id", "")) or "unknown"
        self.user_name = (
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or self.user_id
        )
        # Real Discord authors are fixed — identity can't be reassigned
        # mid-session the way the CLI's stand-in can.
        self.cli_mutable = False
    async def send(self, message: str):
        await self._ctx.send(message)

    async def send_approval(self, req: dict):
        """Post an approval request with clickable Approve/Deny buttons.
        Falls back to a plain text prompt if the discord UI components
        aren't available. Called by the dispatcher when a non-host's
        command is queued (see CommandRegistry.run)."""
        cmd = "!" + req["name"] + (" " + " ".join(req["args"]) if req["args"] else "")
        text = (
            f"🕓 **{req['user_name']}** requests `{cmd}` "
            f"(id `{req['id']}`). A host can approve or deny."
        )
        try:
            view = _ApprovalView(req, self._mgr, self.channel_key)
            await self._ctx.send(text, view=view)
        except Exception:
            # No UI support (older discord.py) — text prompt + commands.
            await self._ctx.send(
                text + f"\nUse `!approve {req['id']}` or `!deny {req['id']}`."
            )


class _InteractionCtx:
    """ReplyContext built from a button-click interaction, so an approved
    command runs with the CLICKER's identity (a host) against the
    interaction's channel."""
    cli_mutable = False

    def __init__(self, interaction):
        guild = getattr(interaction, "guild", None)
        gid = getattr(guild, "id", "DM")
        self.channel_key = f"{gid}:{interaction.channel_id}"
        user = interaction.user
        self.user_id = str(getattr(user, "id", "")) or "unknown"
        self.user_name = (
            getattr(user, "display_name", None)
            or getattr(user, "name", None)
            or self.user_id
        )
        self._channel = interaction.channel

    async def send(self, message: str):
        await self._channel.send(message)


class _ApprovalView(discord.ui.View):
    """Approve / Deny buttons for one pending request. Only a host of the
    request's match may resolve it; non-hosts get an ephemeral refusal.
    Approving re-dispatches the original command with host authority via
    the shared registry; denying just drops it. Disables itself once
    resolved (or on timeout)."""

    def __init__(self, req: dict, mgr, channel_key: str, timeout: float = 600.0):
        super().__init__(timeout=timeout)
        self._req = req
        self._mgr = mgr
        self._channel_key = channel_key

    def _match(self):
        if self._mgr is None:
            return None
        mid = self._mgr.active_by_channel.get(self._channel_key)
        return self._mgr.matches.get(mid) if mid is not None else None

    async def _require_host(self, interaction) -> bool:
        m = self._match()
        ictx = _InteractionCtx(interaction)
        if m is None or not m.is_host(ictx.user_id):
            await interaction.response.send_message(
                "❌ Only a host can resolve this request.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        if not await self._require_host(interaction):
            return
        m = self._match()
        req = m.pop_pending_request(self._req["id"]) if m else None
        if req is None:
            await interaction.response.send_message(
                "Already resolved.", ephemeral=True
            )
            return self._finish()
        await interaction.response.defer()
        ictx = _InteractionCtx(interaction)
        cmd = "!" + req["name"] + (" " + " ".join(req["args"]) if req["args"] else "")
        await ictx.send(
            f"✅ {ictx.user_name} approved `{cmd}` (by {req['user_name']})."
        )
        await registry.run(req["name"], req["args"], ictx, self._mgr)
        await self._disable(interaction)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        if not await self._require_host(interaction):
            return
        m = self._match()
        req = m.pop_pending_request(self._req["id"]) if m else None
        if req is None:
            await interaction.response.send_message(
                "Already resolved.", ephemeral=True
            )
            return self._finish()
        cmd = "!" + req["name"] + (" " + " ".join(req["args"]) if req["args"] else "")
        await interaction.response.send_message(
            f"🚫 {interaction.user.display_name} denied `{cmd}` "
            f"(by {req['user_name']})."
        )
        await self._disable(interaction)

    async def _disable(self, interaction):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        self._finish()

    def _finish(self):
        self.stop()

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
        await registry.run(bound_root, args, DiscordCtxWrapper(ctx, mgr), mgr)

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
