## vtt_commands.py (framework‑agnostic commands + registry)
# vtt_commands.py
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Protocol, Any, Tuple
from logic import MatchManager, Entity, VTTError, OutOfBounds, Occupied, NotFound, DuplicateId

# ---- Context abstraction -----------------------------------------------------
class ReplyContext(Protocol):
    channel_key: str  # unique per chat location (e.g., guild:channel). For CLI, just "CLI".
    async def send(self, message: str) -> None: ...

# ---- Command registry --------------------------------------------------------
Handler = Callable[[ReplyContext, List[str], MatchManager], Any]

class CommandRegistry:
    def __init__(self):
        self._handlers: Dict[str, Handler] = {}
        # help metadata: {root: {"usage": str, "desc": str, "subs": {sub: {"usage": str, "desc": str}}}}
        self._help: Dict[str, Dict[str, Any]] = {}
    def command(self, name: str, *, usage: Optional[str] = None, desc: Optional[str] = None):
        def deco(fn: Handler):
            self._handlers[name] = fn
            meta = self._help.setdefault(name, {"usage": None, "desc": None, "subs": {}})
            if usage:
                meta["usage"] = usage
            if desc:
                meta["desc"] = desc
            return fn
        return deco

    #usage/help for commands is partially automated instead of a hardcoded help menu, the usage/help info is included in each command definition, then it's pulled from that

    def annotate_sub(self, root: str, *subs: str, usage: str, desc: Optional[str] = None):
        meta = self._help.setdefault(root, {"usage": None, "desc": None, "subs": {}})
        for sub in subs:
            meta["subs"][sub] = {"usage": usage, "desc": (desc or "")}

    def help_for(self, path: List[str]) -> Tuple[str, str]:
        """
        Returns (title, text) for a given help path:
        [] => all commands
        [root] => command details + subcommands
        [root, sub] => specific subcommand
        """
        if not path:
            # All commands
            lines = ["**Commands**"]
            for root in sorted(self._handlers.keys()):
                m = self._help.get(root, {})
                usage = m.get("usage") or f"!{root}"
                desc = m.get("desc") or ""
                lines.append(f"`{usage}` — {desc}".rstrip())
            return ("Help", "\n".join(lines))
        root = path[0]
        if root not in self._handlers:
            return ("Help", f"Unknown command `{root}`. Try `!help`.")
        meta = self._help.get(root, {"subs": {}})
        if len(path) == 1:
            # Root details
            usage = meta.get("usage") or f"!{root}"
            desc = meta.get("desc") or ""
            lines = [f"**!{root}**", f"Usage: `{usage}`"]
            if desc:
                lines.append(desc)
            subs = meta.get("subs") or {}
            if subs:
                lines.append("\n**Subcommands**")
                for s, sm in sorted(subs.items()):
                    lines.append(f"- `{sm['usage']}` — {sm.get('desc','')}".rstrip())
            return (f"Help: {root}", "\n".join(lines))
        # Subcommand
        sub = path[1]
        sm = (meta.get("subs") or {}).get(sub)
        if not sm:
            return (f"Help: {root}", f"No help found for `{root} {sub}`.")
        lines = [f"**!{root} {sub}**", f"Usage: `{sm['usage']}`"]
        if sm.get("desc"):
            lines.append(sm["desc"])
        return (f"Help: {root} {sub}", "\n".join(lines))

    async def run(self, name: str, args: List[str], ctx: ReplyContext, mgr: MatchManager):
        h = self._handlers.get(name)
        if not h:
            await ctx.send(f"❓ Unknown command `{name}`")
            return
        try:
            result = await h(ctx, args, mgr)
            return result
        except VTTError as e:
            await ctx.send(f"❌ {e}")
        except Exception as e:
            await ctx.send(f"💥 Unexpected error: {e}")

registry = CommandRegistry()

# ---- Helpers ----------------------------------------------------------------

def _entity_line(e: Entity) -> str:
    # If max_hp wasn't set, many tables treat it as current hp initially
    max_hp = e.max_hp if getattr(e, "max_hp", None) is not None else e.hp
    return f"{e.name} ({e.id}): HP: {e.hp}/{max_hp} X,Y: {e.x},{e.y} facing {e.facing}"
    #TODO: add a way to dynamically define what variables are shown for entities!

def active_match(mgr: MatchManager, ctx: ReplyContext):
    mid = mgr.get_active_for_channel(ctx.channel_key)
    if not mid:
        raise NotFound("No active match for this channel. Use `!match use <id>`.")
    return mgr.get(mid)

#boilerplate code for returning if not enough arguments for a command/subcommand were sent
async def return_help_if_not_enough_args(
    ctx: ReplyContext,
    args: List[str],
    required: int,
    command: str,
    subcommand: str | None = None,
) -> bool:
    """
    Return True if help was sent because not enough args were given.
    """
    if len(args) < required:
        keys = [command] + ([subcommand] if subcommand else [])
        title, body = registry.help_for(keys)
        await ctx.send(f"**{title}**\n{body}")
        return True
    return False

# ---- Commands ----------------------------------------------------------------
@registry.command("match", usage="!match | !match new <id> <name> <w> <h> | !match use <id>", desc="List matches, create one, or switch the active match for this channel.")
async def match_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        pairs = mgr.list()
        if not pairs: return await ctx.send("No matches. `!match new <name> <w> <h>`")
        lines = [f"**{name}** — `{mid}`" for mid, name in pairs]
        return await ctx.send("Matches:\n" + "\n".join(lines))
    sub = args[0]
    if sub == "new":# and len(args) >= 5:
        if await return_help_if_not_enough_args(ctx, args, 5, "match", "new"):
            return
        match_id = args[1]; name = args[2]; w = int(args[3]); h = int(args[4])
        mid = mgr.create_match(match_id, name, w, h)
        return await ctx.send(f"Created **{name}** with id `{mid}`.")
    if sub == "use":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "match", "use"):
            return
        mid = args[1]
        mgr.set_active_for_channel(ctx.channel_key, mid)
        return await ctx.send(f"Channel using match `{mid}`.")
    if sub == "delete":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "match", "delete"):
            return
        mgr.delete_match(args[1])
        return await ctx.send(f"Deleted `{args[1]}`.")
    if sub == "rename":
        if await return_help_if_not_enough_args(ctx, args, 3, "match", "rename"):
            return
        mid = args[1]
        m = mgr.matches.get(mid)
        if not m:
            raise NotFound(f"Match '{mid}' not found.")
        m.name = " ".join(args[2:])
        return await ctx.send(f"Renamed match `{mid}`.")
    # Fallback: show help menu for the command if it's not properly typed
    title, body = registry.help_for(["match"])
    return await ctx.send(f"**{title}**\n{body}")

@registry.command("ent", usage="!ent <subcommand> ...", desc="Manage entities in the active match, lots of available sub-commands.")
async def ent_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    sub = args[0]

    m = active_match(ctx, mgr)
    if not args:
        # if no subcommand: just show the authoritative help for !ent
        title, body = registry.help_for(["ent"])
        return await ctx.send(f"**{title}**\n{body}")

    sub = args[0].lower()#makes the first arg lowercase, so "ADD", "aDD", etc. become "add"

    # add
    if sub == "add":# and len(args) >= 6:
        if await return_help_if_not_enough_args(ctx, args, 6, "ent", "add"):
            return
        eid, name, hp, x, y = args[1], args[2], int(args[3]), int(args[4]), int(args[5])
        init = int(args[6]) if len(args) >= 7 else None
        e = Entity(id=eid, name=name, hp=hp, x=x, y=y)
        e.spawn(m, x, y, initiative=init)
        return await ctx.send(f"Added `{name}` with id `{eid}` at ({x},{y}).")

    # --- info (single entity line) ---
    if sub == "info":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "info"):
            return
        return await ctx.send(_entity_line(m.entities[eid]))

    # delete / remove
    if sub in ("remove", "del", "rm"):# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "remove"):
            return
        eid = args[1]
        if eid not in m.entities:
            return await ctx.send(f"Entity `{eid}` not found.")
        m.entities[eid].remove()
        return await ctx.send(f"Removed `{eid}` from match.")

    # rename
    if sub == "rename":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "rename"):
            return
        eid = args[1]
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        new_name = " ".join(args[2:])
        m.entities[eid].name = new_name
        m._rebuild_turn_order()
        return await ctx.send(f"Renamed `{eid}` to **{new_name}**.")

    # tp (absolute)
    if sub == "tp":#and len(args) >= 4:
        if await return_help_if_not_enough_args(ctx, args, 4, "ent", "tp"):
            return
        eid = args[1]; x = int(args[2]); y = int(args[3])
        m.entities[eid].tp(x, y)
        return await ctx.send(f"Teleported `{eid}` to ({x},{y}).")

    # move (stepwise)
    if sub == "move":# and len(args) >= 3:
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "move"):
            return
        eid = args[1]
        tokens = " ".join(args[2:]).replace(",", " ").split()
        if not tokens:
            # Defer to help for usage details
            title, body = registry.help_for(["ent","move"])
            return await ctx.send(f"**{title}**\n{body}")
    
        moves: list[tuple[str,int]] = []
        i = 0
        dirs = {"up","down","left","right","u","d","l","r"}
        while i < len(tokens):
            t = tokens[i].lower()
            if t in dirs:
                moves.append((t, 1)); i += 1
            else:
                try: n = int(t)
                except ValueError:
                    return await ctx.send(f"Unexpected token '{t}'.")
                if i + 1 >= len(tokens): return await ctx.send("Count must be followed by a direction.")
                d = tokens[i+1].lower()
                if d not in dirs: return await ctx.send(f"'{d}' is not a direction.")
                moves.append((d, n)); i += 2
    
        total_steps = sum(max(1, int(n)) for _, n in moves)
        try:
            m.entities[eid].move_dirs(moves)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        e = m.entities[eid]
        return await ctx.send(f"Moved `{eid}` {total_steps} step(s) to ({e.x},{e.y}); facing {e.facing}.")
    
    # face
    if sub == "face":# and len(args) >= 3:
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "face"):
            return
        eid = args[1]; dir_ = args[2].lower()
        mapping = {"u":"up","d":"down","l":"left","r":"right"}
        dir_full = mapping.get(dir_, dir_)
        if dir_full not in ("up","down","left","right"):
            return await ctx.send("Use: up/down/left/right")
        m.entities[eid].facing = dir_full
        return await ctx.send(f"Facing of `{eid}` set to {dir_full}.")

    # hp
    if sub == "hp":# and len(args) >= 3:
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "hp"):
            return
        eid = args[1]; delta = int(args[2])
        if delta >= 0:
            m.entities[eid].heal_entity(delta)
            return await ctx.send(f"Healed `{eid}` by {delta}.")
        else:
            m.entities[eid].damage_entity(-delta)
            return await ctx.send(f"Damaged `{eid}` by {-delta}.")
    
    # init
    if sub == "init":# and len(args) >= 3:
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "init"):
            return
        eid = args[1]; value = int(args[2])
        m.entities[eid].set_initiative_entity(value)
        return await ctx.send(f"Set initiative of `{eid}` to {value}.")
    # Fallback: show authoritative help for the root command
    title, body = registry.help_for(["ent"])
    return await ctx.send(f"**{title}**\n{body}")

@registry.command("turn", usage="!turn | !turn next", desc="Show turn order or advance to the next turn.")
async def turn_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if not args:
        order_lines = []
        for idx, eid in enumerate(m.turn_order):
            mark = "➡️" if idx == m.active_index else "  "
            e = m.entities.get(eid)
            if e: order_lines.append(f"{mark} `{eid[:8]}` **{e.name}** (init {e.initiative})")
        return await ctx.send("Turn order:\n" + ("\n".join(order_lines) or "(empty)"))
    sub = args[0].lower()
    if sub == "next":
        eid = m.next_turn()
        if not eid: return await ctx.send("No turn order yet.")
        e = m.entities[eid]
        return await ctx.send(f"It is now **{e.name}**'s turn (id `{eid[:8]}`)")
    if sub == "set":
        if await return_help_if_not_enough_args(ctx, args, 2, "turn", "set"):
            return
        eid = args[1]
        if eid not in m.turn_order:
            raise NotFound(f"Entity '{eid}' not in turn order.")
        m.active_index = m.turn_order.index(eid)
        return await ctx.send(f"Turn set to `{eid}`.")
    # Fallback: show authoritative help
    title, body = registry.help_for(["turn"])
    return await ctx.send(f"**{title}**\n{body}")

#global info about the match that isn't the map or entities
@registry.command("match_toplevel", usage="!match_toplevel", desc="Show active match summary (name/id/turn number).")
async def match_top_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    return await ctx.send(f"**{m.name}** `{m.id}`\nCurrent Turn Number: **{m.turn_number}**\n")

@registry.command("map", usage="!map", desc="Render the ASCII map for the active match.")
async def map_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    return await ctx.send(f"```\n{m.render_ascii()}\n```")

@registry.command("list", usage="!list", desc="List entities in a match, sorted by turn order")
async def list_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    es = m.entities_in_turn_order()
    if not es:
        return await ctx.send("(no entities)")

    active_id = m.turn_order[m.active_index] if m.turn_order else None
    lines = []
    #add a right arrow to show which entity's turn it is right now
    for e in es:
        marker = ">" if e.id == active_id else " "
        lines.append(f"{marker} {_entity_line(e)}")
    return await ctx.send("\n".join(lines))

@registry.command("state", usage="!state", desc="Show match summary, entities, and map.")
async def state_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    # New behavior: show list (turn-order) then map
    await match_top_cmd(ctx, args, mgr)
    await list_cmd(ctx, args, mgr)
    await map_cmd(ctx, args, mgr)

@registry.command("store", usage="!store save <path> | !store load <path>", desc="Save/load all matches and channel bindings.")
async def store_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["store"])
        return await ctx.send(f"**{title}**\n{body}")
    sub = args[0].lower()
    if sub == "save":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "store", "save"):
            return
        mgr.save(args[1]); return await ctx.send(f"Saved to `{args[1]}`")
    if sub == "load":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "store", "load"):
            return
        mgr.load(args[1]); return await ctx.send(f"Loaded from `{args[1]}`")
    # Fallback: show authoritative help
    title, body = registry.help_for(["store"])
    return await ctx.send(f"**{title}**\n{body}")

# ---- Automated Help command (shows available commands----------------------------------------------------------
@registry.command("help", usage="!help [command [sub]]", desc="Show command usage. Try `!help ent` or `!help ent move`.")
async def help_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    title, body = registry.help_for(args)
    await ctx.send(f"**{title}**\n{body}")



# ---- Help annotations for sub-commands (module scope: run at import time) ---------------------

#IMPORTANT: KEEP IT UPDATED WHENEVER I MAKE CHANGES TO THE COMMANDS THEMSELVES!!!!!!

# match
registry.annotate_sub(
    "match", "new",
    usage="!match new <id> <name> <w> <h>",
    desc="Create a match with an explicit id, display name, and grid size."
)
registry.annotate_sub(
    "match", "use",
    usage="!match use <id>",
    desc="Set the current channel's active match."
)
registry.annotate_sub(
    "match", "delete",
    usage="!match delete <id>",
    desc="Delete a match by id."
)

# ent
registry.annotate_sub(
    "ent", "info",
    usage="!ent info <id>",
    desc="Show a single entity summary."
)
registry.annotate_sub(
    "ent", "add",
    usage="!ent add <id> <name> <hp> <x> <y> [init]",
    desc="Create and place a new entity; optional initiative."
)
registry.annotate_sub(
    "ent", "remove", #"del", "rm",
    usage="!ent remove <id>",
    desc="Remove an entity from the match. Alt aliases: del, rm."
)
registry.annotate_sub(
    "ent", "tp",
    usage="!ent tp <id> <x> <y>",
    desc="Teleport entity to an absolute cell (requires free cell)."
)
registry.annotate_sub(
    "ent", "move",
    usage="!ent move <id> <dir[,dir...]> | !ent move <id> <n> <dir> [<n> <dir> ...]",
    desc="Stepwise move; directions: up/down/left/right (u/d/l/r). Final cell must be free."
)
registry.annotate_sub(
    "ent", "hp",
    usage="!ent hp <id> <±n>",
    desc="Adjust HP by a signed amount; death/prone handled by rules."
)
registry.annotate_sub(
    "ent", "init",
    usage="!ent init <id> <n>",
    desc="Set (or update) entity initiative to a fixed value."
)
registry.annotate_sub(
    "ent", "face",
    usage="!ent face <id> <dir>",
    desc="Set facing to up/down/left/right (aliases: u/d/l/r)."
)

# turn
registry.annotate_sub(
    "turn", "next",
    usage="!turn next",
    desc="Advance to the next entity's turn (turn number wraps/increments)."
)

# store
registry.annotate_sub(
    "store", "save",
    usage="!store save <path>",
    desc="Save all matches and channel bindings to a JSON file."
)
registry.annotate_sub(
    "store", "load",
    usage="!store load <path>",
    desc="Load matches and channel bindings from a JSON file."
)