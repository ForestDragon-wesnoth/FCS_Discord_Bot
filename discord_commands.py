## discord_commands.py (thin Discord adapter)

# discord_commands.py
from typing import Any, List, Dict, Optional, Tuple
import discord
from discord.ext import commands
from logic import MatchManager, Workspaces
import vtt_commands
from vtt_commands import registry, run_approved_request
import re

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

    Code-fence aware: a map arrives as one ```ansi block, and a plain line
    split would leave the first chunk's fence unclosed and the next chunk's
    rows unfenced (both render as garbage on Discord). When a chunk ends
    INSIDE a fence it is closed, and the next chunk reopens it with the same
    opener line (so `ansi` coloring carries over). Room for the closing
    fence is reserved while packing a fenced chunk, and a chunk never ends
    on a bare opener (that would post an empty block).

    A single line longer than the space available (no newline to break on)
    is the one case we can't honor cleanly — it's hard-split into
    limit-sized pieces as a last resort. Returns at least one chunk (the
    message unchanged when it already fits, including the empty string)."""
    if len(message) <= limit:
        return [message]
    close = "\n```"
    chunks: List[str] = []
    # None = no chunk in progress. Distinct from "" so an EMPTY line at the
    # start of a chunk is kept rather than silently swallowed.
    cur: Optional[str] = None
    opener: Optional[str] = None      # the ``` line of the fence we're inside

    def _has_content(c: Optional[str]) -> bool:
        # A chunk holding only the re-opened fence carries nothing to flush.
        return c is not None and c != opener

    def _flush(c: str) -> None:
        if opener is not None and c.endswith("\n" + opener):
            # Don't strand an empty block: the opener moves to the next
            # chunk along with the content it introduces.
            chunks.append(c[:-(len(opener) + 1)])
        else:
            chunks.append(c + (close if opener is not None else ""))

    for line in message.split("\n"):
        is_fence = line.lstrip().startswith("```")
        if is_fence and opener is not None and cur == opener:
            # Closing a block whose content all went out already (after a
            # hard split): the re-opened fence would post an empty block.
            cur, opener = None, None
            continue
        # Inside a fence, every content line must leave room for the close;
        # the closing fence line itself IS the close, so it needs none.
        reserve = len(close) if (opener is not None and not is_fence) else 0
        # What a fresh chunk would start with before this line.
        base = len(opener) + 1 if opener is not None else 0
        if base + len(line) + reserve > limit:
            # Oversized single line: flush whatever's buffered (closing an
            # open fence), then hard-split the line itself.
            if _has_content(cur):
                _flush(cur)
            for i in range(0, len(line), limit):
                chunks.append(line[i:i + limit])
            if is_fence:
                opener = None if opener is not None else line.lstrip()
            # Still inside a fence: the next chunk re-opens it.
            cur = opener
            continue
        if _has_content(cur) and len(cur) + 1 + len(line) + reserve > limit:
            _flush(cur)
            if is_fence and opener is not None:
                # This line WAS the close, and the flush already closed the
                # block — don't re-open it just to post an empty one.
                cur, opener = None, None
                continue
            cur = opener
        cur = line if cur is None else cur + "\n" + line
        if is_fence:
            opener = None if opener is not None else line.lstrip()
    if cur is not None:
        chunks.append(cur)
    return chunks

def _is_comment_or_blank(line: str) -> bool:
    s = (line or "").strip()
    return not s or s.startswith("#")

def _command_lines(content: str, prefix: str) -> List[str]:
    """The commands of a message, one per line (blank and `#` lines
    dropped), except that the lines after a `template import` line that
    don't start with the prefix continue it (its JSON spans lines), and so
    do those after a line with a quote still open (a multi-line body), as
    in the GUI."""
    out: List[str] = []
    for ln in content.splitlines():
        if _is_comment_or_blank(ln):
            continue
        s = ln.strip()
        if (out and not (prefix and s.startswith(prefix))
                and re.match(r"template\s+import\s",
                             _strip_prefix(out[-1].split("\n", 1)[0], prefix),
                             re.IGNORECASE)):
            out[-1] += "\n" + s
        elif (out and not (prefix and s.startswith(prefix))
                and vtt_commands.quote_unclosed(out[-1])):
            # A quoted body typed over several lines: one command, its line
            # breaks the `\n` a body reads (indentation kept).
            out[-1] += "\\n" + ln.rstrip()
        else:
            out.append(ln)
    return out


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
        parts = vtt_commands.split_command_line(s)
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
        wrapper = DiscordCtxWrapper(ctx, mgr)
        try:
            await registry.run(root, args, wrapper, mgr)
        finally:
            await wrapper.flush_dropped()
        return True
    except ValueError as e:
        _dbg(ctx, batch_parse_error=str(e), line=line)
        await ctx.send(f"❌ Parse error in line: `{line}`\n→ {e}")
        return False
    except Exception as e:
        _dbg(ctx, batch_unexpected_error=str(e), line=line)
        await ctx.send(f"❌ Error executing line: `{line}`\n→ {e}")
        return False


def _is_guild_admin(user) -> bool:
    """True iff `user` is a guild Member holding the Administrator
    permission (server-wide commands, see vtt_commands.ctx_is_admin)."""
    perms = getattr(user, "guild_permissions", None)
    return bool(getattr(perms, "administrator", False))


def _is_bot_owner(user, mgr) -> bool:
    """True iff `user` is among the bot owner ids read at startup (the
    Discord application's owner, or its team's members)."""
    owners = getattr(getattr(mgr, "workspaces", None), "owner_ids", None) or ()
    return str(getattr(user, "id", "")) in owners


# Every server has its own workspace (systems, matches, saves); a DM has no
# server to hold one, so commands there are refused with this reply.
DM_REFUSAL = ("❌ This bot only works in servers: each server keeps its own "
              "game systems, matches and saves. To play alone, create a "
              "server just for yourself (it takes a few seconds) and invite "
              "the bot there.")


class DiscordCtxWrapper:
    # Discord renders ANSI colors inside ```ansi code blocks, so the
    # colorized map renderer is enabled for this surface.
    supports_color = True
    # Users are numeric ids: a typed name in `!host add` / `user=` can
    # never match one, so those commands want a mention or an id.
    numeric_user_ids = True
    # Discord messages are narrow, so the map viewport engages in 'auto'
    # mode on large maps (pan with `!map pan` / the arrow buttons).
    viewport_capable = True
    # Discord's message cap; the map reply drops its coordinate rulers rather
    # than split a map across two messages.
    message_limit = DISCORD_MAX_CONTENT

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
        # Server-wide commands (!system edits, !store, !run, ...) need the
        # guild Administrator permission — see vtt_commands.ctx_is_admin.
        self.is_admin = _is_guild_admin(author)
        self.is_bot_owner = _is_bot_owner(author, mgr)
        self._sent = 0
        self._dropped = 0

    def _reply_cap(self, rule: str = "reply_message_cap",
                   default: int = 8) -> int:
        """A message-cap rule of this channel's match (else of the server's
        default system)."""
        mgr = self._mgr
        try:
            mid = mgr.get_active_for_channel(self.channel_key) if mgr else None
            m = mgr.matches.get(mid) if mid else None
            if m is not None:
                return max(1, int(m.rules.get(rule, default)))
            s = mgr.systems.get(mgr.default_system_name) if mgr else None
            if s is not None:
                return max(1, int(mgr.system_value(s, rule)))
        except Exception:
            pass
        return default

    async def send(self, message: str):
        # Split over-long output at line boundaries so we never trip
        # Discord's content-length cap (see _split_for_discord). Discord also
        # rejects an EMPTY / whitespace-only message (API error 50006), so a
        # handler whose output happened to be empty must not crash the send.
        # One reply takes at most reply_message_cap messages: the server's
        # command lock is held while they go out.
        chunks = [c for c in _split_for_discord(message) if c.strip()]
        cap = self._reply_cap()
        if len(chunks) > cap:
            cut = sum(len(c) for c in chunks[cap:])
            chunks = chunks[:cap] + [
                f"✂️ … {cut} more characters left out (reply_message_cap "
                f"{cap}): narrow the command (a viewport, a page, fewer "
                f"dice)."]
        # And one command at most command_message_cap messages in all (a
        # sweep, batch or macro posts one reply per line): the rest are
        # counted and named in one note, sent once the command ends.
        total = self._reply_cap("command_message_cap", 20)
        for chunk in chunks:
            if self._sent >= total:
                self._dropped += 1
                continue
            self._sent += 1
            await self._ctx.send(chunk)

    async def flush_dropped(self) -> None:
        """The note for the messages command_message_cap left out."""
        if self._dropped:
            n, self._dropped = self._dropped, 0
            await self._ctx.send(
                f"✂️ … {n} more message(s) of this command left out "
                f"(command_message_cap {self._reply_cap('command_message_cap', 20)}).")

    async def set_autoupdate(self, m, on: bool) -> str:
        """Turn this channel's self-refreshing map board on/off. Posts the
        board message (with pan buttons when the viewport is engaged) and
        registers it for post-command refresh; returns a status line. Called
        by the `!map autoupdate` handler via getattr (Discord-only — other
        surfaces lack this method and report the feature as unavailable)."""
        if not on:
            _boards.pop(self.channel_key, None)
            return "🗺️ Auto-update board OFF for this channel."
        import io
        msg = None
        engaged = False
        # Image board when the match's render mode is `image` (and Pillow is
        # available); otherwise the ASCII board. Falls back to text on failure.
        if getattr(m, "render_mode", "text") == "image":
            try:
                header, data, engaged = await _board_image(m, self.channel_key, self._mgr)
                view = _board_view(m, self.channel_key, self._mgr, engaged)
                file = discord.File(io.BytesIO(data), filename=f"{m.id}.png")
                msg = await self._ctx.send(header or "🗺️", file=file, view=view) \
                    if view else await self._ctx.send(header or "🗺️", file=file)
            except Exception:
                msg = None
        if msg is None:
            text, engaged = _board_render(m, self.channel_key)
            view = _board_view(m, self.channel_key, self._mgr, engaged)
            try:
                msg = await self._ctx.send(text, view=view) if view \
                    else await self._ctx.send(text)
            except Exception:
                msg = await self._ctx.send(text)
        _boards[self.channel_key] = {"message": msg, "match_id": m.id,
                                     "workspace": getattr(self._mgr, "guild_key", None)}
        kind = "image" if getattr(m, "render_mode", "text") == "image" else "map"
        return (f"🗺️ Auto-update {kind} board ON — this message refreshes on "
                "every change" + (" (use the arrows to pan)." if engaged else "."))

    async def retire_server_boards(self, mgr) -> None:
        """Stop every auto-update board of this server (`!server wipe`: the
        matches they show are gone)."""
        key = getattr(mgr, "guild_key", None)
        for ck, entry in list(_boards.items()):
            if entry.get("workspace") == key:
                await _retire_board(ck, entry, entry.get("match_id", "?"))

    async def post_scene_image(self, m, pov, highlights=None,
                               hidden=None, coords=None, legend=None,
                               mp=None) -> str:
        """Render the match's graphics scene to a PNG and post it as an
        attachment. Called by `!map image` via getattr (Discord-only — other
        surfaces lack this method). Respects the resolved POV + the channel's
        viewport window; returns a short status line (the image is the
        payload). Reports a clean message if Pillow isn't installed."""
        try:
            from sprite_render import scene_for_png, render_scene_png
        except Exception:
            return ("❌ Graphics rendering needs Pillow on the bot host "
                    "(`pip install Pillow`).")
        mode = str(m.rules.get("viewport_mode", "auto"))
        enabled = mode != "off"
        mp = mp or m.channel_map(self.channel_key, pov)
        viewport = m.resolve_viewport(self.channel_key, enabled=enabled, mp=mp)
        try:
            import asyncio
            # Build the scene HERE (it reads the match) and only draw pixels
            # in the worker thread — see sprite_render.scene_for_png.
            scene, cell = scene_for_png(m, pov_team=pov, viewport=viewport,
                                        highlights=highlights,
                                        hidden_layers=hidden, coords=coords,
                                        legend=(m.map_legend_enabled
                                                if legend is None else legend),
                                        mp=mp)
            data = await asyncio.to_thread(
                render_scene_png, scene, _get_sprite_loader(self._mgr), cell)
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
        cmd = _shown_cmd(req["name"], req["args"])
        text = (
            f"🕓 **{req['user_name']}** requests `{cmd}` "
            f"(id `{req['id']}`). A host can approve or deny."
        )
        try:
            view = _ApprovalView(req, self._mgr, self.channel_key)
            view.message = await self._ctx.send(text, view=view)
        except Exception:
            # No UI support (older discord.py) — text prompt + commands.
            await self._ctx.send(
                text + f"\nUse `!approve {req['id']}` or `!deny {req['id']}`."
            )


    async def offer_resume(self, m, entry: dict):
        """A host's command held on a paused match (pause_affects_hosts):
        ask to unpause, with Resume & run / Cancel buttons. Called by the
        dispatcher (CommandRegistry._hold_command)."""
        cmd = _shown_cmd(entry["name"], entry["args"])
        n = len(m.held_commands)
        text = (f"⏸ **{m.name}** is paused — `{cmd}` is held"
                + (f" ({n} commands waiting)" if n > 1 else "")
                + ". Resume the match to run it?")
        try:
            view = _ResumeView(m.id, entry, self._mgr)
            view.message = await self._ctx.send(text, view=view)
        except Exception:
            await self._ctx.send(
                text + " `!match resume` runs it; `!match resume drop` "
                       "discards held commands.")


def _shown_cmd(name: str, args: list) -> str:
    """A command as a prompt shows it, cut to fit one Discord message
    (2000 characters, the prompt's own text included): a near-limit command
    queued a request whose prompt Discord refused."""
    cmd = "!" + name + (" " + " ".join(args) if args else "")
    return cmd if len(cmd) <= 1500 else cmd[:1499] + "…"


class _InteractionCtx(DiscordCtxWrapper):
    """ReplyContext built from a button-click interaction, so an approved
    command runs with the CLICKER's identity (a host) against the
    interaction's channel. It is the typed commands' adapter on the
    interaction's channel, so a command run from Approve / Resume keeps the
    Discord surface: image maps, auto-update boards, the message limit,
    approval and resume prompts."""

    def __init__(self, interaction, mgr=None):
        guild = getattr(interaction, "guild", None)
        gid = getattr(guild, "id", "DM")
        self._mgr = mgr
        # channel.send takes what a command context's send takes (content,
        # file=, view=), so the wrapper's hooks post through it.
        self._ctx = interaction.channel
        self.channel_key = f"{gid}:{interaction.channel_id}"
        user = interaction.user
        self.is_admin = _is_guild_admin(user)
        self.is_bot_owner = _is_bot_owner(user, mgr)
        self.user_id = str(getattr(user, "id", "")) or "unknown"
        self.user_name = (
            getattr(user, "display_name", None)
            or getattr(user, "name", None)
            or self.user_id
        )
        self.cli_mutable = False
        self._sent = 0
        self._dropped = 0


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
        # The posted message, set by send_approval, so on_timeout can edit it.
        self.message = None

    async def on_timeout(self):
        """The buttons stop responding after `timeout`, but they used to stay
        clickable-looking ("This interaction failed" on click). Grey them out
        and say how to resolve the request, which is still queued: the text
        `!approve` / `!deny` commands have no timeout."""
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            rid = self._req.get("id")
            await self.message.edit(
                content=(self.message.content or "")
                + f"\n⌛ Buttons expired — use `!approve {rid}` or "
                  f"`!deny {rid}`.",
                view=self)
        except Exception:
            pass

    def _match(self):
        if self._mgr is None:
            return None
        # Resolve against the match the request was QUEUED on (stored on the
        # request), NOT the channel's currently-active match — a host may have
        # switched the active match between queueing and clicking Approve, and
        # request ids are per-match sequential, so the active match could hold a
        # DIFFERENT request with the same id. Fall back to the channel's active
        # match for legacy requests lacking match_id.
        mid = self._req.get("match_id")
        if mid is None:
            mid = self._mgr.active_by_channel.get(self._channel_key)
        return self._mgr.matches.get(mid) if mid is not None else None

    def _pop_own(self, m):
        """Pop THIS view's request, or None if it's already gone. Checked by
        identity, not just id: ids are short per-match counters, so a button
        that outlived its request (resolved via text `!approve`, or a match
        reloaded from disk) must not resolve a DIFFERENT request that now
        holds the same id."""
        rid = self._req.get("id")
        if m is None or m.pending_requests.get(rid) is not self._req:
            return None
        return m.pop_pending_request(rid)

    async def _require_host(self, interaction) -> bool:
        m = self._match()
        ictx = _InteractionCtx(interaction, self._mgr)
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
        req = self._pop_own(m)
        if req is None:
            await interaction.response.send_message(
                "Already resolved.", ephemeral=True
            )
            return self._finish()
        await interaction.response.defer()
        ictx = _InteractionCtx(interaction, self._mgr)
        cmd = _shown_cmd(req["name"], req["args"])
        await ictx.send(
            f"✅ {ictx.user_name} approved `{cmd}` (by {req['user_name']})."
        )
        # Re-dispatch against the request's OWN match and channel, not whatever
        # is active where the button was clicked — and without permanently
        # re-pointing that channel (see vtt_commands.run_approved_request).
        try:
            await run_approved_request(req, ictx, self._mgr)
        finally:
            await ictx.flush_dropped()
        if _boards:
            mid = req.get("match_id") or \
                self._mgr.get_active_for_channel(ictx.channel_key)
            if mid:
                await _refresh_boards_for_match(self._mgr, mid)
        await self._disable(interaction)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger)
    async def deny(self, interaction, button):
        if not await self._require_host(interaction):
            return
        m = self._match()
        req = self._pop_own(m)
        if req is None:
            await interaction.response.send_message(
                "Already resolved.", ephemeral=True
            )
            return self._finish()
        cmd = _shown_cmd(req["name"], req["args"])
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

class _ResumeView(discord.ui.View):
    """Resume & run / Cancel for one command held on a paused match.
    Resume & run = `!match resume` from the clicker (a host): it unpauses
    and runs EVERY held command in order, this one included. Cancel drops
    only this held command and leaves the match paused."""

    def __init__(self, match_id: str, entry: dict, mgr, timeout: float = 600.0):
        super().__init__(timeout=timeout)
        self._mid = match_id
        self._entry = entry
        self._mgr = mgr
        self.message = None

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(
                content=(self.message.content or "")
                + "\n⌛ Buttons expired — use `!match resume` (or `!match "
                  "resume drop`).",
                view=self)
        except Exception:
            pass

    def _match(self):
        return self._mgr.matches.get(self._mid) if self._mgr else None

    async def _require_host(self, interaction) -> bool:
        m = self._match()
        ictx = _InteractionCtx(interaction, self._mgr)
        if m is None or (m.owner is not None and not m.is_host(ictx.user_id)):
            await interaction.response.send_message(
                "❌ Only a host can resume the match.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Resume & run", style=discord.ButtonStyle.success)
    async def resume(self, interaction, button):
        if not await self._require_host(interaction):
            return
        m = self._match()
        if m is None or not m.paused:
            await interaction.response.send_message(
                "The match isn't paused any more.", ephemeral=True)
            return await self._disable(interaction)
        await interaction.response.defer()
        ictx = _InteractionCtx(interaction, self._mgr)
        # Point the run at this match even if the clicked channel shows
        # another one, then put the channel back (run_approved_request's rule).
        # A click is not a typed command, so it isn't the clicker's `!again`.
        depth = vtt_commands._RUN_DEPTH.set(1)
        try:
            # The lock before the re-point (see run_approved_request).
            async with vtt_commands.holding_command_lock(self._mgr, ictx):
                with vtt_commands._channel_pointed_at(
                        self._mgr, ictx.channel_key, self._mid):
                    await registry.run("match", ["resume"], ictx, self._mgr)
        finally:
            vtt_commands._RUN_DEPTH.reset(depth)
            await ictx.flush_dropped()
        if _boards:
            await _refresh_boards_for_match(self._mgr, self._mid)
        await self._disable(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if not await self._require_host(interaction):
            return
        m = self._match()
        held = m.held_commands if m is not None else []
        # By identity: the same text may be held twice.
        for i, h in enumerate(held):
            if h is self._entry:
                del held[i]
                cmd = _shown_cmd(h["name"], h["args"])
                await interaction.response.send_message(
                    f"🚫 Dropped held `{cmd}`; the match stays paused.")
                return await self._disable(interaction)
        await interaction.response.send_message(
            "That command isn't held any more.", ephemeral=True)
        await self._disable(interaction)

    async def _disable(self, interaction):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
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

# channel_key -> {"message": discord.Message, "match_id": str,
#                 "workspace": the server's workspace key}
_boards: Dict[str, Dict[str, Any]] = {}


# --- Graphics image rendering (the `!map image` surface hook) ---------------
# render_scene -> PNG bytes via sprite_render (Pillow). The loader caches PNGs
# from the sprites/ folder; we lazily build it once and reuse it. Importing
# sprite_render is deferred so a Discord deployment without Pillow still loads
# (the hook reports graphics as unavailable instead of crashing the adapter).
# One loader per server: its own data/<guild>/sprites/ folder is searched
# before the shared sprites/ folder.
_sprite_loaders: Dict[Any, Any] = {}


def _get_sprite_loader(mgr=None):
    key = getattr(mgr, "guild_key", None)
    if key not in _sprite_loaders:
        from sprite_render import SpriteLoader
        from storage import storage_of
        st = storage_of(mgr)
        _sprite_loaders[key] = SpriteLoader(
            first=st.sprites_dir(key) if st is not None and key else None)
    return _sprite_loaders[key]


def _board_render(m, channel_key: str) -> Tuple[str, bool]:
    """(message text, viewport_engaged) for a channel's board: the same
    POV + viewport + legend render as `!map`, with a windowed header.

    A board is ONE message edited in place, so unlike a `!map` reply it
    can't be split across messages — and an edit over Discord's content cap
    fails, which used to silently drop the board. ANSI color costs ~9 chars
    per colored cell, so a default 28x28 window with a dozen or so team-
    colored units already crosses 2000. Degrade instead: drop color, then
    the legend; if even the bare map is too big, say so (the viewport rules
    set the window size)."""
    pov = m.channel_pov(channel_key)
    mode = str(m.rules.get("viewport_mode", "auto"))
    enabled = mode != "off"   # Discord is viewport_capable; auto => on
    mp = _board_map(m, channel_key)
    viewport = m.resolve_viewport(channel_key, enabled=enabled, mp=mp)
    if m.map_blank_text(mp, pov) is not None:
        viewport = None             # a blank board has nothing to pan
    legend = bool(getattr(m, "map_legend_enabled", False))
    colorize = bool(getattr(m, "color_enabled", True))
    header = _board_header(m, mp, viewport, pov) + ("\n" if len(m.maps) > 1
                                              or viewport else "")
    coords = m.coords_on()
    # Richest first; each fallback drops one optional layer (the coordinate
    # rulers first: two-plus lines and a margin on every row).
    attempts = [(colorize, legend, coords), (colorize, legend, False),
                (False, legend, False), (False, False, False)]
    text = ""
    for col, leg, crd in dict.fromkeys(attempts):
        body = m.render_ascii(pov, colorize=col, viewport=viewport, legend=leg,
                              coords=crd, mp=mp)
        text = f"{header}```{'ansi' if col else ''}\n{body}\n```"
        if len(text) <= DISCORD_MAX_CONTENT:
            if (col, leg, crd) != attempts[0]:
                dropped = [n for n, was, now in (("coordinates", coords, crd),
                                                 ("color", colorize, col),
                                                 ("legend", legend, leg))
                           if was and not now]
                text = (f"(board too large for one Discord message — "
                        f"showing without {' and '.join(dropped)})\n" + text)
                if len(text) > DISCORD_MAX_CONTENT:
                    continue
            return text, bool(viewport)
    return (f"⚠️ This map board is {len(text)} characters, over Discord's "
            f"{DISCORD_MAX_CONTENT}-character message limit even without "
            f"coordinates, color or legend. Lower the `viewport_width` / `viewport_height` "
            f"rules to shrink the window."), bool(viewport)


async def _board_image(m, channel_key: str, mgr=None):
    """(header text, PNG bytes, viewport_engaged) for an IMAGE board: the same
    POV + viewport as the text board, rendered to a PNG via sprite_render.
    Raises if Pillow is unavailable (caller falls back to a text board)."""
    import asyncio
    from sprite_render import scene_for_png, render_scene_png
    pov = m.channel_pov(channel_key)
    vmode = str(m.rules.get("viewport_mode", "auto"))
    enabled = vmode != "off"
    mp = _board_map(m, channel_key)
    viewport = m.resolve_viewport(channel_key, enabled=enabled, mp=mp)
    if m.map_blank_text(mp, pov) is not None:
        viewport = None             # a blank board has nothing to pan
    # Scene on the event loop (reads the match), pixels in a worker thread.
    scene, cell = scene_for_png(m, pov_team=pov, viewport=viewport,
                                legend=bool(m.map_legend_enabled), mp=mp)
    data = await asyncio.to_thread(
        render_scene_png, scene, _get_sprite_loader(mgr), cell)
    return _board_header(m, mp, viewport, pov), data, bool(viewport)


def _board_header(m, mp, viewport, pov=None) -> str:
    """A board's header line: the map's title on a match with several (its
    name and place among the maps the view knows of, or the unnamed label
    of a map it hasn't seen), and the viewport window when one is engaged
    ("" when neither applies)."""
    bits = []
    if len(m.maps) > 1:
        bits.append(m.map_heading(mp, pov))
    if viewport and m.map_blank_text(mp, pov) is None:
        vx, vy, vw, vh = viewport
        bits.append(f"viewport ({vx},{vy})–({vx + vw - 1},{vy + vh - 1}) "
                    f"of {mp.grid_width}×{mp.grid_height}")
    return ("🗺️ " + " · ".join(bits)) if bits else ""


async def _apply_board(message, m, channel_key: str, mgr: MatchManager) -> bool:
    """Edit an existing board `message` to the current view, honoring the
    match's render_mode (`image` → a PNG attachment, else the ASCII block).
    Returns whether the viewport is engaged. Image mode falls back to text if
    rendering fails (e.g. Pillow missing). Raises only on the edit itself."""
    import io
    if getattr(m, "render_mode", "text") == "image":
        try:
            header, data, engaged = await _board_image(m, channel_key, mgr)
            view = _board_view(m, channel_key, mgr, engaged)
            await message.edit(
                content=(header or None),
                attachments=[discord.File(io.BytesIO(data),
                                          filename=f"{m.id}.png")],
                view=view)
            return engaged
        except Exception:
            pass  # fall through to a text board
    text, engaged = _board_render(m, channel_key)
    view = _board_view(m, channel_key, mgr, engaged)
    await message.edit(content=text, attachments=[], view=view)
    return engaged


def _pan_step(m, axis_dim: int) -> int:
    """Tiles per arrow-button click: the viewport_button_step rule, or half
    the window (0 = half-screen scroll) for that axis."""
    step = int(m.rules.get("viewport_button_step", 0))
    return step if step > 0 else max(1, axis_dim // 2)


def _board_map(m, channel_key: str):
    """The map a channel's board shows: the one a viewer browsed to with
    the board's map buttons, until the next turn change (any team's) or
    until the view may no longer list it; else the channel's map (`!map
    use`, else the map the board follows). Browsing is the board's own
    display state: nothing else reads it."""
    entry = _boards.get(channel_key)
    pov = m.channel_pov(channel_key)
    browse = (entry or {}).get("browse")
    if browse and browse.get("match") == m.id \
            and browse.get("turn") == m.turns_elapsed:
        mp = m.maps.get(browse.get("map"))
        if mp is not None and mp in m.listed_maps(pov):
            return mp
    if entry is not None:
        entry.pop("browse", None)
    return m.channel_map(channel_key, pov)


def _board_view(m, channel_key: str, mgr: MatchManager, engaged: bool):
    """The buttons under a board: the pan arrows while the viewport is
    engaged, and map browsing while the channel's view knows of several
    maps (None when neither applies)."""
    maps = m.listed_maps(m.channel_pov(channel_key))
    if not engaged and len(maps) < 2:
        return None
    return _BoardView(channel_key, mgr, engaged, len(maps))


# More maps than this get a select menu instead of the ◀ ▶ buttons.
_BOARD_MAP_BUTTONS_MAX = 5
# Discord's limit on a select menu's options.
_SELECT_MAX = 25


class _BoardView(discord.ui.View):
    """The controls under an auto-update board: 4 pan arrows (each click
    pans that channel's viewport on the shown map by the button step) and,
    on a match with several maps the channel's view knows of, ◀ ▶ buttons
    or a select menu that show another map until the next turn change.
    Each click edits the board in place."""

    def __init__(self, channel_key: str, mgr: MatchManager, pan: bool,
                 n_maps: int):
        super().__init__(timeout=None)
        self.channel_key = channel_key
        self._mgr = mgr
        if pan:
            for label, dx, dy in (("⬆", 0, -1), ("⬇", 0, 1), ("⬅", -1, 0),
                                  ("➡", 1, 0)):
                btn = discord.ui.Button(label=label, row=0,
                                        style=discord.ButtonStyle.secondary)
                btn.callback = self._pan_cb(dx, dy)
                self.add_item(btn)
        if n_maps > _BOARD_MAP_BUTTONS_MAX:
            m = self._match()
            if m is not None:
                pov = m.channel_pov(channel_key)
                listed = m.listed_maps(pov)
                # A select menu holds 25 options (Discord's limit): past
                # that it lists the 25 around the shown map, and ◀ ▶
                # buttons step on from its ends.
                start = 0
                if len(listed) > _SELECT_MAX:
                    cur = _board_map(m, channel_key)
                    ci = listed.index(cur) if cur in listed else 0
                    start = max(0, min(ci - _SELECT_MAX // 2,
                                       len(listed) - _SELECT_MAX))
                opts = [discord.SelectOption(
                            label=f"{i + 1}. {m.map_label(mp, pov)}".replace(
                                "`", "")[:100], value=str(i))
                        for i, mp in enumerate(listed)
                        if start <= i < start + _SELECT_MAX]
                sel = discord.ui.Select(placeholder="Show another map",
                                        options=opts, row=1)

                async def _picked(interaction, sel=sel):
                    await self._browse(interaction, index=int(sel.values[0]))
                sel.callback = _picked
                self.add_item(sel)
                if len(listed) > _SELECT_MAX:
                    for label, step in (("◀ map", -1), ("map ▶", 1)):
                        btn = discord.ui.Button(
                            label=label, row=2,
                            style=discord.ButtonStyle.secondary)
                        btn.callback = self._browse_cb(step)
                        self.add_item(btn)
        elif n_maps > 1:
            for label, step in (("◀ map", -1), ("map ▶", 1)):
                btn = discord.ui.Button(label=label, row=1,
                                        style=discord.ButtonStyle.secondary)
                btn.callback = self._browse_cb(step)
                self.add_item(btn)

    def _match(self):
        entry = _boards.get(self.channel_key)
        mid = (entry or {}).get("match_id") or \
            self._mgr.get_active_for_channel(self.channel_key)
        return self._mgr.matches.get(mid) if mid else None

    def _pan_cb(self, dx: int, dy: int):
        async def _cb(interaction):
            await self._pan(interaction, dx, dy)
        return _cb

    def _browse_cb(self, step: int):
        async def _cb(interaction):
            await self._browse(interaction, step=step)
        return _cb

    async def _ack(self, interaction) -> None:
        try:
            await interaction.response.defer()
        except Exception:
            pass

    async def _usable_match(self, interaction):
        """The match the BOARD shows (the channel may have switched its
        active match since); None after retiring a board whose channel was
        unbound."""
        entry = _boards.get(self.channel_key)
        m = self._match()
        if m is not None and self.channel_key not in m.bound_channels:
            await self._ack(interaction)
            if entry is not None:
                entry["message"] = interaction.message
                await _retire_board(self.channel_key, entry, m.id)
            return None
        return m

    async def _redraw(self, interaction, m) -> None:
        entry = _boards.get(self.channel_key)
        if entry is not None:
            entry["message"] = interaction.message
        await self._ack(interaction)
        try:
            await _apply_board(interaction.message, m, self.channel_key,
                               self._mgr)
        except Exception:
            pass

    async def _pan(self, interaction, dx: int, dy: int):
        # Acknowledge first: Discord fails a click not answered within 3 s,
        # and the pan below waits for the command running on the server.
        await self._ack(interaction)
        m = await self._usable_match(interaction)
        if m is None:
            return
        # The map the board shows (a match with several maps has a camera
        # per map).
        mp = _board_map(m, self.channel_key)
        if not m.viewport_engaged(mp):
            await self._ack(interaction)
            return
        # A click isn't a command, but it changes the camera like one: it
        # waits for the command running on this server and is saved after.
        async with vtt_commands.holding_command_lock(self._mgr):
            vw, vh = m._viewport_dims(mp)
            m.pan_view(self.channel_key,
                       dx * _pan_step(m, vw), dy * _pan_step(m, vh), mp)
        await self._redraw(interaction, m)

    async def _browse(self, interaction, step: int = 0,
                      index: Optional[int] = None):
        m = await self._usable_match(interaction)
        if m is None:
            return
        entry = _boards.get(self.channel_key)
        listed = m.listed_maps(m.channel_pov(self.channel_key))
        if entry is None or not listed:
            await self._ack(interaction)
            return
        cur = _board_map(m, self.channel_key)
        if index is None:
            i = listed.index(cur) if cur in listed else 0
            index = (i + step) % len(listed)
        index = max(0, min(index, len(listed) - 1))
        # Browsing is display state on this board only (no undo step, no
        # journal entry); the next turn change returns it to its followed
        # map (_board_map).
        entry["browse"] = {"match": m.id, "map": listed[index].name,
                           "turn": m.turns_elapsed}
        await self._redraw(interaction, m)


async def _refresh_boards_for_match(mgr: MatchManager, match_id: str) -> None:
    """Edit every board bound to `match_id` in place (after a state change).
    Best-effort: a failed edit (deleted message, perms) drops that board."""
    for ck, entry in list(_boards.items()):
        # Match ids are per server: another server's board on a match with
        # the same id is not this match's board.
        if (entry.get("match_id") != match_id
                or entry.get("workspace") != getattr(mgr, "guild_key", None)):
            continue
        m = mgr.matches.get(match_id)
        if m is None:
            _boards.pop(ck, None)
            continue
        if ck not in m.bound_channels:
            # The channel was unbound (`!match unbind`, or an undo under
            # undo_channel_bindings_mode=revert). An unbound channel has no
            # POV, i.e. omniscient — refreshing would post the whole fogged
            # board into what was a team channel. Retire the board instead.
            await _retire_board(ck, entry, match_id)
            continue
        try:
            await _apply_board(entry["message"], m, ck, mgr)
        except Exception:
            _boards.pop(ck, None)


async def _retire_board(channel_key: str, entry: Dict[str, Any],
                        match_id: str) -> None:
    """Stop a board whose channel no longer belongs to its match: drop it
    from the registry and replace its content (and pan buttons) with a
    note, so nothing stale or unfiltered stays on screen."""
    _boards.pop(channel_key, None)
    try:
        await entry["message"].edit(
            content=(f"🗺️ Auto-update board stopped — this channel is no "
                     f"longer bound to `{match_id}`."),
            attachments=[], view=None)
    except Exception:
        pass


def wire_commands(bot: commands.Bot, workspaces: Workspaces):
    """Register every registry root as a discord.py command. Each command
    runs against the workspace (MatchManager) of the server it came from;
    DMs are refused (DM_REFUSAL)."""

    def _workspace(ctx) -> Optional[MatchManager]:
        guild = getattr(ctx, "guild", None)
        if guild is None:
            return None
        mgr = workspaces.get(guild.id)
        mgr.guild_name = str(getattr(guild, "name", "") or guild.id)
        return mgr

    async def _load_owners():
        # The bot owner: the Discord application's owner, or every member of
        # the team that owns it.
        try:
            app = await bot.application_info()
        except Exception as ex:  # noqa: BLE001 - startup must not die on it
            print(f"⚠️ Couldn't read the bot's owner ({ex}); `!owner` is "
                  f"refused to everyone until the next start.")
            return
        team = getattr(app, "team", None)
        if team is not None:
            ids = {str(mem.id) for mem in getattr(team, "members", [])}
        else:
            owner = getattr(app, "owner", None)
            ids = {str(owner.id)} if owner is not None else set()
        workspaces.owner_ids = ids

    bot.add_listener(_load_owners, "on_ready")

    async def _dispatch(ctx, bound_root: str):
        mgr = _workspace(ctx)
        if mgr is None:
            return await ctx.send(DM_REFUSAL)
        content = ctx.message.content or ""
        # --- BATCH MODE: multiple non-empty lines pasted in one message ---
        # (a `!template import`'s JSON lines stay with it: _command_lines)
        lines = _command_lines(content, getattr(ctx, "prefix", "") or "")
        if len(lines) == 1:
            content = lines[0]
        s = content.lstrip()
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
            args = vtt_commands.split_command_args(bound_root, s)
        except ValueError as e:
            _dbg(ctx, parse_error=str(e), raw_tail=s)
            await _dbg_chat(ctx, f"parse error: {e}")
            return await ctx.send(f"❌ Parse error: {e}")
    
        _dbg(ctx, final_args=args)
        await _dbg_chat(ctx, f"root={bound_root} args={args}")
        wrapper = DiscordCtxWrapper(ctx, mgr)
        try:
            await registry.run(bound_root, args, wrapper, mgr)
        finally:
            await wrapper.flush_dropped()
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

    # Aliases (`!alias add fb "ent hp ..."`) are per-match data, not
    # discord.py commands, so `!fb` used to die in discord.py's own lookup
    # (CommandNotFound — logged, never answered) and aliases only worked on
    # the CLI. Route an unknown command through the registry when it is an
    # alias on the channel's active match; anything else stays ignored so a
    # server's other `!`-prefixed bots don't get "unknown command" replies.
    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            name = getattr(ctx, "invoked_with", None) or ""
            guild = getattr(ctx, "guild", None)
            # peek: an unknown word must not create a workspace, and a DM
            # (no server) has no aliases.
            mgr = workspaces.peek(guild.id) if guild is not None else None
            mid = mgr.get_active_for_channel(f"{guild.id}:{ctx.channel.id}") \
                if mgr is not None else None
            m = mgr.matches.get(mid) if mid else None
            if name and m is not None and (name in m.aliases
                                           or name.lower() in m.aliases):
                await _dispatch(ctx, name)
            return
        # Keep discord.py's default reporting for everything else.
        import traceback
        traceback.print_exception(type(error), error, error.__traceback__)
