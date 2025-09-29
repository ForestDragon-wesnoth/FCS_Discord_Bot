## vtt_commands.py (framework‑agnostic commands + registry)
# vtt_commands.py
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Protocol, Any
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
    def command(self, name: str):
        def deco(fn: Handler):
            self._handlers[name] = fn
            return fn
        return deco
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
    return f"{e.name} ({e.id}): HP: {e.hp}/{max_hp} facing {e.facing}"
    #TODO: add a way to dynamically define what variables are shown for entities!

def active_match(mgr: MatchManager, ctx: ReplyContext):
    mid = mgr.get_active_for_channel(ctx.channel_key)
    if not mid:
        raise NotFound("No active match for this channel. Use `!match use <id>`.")
    return mgr.get(mid)

# ---- Commands ----------------------------------------------------------------
@registry.command("match")
async def match_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        pairs = mgr.list()
        if not pairs: return await ctx.send("No matches. `!match new <name> <w> <h>`")
        lines = [f"**{name}** — `{mid}`" for mid, name in pairs]
        return await ctx.send("Matches:\n" + "\n".join(lines))
    sub = args[0]
    if sub == "new" and len(args) >= 5:
        match_id = args[1]; name = args[2]; w = int(args[3]); h = int(args[4])
        mid = mgr.create_match(match_id, name, w, h)
        return await ctx.send(f"Created **{name}** with id `{mid}`.")
    if sub == "use" and len(args) >= 2:
        mid = args[1]
        mgr.set_active_for_channel(ctx.channel_key, mid)
        return await ctx.send(f"Channel using match `{mid}`.")
    if sub == "delete" and len(args) >= 2:
        mgr.delete_match(args[1])
        return await ctx.send(f"Deleted `{args[1]}`.")
    return await ctx.send("Usage: `!match new <id> <name> <w> <h>` | `!match use <id>` | `!match delete <id>`")

@registry.command("ent")
async def ent_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        # if !end has no arguments, then mirror the "!list" command: show entities in turn order (active first) with the same format.
        return await list_cmd(ctx, [], mgr)
    sub = args[0]
    m = active_match(mgr, ctx)
    # --- info (single entity line) ---
    if sub == "info" and len(args) >= 2:
        eid = args[1]
        if eid not in m.entities:
            return await ctx.send(f"Entity `{eid}` not found.")
        return await ctx.send(_entity_line(m.entities[eid]))
    # add
    if sub == "add" and len(args) >= 6:
        eid = args[1]; name = args[2]; hp = int(args[3]); x = int(args[4]); y = int(args[5])
        init = int(args[6]) if len(args) >= 7 else None
        e = Entity(id=eid, name=name, hp=hp, x=x, y=y)
        e.spawn(m, x, y, initiative=init)
        return await ctx.send(f"Added `{name}` with id `{eid}` at ({x},{y}).")
    
    # delete / remove
    if sub in ("del", "rm", "remove") and len(args) >= 2:
        eid = args[1]
        if eid not in m.entities:
            return await ctx.send(f"Entity `{eid}` not found.")
        m.entities[eid].remove()
        return await ctx.send(f"Removed `{eid}` from match.")
    
    # tp (absolute)
    if sub == "tp" and len(args) >= 4:
        eid = args[1]; x = int(args[2]); y = int(args[3])
        m.entities[eid].tp(x, y)
        return await ctx.send(f"Teleported `{eid}` to ({x},{y}).")
    
    # move (stepwise)
    if sub == "move" and len(args) >= 3:
        eid = args[1]
        tokens = " ".join(args[2:]).replace(",", " ").split()
        if not tokens:
            return await ctx.send("Usage: `!ent move <id> <dir[,dir...]>` or `!ent move <id> <n> <dir> [<n> <dir> ...]`")
    
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
    if sub == "face" and len(args) >= 3:
        eid = args[1]; dir_ = args[2].lower()
        mapping = {"u":"up","d":"down","l":"left","r":"right"}
        dir_full = mapping.get(dir_, dir_)
        if dir_full not in ("up","down","left","right"):
            return await ctx.send("Use: up/down/left/right")
        m.entities[eid].facing = dir_full
        return await ctx.send(f"Facing of `{eid}` set to {dir_full}.")
    
    # hp
    if sub == "hp" and len(args) >= 3:
        eid = args[1]; delta = int(args[2])
        if delta >= 0:
            m.entities[eid].heal_entity(delta)
            return await ctx.send(f"Healed `{eid}` by {delta}.")
        else:
            m.entities[eid].damage_entity(-delta)
            return await ctx.send(f"Damaged `{eid}` by {-delta}.")
    
    # init
    if sub == "init" and len(args) >= 3:
        eid = args[1]; value = int(args[2])
        m.entities[eid].set_initiative_entity(value)
        return await ctx.send(f"Set initiative of `{eid}` to {value}.")

    return await ctx.send(
        "Usage: "
        "`!ent info <id>` | "
        "`!ent add <id> <name> <hp> <x> <y> [init]` | "
        "`!ent move <id> <x> <y>` | "
        "`!ent hp <id> <±n>` | "
        "`!ent init <id> <n>`"
    )

@registry.command("turn")
async def turn_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if not args:
        order_lines = []
        for idx, eid in enumerate(m.turn_order):
            mark = "➡️" if idx == m.active_index else "  "
            e = m.entities.get(eid)
            if e: order_lines.append(f"{mark} `{eid[:8]}` **{e.name}** (init {e.initiative})")
        return await ctx.send("Turn order:\n" + ("\n".join(order_lines) or "(empty)"))
    if args[0] == "next":
        eid = m.next_turn()
        if not eid: return await ctx.send("No turn order yet.")
        e = m.entities[eid]
        return await ctx.send(f"It is now **{e.name}**'s turn (id `{eid[:8]}`)")
    return await ctx.send("Usage: `!turn` | `!turn next`")

#global info about the match that isn't the map or entities
@registry.command("match_toplevel")
async def match_top_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    return await ctx.send(f"**{m.name}** `{m.id}`\nCurrent Turn Number: **{m.turn_number}**\n")

@registry.command("map")
async def map_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    return await ctx.send(f"\n{m.render_ascii()}\n```")

@registry.command("list")
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

@registry.command("state")
async def state_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    # New behavior: show list (turn-order) then map
    await match_top_cmd(ctx, args, mgr)
    await list_cmd(ctx, args, mgr)
    await map_cmd(ctx, args, mgr)

@registry.command("store")
async def store_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args: return await ctx.send("Use: `!store save <path>` | `!store load <path>`")
    if args[0] == "save" and len(args) >= 2:
        mgr.save(args[1]); return await ctx.send(f"Saved to `{args[1]}`")
    if args[0] == "load" and len(args) >= 2:
        mgr.load(args[1]); return await ctx.send(f"Loaded from `{args[1]}`")
    return await ctx.send("Use: `!store save <path>` | `!store load <path>`")
