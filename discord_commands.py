## discord_commands.py (thin Discord adapter)

# discord_commands.py
from typing import Any, List, Dict, Optional, Tuple
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

# Discord rejects a message whose `content` exceeds a hard server-side
# length cap (API error 50035). 2000 is the universally-safe bot limit
# (works regardless of Nitro/boost/channel context). Long command output
# (e.g. !help, !system rules, big maps) is split into multiple messages
# at this boundary — see _split_for_discord.
DISCORD_MAX_CONTENT = 2000

def _split_for_discord(message: str, limit: int = DISCORD_MAX_CONTENT) -> List[str]:
    """Split `message` into a list of chunks, each at most `limit`
    characters, breaking ONLY at newline boundaries so a single logical
    line is never cut across two messages. Consecutive lines are packed
    into one chunk until the next line wouldn't fit.

    A single line longer than `limit` (no newline to break on) is the one
    case we can't honor cleanly — it's hard-split into limit-sized pieces
    as a last resort. Returns at least one chunk (the message unchanged
    when it already fits, including the empty string)."""
    if len(message) <= limit:
        return [message]
    chunks: List[str] = []
    cur = ""
    for line in message.split("\n"):
        # Flush the current chunk if appending this line (plus the
        # rejoining newline, when cur is non-empty) would overflow.
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = ""
        if len(line) > limit:
            # Oversized single line: flush whatever's buffered, then
            # hard-split the line itself.
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            continue
        cur = line if not cur else cur + "\n" + line
    if cur:
        chunks.append(cur)
    return chunks

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
    # Discord renders ANSI colors inside ```ansi code blocks, so the
    # colorized map renderer is enabled for this surface.
    supports_color = True
    # Discord messages are narrow, so the map viewport engages in 'auto'
    # mode on large maps (pan with `!map pan` / the arrow buttons).
    viewport_capable = True

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
        # Split over-long output at line boundaries so we never trip
        # Discord's content-length cap (see _split_for_discord).
        for chunk in _split_for_discord(message):
            await self._ctx.send(chunk)

    async def set_autoupdate(self, m, on: bool) -> str:
        """Turn this channel's self-refreshing map board on/off. Posts the
        board message (with pan buttons when the viewport is engaged) and
        registers it for post-command refresh; returns a status line. Called
        by the `!map autoupdate` handler via getattr (Discord-only — other
        surfaces lack this method and report the feature as unavailable)."""
        if not on:
            _boards.pop(self.channel_key, None)
            return "🗺️ Auto-update board OFF for this channel."
        text, engaged = _board_render(m, self.channel_key)
        view = _PanView(self.channel_key, self._mgr) if engaged else None
        try:
            msg = await self._ctx.send(text, view=view) if view \
                else await self._ctx.send(text)
        except Exception:
            msg = await self._ctx.send(text)
        _boards[self.channel_key] = {"message": msg, "match_id": m.id}
        return ("🗺️ Auto-update board ON — this message refreshes on every "
                "change" + (" (use the arrows to pan)." if engaged else "."))

    async def post_scene_image(self, m, pov) -> str:
        """Render the match's graphics scene to a PNG and post it as an
        attachment. Called by `!map image` via getattr (Discord-only — other
        surfaces lack this method). Respects the resolved POV + the channel's
        viewport window; returns a short status line (the image is the
        payload). Reports a clean message if Pillow isn't installed."""
        try:
            from sprite_render import render_match_png
        except Exception:
            return ("❌ Graphics rendering needs Pillow on the bot host "
                    "(`pip install Pillow`).")
        mode = str(m.rules.get("viewport_mode", "auto"))
        enabled = mode != "off"
        viewport = m.resolve_viewport(self.channel_key, enabled=enabled)
        try:
            import asyncio
            data = await asyncio.to_thread(
                render_match_png, m, _get_sprite_loader(),
                pov_team=pov, viewport=viewport)
        except RuntimeError as e:
            return f"❌ {e}"
        except Exception as e:
            return f"❌ Could not render the scene image: {e}"
        import io
        try:
            await self._ctx.send(
                file=discord.File(io.BytesIO(data), filename=f"{m.id}.png"))
        except Exception as e:
            return f"❌ Could not post the image: {e}"
        return ""  # the attachment is the reply

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
    supports_color = True
    viewport_capable = True

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
        for chunk in _split_for_discord(message):
            await self._channel.send(chunk)


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
        if _boards:
            mid = self._mgr.get_active_for_channel(ictx.channel_key)
            if mid:
                await _refresh_boards_for_match(self._mgr, mid)
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
# ----------------------------------------------------------------------
# Auto-update boards (#24) + pan buttons (#110, Discord surface)
# ----------------------------------------------------------------------
# A "board" is a self-refreshing map message: one per channel, edited in
# place after every state-changing command (and on a pan-button click)
# instead of spamming a fresh map. Runtime only — the Discord Message
# handles can't be serialized, so boards don't survive a restart (re-issue
# `!map autoupdate on`). The render itself (POV + viewport + legend) is the
# same surface-agnostic render_ascii the `!map` command uses; only the
# message lifecycle lives here, which is why it can't be harness-tested.

# channel_key -> {"message": discord.Message, "match_id": str}
_boards: Dict[str, Dict[str, Any]] = {}


# --- Graphics image rendering (the `!map image` surface hook) ---------------
# render_scene -> PNG bytes via sprite_render (Pillow). The loader caches PNGs
# from the sprites/ folder; we lazily build it once and reuse it. Importing
# sprite_render is deferred so a Discord deployment without Pillow still loads
# (the hook reports graphics as unavailable instead of crashing the adapter).
_sprite_loader = None


def _get_sprite_loader():
    global _sprite_loader
    if _sprite_loader is None:
        from sprite_render import SpriteLoader
        _sprite_loader = SpriteLoader()
    return _sprite_loader


def _board_render(m, channel_key: str) -> Tuple[str, bool]:
    """(message text, viewport_engaged) for a channel's board: the same
    POV + viewport + legend render as `!map`, with a windowed header."""
    pov = m.channel_pov(channel_key)
    mode = str(m.rules.get("viewport_mode", "auto"))
    enabled = mode != "off"   # Discord is viewport_capable; auto => on
    viewport = m.resolve_viewport(channel_key, enabled=enabled)
    legend = bool(getattr(m, "map_legend_enabled", False))
    colorize = bool(getattr(m, "color_enabled", True))
    body = m.render_ascii(pov, colorize=colorize, viewport=viewport,
                          legend=legend)
    fence = "ansi" if colorize else ""
    header = ""
    if viewport:
        vx, vy, vw, vh = viewport
        header = (f"🗺️ viewport ({vx},{vy})–({vx + vw - 1},{vy + vh - 1}) "
                  f"of {m.grid_width}×{m.grid_height}\n")
    return f"{header}```{fence}\n{body}\n```", bool(viewport)


def _pan_step(m, axis_dim: int) -> int:
    """Tiles per arrow-button click: the viewport_button_step rule, or half
    the window (0 = half-screen scroll) for that axis."""
    step = int(m.rules.get("viewport_button_step", 0))
    return step if step > 0 else max(1, axis_dim // 2)


class _PanView(discord.ui.View):
    """The 4 arrow buttons under an auto-update board. Each click pans that
    channel's viewport by the button step and edits the board in place."""

    def __init__(self, channel_key: str, mgr: MatchManager):
        super().__init__(timeout=None)
        self.channel_key = channel_key
        self._mgr = mgr

    async def _pan(self, interaction, dx: int, dy: int):
        mid = self._mgr.get_active_for_channel(self.channel_key)
        m = self._mgr.get(mid) if mid else None
        if m is None or not m.viewport_engaged():
            try:
                await interaction.response.defer()
            except Exception:
                pass
            return
        vw, vh = m._viewport_dims()
        m.pan_view(self.channel_key,
                   dx * _pan_step(m, vw), dy * _pan_step(m, vh))
        text, _ = _board_render(m, self.channel_key)
        entry = _boards.get(self.channel_key)
        if entry is not None:
            entry["message"] = interaction.message
        try:
            await interaction.response.edit_message(content=text, view=self)
        except Exception:
            pass

    @discord.ui.button(label="⬆", style=discord.ButtonStyle.secondary)
    async def up(self, interaction, button):
        await self._pan(interaction, 0, -1)

    @discord.ui.button(label="⬇", style=discord.ButtonStyle.secondary)
    async def down(self, interaction, button):
        await self._pan(interaction, 0, 1)

    @discord.ui.button(label="⬅", style=discord.ButtonStyle.secondary)
    async def left(self, interaction, button):
        await self._pan(interaction, -1, 0)

    @discord.ui.button(label="➡", style=discord.ButtonStyle.secondary)
    async def right(self, interaction, button):
        await self._pan(interaction, 1, 0)


async def _refresh_boards_for_match(mgr: MatchManager, match_id: str) -> None:
    """Edit every board bound to `match_id` in place (after a state change).
    Best-effort: a failed edit (deleted message, perms) drops that board."""
    for ck, entry in list(_boards.items()):
        if entry.get("match_id") != match_id:
            continue
        m = mgr.get(match_id)
        if m is None:
            _boards.pop(ck, None)
            continue
        text, engaged = _board_render(m, ck)
        view = _PanView(ck, mgr) if engaged else None
        try:
            await entry["message"].edit(content=text, view=view)
        except Exception:
            _boards.pop(ck, None)


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
            if _boards:
                gid = getattr(ctx.guild, "id", "DM")
                ck = f"{gid}:{ctx.channel.id}"
                mid = mgr.get_active_for_channel(ck)
                if mid:
                    await _refresh_boards_for_match(mgr, mid)
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
        wrapper = DiscordCtxWrapper(ctx, mgr)
        await registry.run(bound_root, args, wrapper, mgr)
        # Auto-update boards (#24): after any command settles, re-render the
        # boards bound to this channel's active match (so a move/spawn/death
        # in one channel updates every channel watching the same match).
        if _boards:
            mid = mgr.get_active_for_channel(wrapper.channel_key)
            if mid:
                await _refresh_boards_for_match(mgr, mid)

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
