## commands.py (Discord wrappers → engine)

# commands.py
from discord.ext import commands
from typing import Optional
from logic import MatchManager, Entity, VTTError, OutOfBounds, Occupied, NotFound

# You can import a shared manager, or pass one in from bot.py
_mgr = MatchManager()

def get_channel_key(ctx) -> str:
    # Scope matches per channel by default (server+channel)
    gid = getattr(ctx.guild, "id", "DM")
    return f"{gid}:{ctx.channel.id}"

def setup_commands(bot: commands.Bot, mgr: Optional[MatchManager] = None):
    global _mgr
    if mgr is not None:
        _mgr = mgr

    # ---------------- MATCH COMMANDS ----------------
    @bot.group(name="match", invoke_without_command=True)
    async def match_group(ctx):
        pairs = _mgr.list()
        if not pairs:
            await ctx.send("No matches yet. Use `!match new <name> <w> <h>`.")
            return
        lines = [f"**{name}** — `{mid}`" for mid, name in pairs]
        await ctx.send("Matches:\n" + "\n".join(lines))

    @match_group.command(name="new")
    async def match_new(ctx, name: str, width: int, height: int):
        try:
            mid = _mgr.create_match(name, width, height)
            await ctx.send(f"Created match **{name}** with id `{mid}`.")
        except VTTError as e:
            await ctx.send(f"❌ {e}")

    @match_group.command(name="use")
    async def match_use(ctx, match_id: str):
        try:
            _mgr.set_active_for_channel(get_channel_key(ctx), match_id)
            await ctx.send(f"Channel now using match `{match_id}`.")
        except NotFound:
            await ctx.send("❌ Match not found")

    @match_group.command(name="delete")
    async def match_delete(ctx, match_id: str):
        try:
            _mgr.delete_match(match_id)
            await ctx.send(f"Deleted match `{match_id}`.")
        except NotFound:
            await ctx.send("❌ Match not found")

    # ---------------- ENTITY COMMANDS ----------------
    def _active(ctx):
        mid = _mgr.get_active_for_channel(get_channel_key(ctx))
        if not mid:
            raise NotFound("No active match set for this channel. Use `!match use <id>`.\n")
        return _mgr.get(mid)

    @bot.group(name="ent", invoke_without_command=True)
    async def ent_group(ctx):
        try:
            m = _active(ctx)
        except NotFound as e:
            await ctx.send(f"❌ {e}")
            return
        lines = []
        for e in m.entities.values():
            lines.append(f"`{e.id[:8]}` **{e.name}** HP:{e.hp}/{e.max_hp} at ({e.x},{e.y}) init:{e.initiative}")
        await ctx.send("Entities:\n" + ("\n".join(lines) or "(none)"))

    @ent_group.command(name="add")
    async def ent_add(ctx, name: str, hp: int, x: int, y: int, initiative: Optional[int] = None):
        try:
            m = _active(ctx)
            eid = m.add_entity(Entity(name=name, hp=hp, x=x, y=y), x, y, initiative=initiative)
            await ctx.send(f"Added `{name}` with id `{eid}` at ({x},{y}).")
        except (OutOfBounds, Occupied, VTTError) as e:
            await ctx.send(f"❌ {e}")

    @ent_group.command(name="move")
    async def ent_move(ctx, entity_id: str, x: int, y: int):
        try:
            m = _active(ctx)
            m.move_entity(entity_id, x, y)
            await ctx.send(f"Moved `{entity_id}` to ({x},{y}).")
        except (OutOfBounds, Occupied, NotFound) as e:
            await ctx.send(f"❌ {e}")

    @ent_group.command(name="hp")
    async def ent_hp(ctx, entity_id: str, delta: int):
        try:
            m = _active(ctx)
            if delta >= 0:
                m.heal(entity_id, delta)
                await ctx.send(f"Healed `{entity_id}` by {delta}.")
            else:
                m.damage(entity_id, -delta)
                await ctx.send(f"Damaged `{entity_id}` by {-delta}.")
        except NotFound as e:
            await ctx.send(f"❌ {e}")

    @ent_group.command(name="init")
    async def ent_init(ctx, entity_id: str, value: int):
        try:
            m = _active(ctx)
            m.set_initiative(entity_id, value)
            await ctx.send(f"Set initiative of `{entity_id}` to {value}.")
        except NotFound as e:
            await ctx.send(f"❌ {e}")

    # ---------------- TURN COMMANDS ----------------
    @bot.group(name="turn", invoke_without_command=True)
    async def turn_group(ctx):
        try:
            m = _active(ctx)
        except NotFound as e:
            await ctx.send(f"❌ {e}")
            return
        order_lines = []
        for idx, eid in enumerate(m.turn_order):
            mark = "➡️" if idx == m.active_index else "  "
            e = m.entities.get(eid)
            if e:
                order_lines.append(f"{mark} `{eid[:8]}` **{e.name}** (init {e.initiative})")
        await ctx.send("Turn order:\n" + ("\n".join(order_lines) or "(empty, set initiatives with `!ent init`)"))

    @turn_group.command(name="next")
    async def turn_next(ctx):
        try:
            m = _active(ctx)
            eid = m.next_turn()
            if not eid:
                await ctx.send("No turn order yet. Set initiatives with `!ent init`.")
                return
            e = m.entities[eid]
            await ctx.send(f"It is now **{e.name}**'s turn (id `{eid[:8]}`)")
        except NotFound as e:
            await ctx.send(f"❌ {e}")

    # ---------------- DEBUG / VIEW ----------------
    @bot.command(name="state")
    async def state(ctx):
        try:
            m = _active(ctx)
            ascii_map = m.render_ascii()
            await ctx.send(f"**{m.name}** `{m.id}`\n``````\n{ascii_map}\n``````")
        except NotFound as e:
            await ctx.send(f"❌ {e}")

    # ---------------- SAVE / LOAD ----------------
    @bot.group(name="store", invoke_without_command=True)
    async def store_group(ctx):
        await ctx.send("Use `!store save <path>` or `!store load <path>` (paths are server-local)")

    @store_group.command(name="save")
    async def store_save(ctx, path: str):
        try:
            _mgr.save(path)
            await ctx.send(f"Saved to `{path}`")
        except Exception as e:
            await ctx.send(f"❌ Failed to save: {e}")

    @store_group.command(name="load")
    async def store_load(ctx, path: str):
        try:
            _mgr.load(path)
            await ctx.send(f"Loaded from `{path}`")
        except Exception as e:
            await ctx.send(f"❌ Failed to load: {e}")