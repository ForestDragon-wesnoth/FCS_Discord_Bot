## vtt_commands.py (framework‑agnostic commands + registry)
# vtt_commands.py
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Protocol, Any
from logic import MatchManager, Entity, VTTError, OutOfBounds, Occupied, NotFound

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
    if sub == "new" and len(args) >= 4:
        name = args[1]; w = int(args[2]); h = int(args[3])
        mid = mgr.create_match(name, w, h)
        return await ctx.send(f"Created **{name}** with id `{mid}`.")
    if sub == "use" and len(args) >= 2:
        mid = args[1]
        mgr.set_active_for_channel(ctx.channel_key, mid)
        return await ctx.send(f"Channel using match `{mid}`.")
    if sub == "delete" and len(args) >= 2:
        mgr.delete_match(args[1])
        return await ctx.send(f"Deleted `{args[1]}`.")
    return await ctx.send("Usage: `!match new <name> <w> <h>` | `!match use <id>` | `!match delete <id>`")

@registry.command("ent")
async def ent_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        m = active_match(mgr, ctx)
        lines = [f"`{e.id[:8]}` **{e.name}** HP:{e.hp}/{e.max_hp} at ({e.x},{e.y}) init:{e.initiative}" for e in m.entities.values()]
        return await ctx.send("Entities:\n" + ("\n".join(lines) or "(none)"))
    sub = args[0]
    m = active_match(mgr, ctx)
    if sub == "add" and len(args) >= 5:
        name = args[1]; hp = int(args[2]); x = int(args[3]); y = int(args[4])
        init = int(args[5]) if len(args) >= 6 else None
        eid = m.add_entity(Entity(name=name, hp=hp, x=x, y=y), x, y, initiative=init)
        return await ctx.send(f"Added `{name}` with id `{eid}` at ({x},{y}).")
    if sub == "move" and len(args) >= 4:
        eid = args[1]; x = int(args[2]); y = int(args[3]); m.move_entity(eid, x, y)
        return await ctx.send(f"Moved `{eid}` to ({x},{y}).")
    if sub == "hp" and len(args) >= 3:
        eid = args[1]; delta = int(args[2])
        if delta >= 0: m.heal(eid, delta); msg = f"Healed `{eid}` by {delta}."
        else: m.damage(eid, -delta); msg = f"Damaged `{eid}` by {-delta}."
        return await ctx.send(msg)
    if sub == "init" and len(args) >= 3:
        eid = args[1]; value = int(args[2]); m.set_initiative(eid, value)
        return await ctx.send(f"Set initiative of `{eid}` to {value}.")
    return await ctx.send("Usage: `!ent add <name> <hp> <x> <y> [init]` | `!ent move <id> <x> <y>` | `!ent hp <id> <±n>` | `!ent init <id> <n>`")

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

@registry.command("state")
async def state_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    return await ctx.send(f"**{m.name}** `{m.id}`\n```\n{m.render_ascii()}\n````")

@registry.command("store")
async def store_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args: return await ctx.send("Use: `!store save <path>` | `!store load <path>`")
    if args[0] == "save" and len(args) >= 2:
        mgr.save(args[1]); return await ctx.send(f"Saved to `{args[1]}`")
    if args[0] == "load" and len(args) >= 2:
        mgr.load(args[1]); return await ctx.send(f"Loaded from `{args[1]}`")
    return await ctx.send("Use: `!store save <path>` | `!store load <path>`")
