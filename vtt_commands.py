## vtt_commands.py (framework‑agnostic commands + registry)
# vtt_commands.py
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Protocol, Any, Tuple
from logic import MatchManager, Entity, VTTError, OutOfBounds, Occupied, NotFound, DuplicateId, ReservedId, _coerce_rule_value, _parse_bool

#used for Gamesystem-related commands
from logic import DEFAULT_SYSTEM_SETTINGS, ALLOWED_DIRECTIONS, RULE_SCHEMA, RULES_REGISTRY

import re
import json

#TODO:
#from logic import FormulaEngine  # tiny expression/assignment runtime
#from logic import expand_inline_formulas, coerce_number
#from logic import compute_default_specials, reserved_special_id_names

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


def _resolve_eid(m, token: str) -> str:
    """
    Accepts an entity id token and returns a concrete entity id.
    Supports the shorthands 'current' / 'this' to mean the entity
    whose turn it currently is.
    """
    t = str(token).strip().lower()
    if t in {"current", "this"}:
        eid = m.current_entity_id()
        if not eid:
            raise NotFound("No current entity (turn order is empty).")
        return eid
    return token




def _parse_scalar(token: str):
    # int → float → str
    try:
        return int(token, 10)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token  # leave as string

#for creating more dictionaries inside vars, like inventory/skills/etc.

def _set_deep_key(root: Dict[str, Any], dotted: str, value: Any):
    """
    Create/descend dicts along a dotted path and set the final key to value.
    Example: _set_deep_key(e.vars, "parts.effects.slow.duration", 2.5)
    """
    if not dotted:
        raise VTTError("Key path cannot be empty.")
    keys = dotted.split(".")
    cur = root
    for k in keys[:-1]:
        node = cur.get(k)
        if not isinstance(node, dict):
            node = {}
            cur[k] = node
        cur = node
    cur[keys[-1]] = value



# ---- Commands ----------------------------------------------------------------
@registry.command("match", usage="!match <subcommand> ...", desc="List matches, create one, or switch the active match for this channel.")
async def match_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        pairs = mgr.list()
        if not pairs: return await ctx.send("No matches. Create a new match with first (see '!help match new')")
        lines = [f"**{name}** — `{mid}`" for mid, name in pairs]
        return await ctx.send("Matches:\n" + "\n".join(lines))
    sub = args[0]
    if sub == "new":# and len(args) >= 5:
        if await return_help_if_not_enough_args(ctx, args, 5, "match", "new"):
            return
        match_id, name, w, h = args[1], args[2], int(args[3]), int(args[4])
        system_name = None
        # parse optional --system <name>
        i = 5
        while i < len(args):
            if args[i] in ("--system", "-s") and i + 1 < len(args):
                system_name = args[i+1]
                i += 2
            elif args[i].startswith("system="):
                system_name = args[i].split("=", 1)[1]
                i += 1
            else:
                i += 1
        mid = mgr.create_match(match_id, name, w, h, channel_key=ctx.channel_key, system_name=system_name)
        return await ctx.send(f"Created match `{name}` with id `{mid}` using system `{mgr.get(mid).system_name}`.")
    if sub == "use":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "match", "use"):
            return
        mid = args[1]
        mgr.set_active_for_channel(ctx.channel_key, mid)
        m = mgr.matches.get(mid)
        return await ctx.send(f"Active match is now **{m.name}** (`{mid}`, system `{m.system_name}`).")
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
#annonate subcommands next to the command itself:
registry.annotate_sub(
    "match", "new",
    usage="!match new <id> <name> <w> <h> [--system <name>]", 
    desc="Create a match; optionally override the default GameSystem, the argument has to be started with --system."
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
registry.annotate_sub(
    "match", "rename",
    usage="!match rename <id> <new_name>",
    desc="Rename a selected match."
)

# ---- system (gamesystem commands)-----

#TODO: add "delete" command for gamesystems (but TEST IT VERY CAREFULLY, like making sure there is always at least one gamesystem per server that's defaulted to, etc.)
@registry.command("system", usage="!system <subcommand> ...", desc="Manage GameSystems and defaults (global/server/channel).")
async def system_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        names = mgr.list_systems()
        lines = [f"`{n}`" + ("  ← default" if n == mgr.default_system_name else "") for n in names]
        return await ctx.send("Systems:\n" + ("\n".join(lines) or "(none)"))
    sub = args[0]
    if sub == "list":
        systems = mgr.systems
        if not systems:
            return await ctx.send("No game systems exist yet.")
        lines = [f"- **{name}**" for name in systems]
        return await ctx.send("Current game systems:\n" + "\n".join(lines))
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "system", "info"):
            return
    
        name = args[1]
        s = mgr.get_system(name)
        default_badge = " (default)" if name == mgr.default_system_name else ""
    
        overridden_lines: List[str] = []
        inherited_lines: List[str] = []
    
        for k in sorted(RULES_REGISTRY.keys()):
            spec = RULES_REGISTRY[k]
            schema = spec["schema"]
            t = schema.get("type")
            extra = ""
            if t == "enum":
                extra = f", one of {{{', '.join(sorted(schema.get('choices', [])))}}}"
    
            effective_val = s.get(k)
            default_val = DEFAULT_SYSTEM_SETTINGS.get(k)
            desc = spec.get("desc", "")
    
            if k in (s.settings or {}) and s.settings[k].value != default_val:
                overridden_lines.append(
                    f"- `{k}` = {effective_val!r} — {desc} (type: {t}{extra})"
                )
            else:
                inherited_lines.append(
                    f"- `{k}` = {default_val!r} — {desc} (type: {t}{extra})"
                )
    
        if not overridden_lines:
            overridden_lines = ["(none)"]
        if not inherited_lines:
            inherited_lines = ["(none)"]
    
        header = f"**{name}**{default_badge} — rule breakdown"
        msg = (
            f"{header}\n\n"
            f"**Rules overwritten by this system:**\n"
            + "\n".join(overridden_lines)
            + "\n\n**Rules inherited from default:**\n"
            + "\n".join(inherited_lines)
        )
    
        return await ctx.send(msg)

    if sub == "rules":
        lines: List[str] = []
        for k in sorted(RULES_REGISTRY.keys()):
            spec = RULES_REGISTRY[k]
            schema = spec["schema"]
            t = schema.get("type")
            extra = ""
            if t == "enum":
                extra = f", one of {{{', '.join(sorted(schema.get('choices', [])))}}}"
            default_val = DEFAULT_SYSTEM_SETTINGS.get(k)
            desc = spec.get("desc", "")
            lines.append(
                f"- `{k}` — {desc} (type: {t}{extra}; default: {default_val!r})"
            )
        return await ctx.send("**All rules**\n" + ("\n".join(lines) or "(none)"))
    if sub == "new":
        if await return_help_if_not_enough_args(ctx, args, 2, "system", "new"):
            return
        name = args[1]
        mgr.create_system(name)
        return await ctx.send(f"Created GameSystem `{name}`.")    
    if sub == "set":
        if await return_help_if_not_enough_args(ctx, args, 4, "system", "set"):
            return
        name, key, raw_value = args[1], args[2], args[3]
    
        # hard block unknown keys (also guards against keys that exist in DEFAULT_SYSTEM_SETTINGS
        # but we haven't made safe yet)
        value = _coerce_rule_value(key, raw_value)
    
        # optional: ensure the key exists in defaults too, so saves show a stable shape
        if key not in DEFAULT_SYSTEM_SETTINGS:
            # You can relax this if you want to allow brand-new custom keys
            raise VTTError(f"'{key}' is not in engine defaults. Add it to DEFAULT_SYSTEM_SETTINGS first.")
    
        s = mgr.get_system(name)
        s.set(key, value)
        return await ctx.send(f"`{name}`.{key} = {value!r}")
    if sub == "default":
        if await return_help_if_not_enough_args(ctx, args, 3, "system", "default"):
            return
        scope = args[1]
        name = args[2]
        if scope == "global":
            mgr.set_global_default_system(name)
            return await ctx.send(f"Global default GameSystem is now `{name}`.")
        elif scope == "server":
            server_id = (ctx.channel_key.split(":",1)[0])
            mgr.set_server_default_system(server_id, name)
            return await ctx.send(f"Server default GameSystem for `{server_id}` is now `{name}`.")
        elif scope == "channel":
            mgr.set_channel_default_system(ctx.channel_key, name)
            return await ctx.send(f"Channel default GameSystem is now `{name}`.")
        else:
            return await ctx.send("Scope must be one of: global | server | channel")
    title, body = registry.help_for(["system"])
    return await ctx.send(f"**{title}**\n{body}")
registry.annotate_sub("system", "list", usage="!system list", desc="List existing GameSystems.")
registry.annotate_sub("system", "info", usage="!system info <name>", desc="Show a GameSystem's settings.")
registry.annotate_sub("system", "rules", usage="!system rules", desc="List all available rules, their defaults, their types, and descriptions")
registry.annotate_sub("system", "new", usage="!system new <name>", desc="Create a GameSystem.")
registry.annotate_sub("system", "set", usage="!system set <name> <key> <value>", desc="Change a GameSystem setting (booleans/int auto-coerced).")
registry.annotate_sub("system", "default", usage="!system default <global|server|channel> <name>", desc="Set default GameSystem.")

@registry.command("ent", usage="!ent <subcommand> ...", desc="Manage entities in the active match, lots of available sub-commands. Note: <id> parameter also accepts 'this' or 'current', to target the entity whose turn it is right now")
async def ent_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    # Show authoritative help if no subcommand was given
    if not args:
        # if no subcommand: just show the authoritative help for !ent
        title, body = registry.help_for(["ent"])
        return await ctx.send(f"**{title}**\n{body}")

    m = active_match(mgr, ctx)

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
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "info"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        e = m.entities[eid]
        # Show normal line plus full dump of the vars data
        vars_block = json.dumps(e.vars or {}, indent=2, sort_keys=True)
        return await ctx.send(f"{_entity_line(e)}\n\n**vars (json)**:\n```{vars_block}\n```")

    # delete / remove
    if sub in ("remove", "del", "rm"):# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "remove"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            return await ctx.send(f"Entity `{eid}` not found.")
        m.entities[eid].remove()
        return await ctx.send(f"Removed `{eid}` from match.")

    # rename
    if sub == "rename":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "rename"):
            return
        eid = _resolve_eid(m, args[1])
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
        eid = _resolve_eid(m, args[1]); 
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        x = int(args[2]); y = int(args[3])
        m.entities[eid].tp(x, y)
        return await ctx.send(f"Teleported `{eid}` to ({x},{y}).")

    # move (stepwise)
    if sub == "move":# and len(args) >= 3:
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "move"):
            return
        eid = _resolve_eid(m, args[1]); 
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
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
        eid = _resolve_eid(m, args[1]); 
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        dir_ = args[2].lower()
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
        eid = _resolve_eid(m, args[1]); 
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")        
        delta = int(args[2])
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
        eid = _resolve_eid(m, args[1]); 
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")        
        value = int(args[2])
        m.entities[eid].set_initiative_entity(value)
        return await ctx.send(f"Set initiative of `{eid}` to {value}.")
    # clone
    if sub == "clone":
        if await return_help_if_not_enough_args(ctx, args, 5, "ent", "clone"):
            return

        src_id = _resolve_eid(m, args[1])
        new_id = args[2]
        x, y = int(args[3]), int(args[4])

        if src_id not in m.entities:
            raise NotFound(f"Entity '{src_id}' not found.")
        if new_id in m.entities:
            raise DuplicateId(f"Entity id '{new_id}' already exists.")
        if not m.in_bounds(x, y):
            raise OutOfBounds(f"({x},{y}) outside {m.grid_width}x{m.grid_height}")
        if m.is_occupied(x, y):
            raise Occupied(f"Cell ({x},{y}) already occupied.")

        src = m.entities[src_id]
        payload = src.to_dict()
        payload.update({"id": new_id, "x": x, "y": y})

        clone = Entity.from_dict(payload)
        # Use spawn to register/validate; preserve original facing after spawn
        clone.spawn(m, x, y, initiative=src.initiative)
        clone.facing = src.facing

        return await ctx.send(f"Cloned `{src_id}` → `{new_id}` at ({x},{y}).")
    # set_var
    if sub == "set_var":
        # Usage: !ent set_var <id> <key> <value>
        if await return_help_if_not_enough_args(ctx, args, 4, "ent", "set_var"):
            return

        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")

        key_path = args[2]                # dotted path: e.g., "parts.effects.slow.duration"
        raw_value = args[3]               # use quotes for spaces: "burning aura"
        value = _parse_scalar(raw_value)  # int → float → str

        e = m.entities[eid]
        _set_deep_key(e.vars, key_path, value)

        # Optional: echo the new value for quick confirmation
        return await ctx.send(f"`{eid}` vars.{key_path} = {value!r}")

    # Fallback: show authoritative help for the root command
    title, body = registry.help_for(["ent"])
    return await ctx.send(f"**{title}**\n{body}")
#annonate subcommands next to the command itself:
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
    "ent", "rename",
    usage="!ent rename <id> <new_name>",
    desc="Rename an entity."
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
    "ent", "face",
    usage="!ent face <id> <dir>",
    desc="Set facing to up/down/left/right (aliases: u/d/l/r)."
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
    "ent", "clone",
    usage="!ent clone <id> <new_id> <x> <y>",
    desc="Create a perfect copy of <id> with new id <new_id> at position (x,y)."
)
registry.annotate_sub(
    "ent", "set_var",
    usage="!ent set_var <id> <key> <value>",
    desc="Set a value in the entity's vars. <key> supports dotted paths (for example '!ent set_var adventurer inventory.sword.damage 10' will do sub-containers for inventory and sword); value auto-coerces int/float/string."
)

@registry.command("turn", usage="!turn | !turn next | ...", desc="See/advance/set/etc. turns")
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
        eid = _resolve_eid(m, args[1])
        if eid not in m.turn_order:
            raise NotFound(f"Entity '{eid}' not in turn order.")
        m.active_index = m.turn_order.index(eid)
        return await ctx.send(f"Active turn set to `{eid}` (round {m.turn_number})")
    # Fallback: show authoritative help
    title, body = registry.help_for(["turn"])
    return await ctx.send(f"**{title}**\n{body}")
#annonate subcommands next to the command itself:
registry.annotate_sub(
    "turn", "next",
    usage="!turn next",
    desc="Advance to the next entity's turn (turn number wraps/increments)."
)
registry.annotate_sub(
    "turn", "set",
    usage="!turn set <id>",
    desc="Set the current turn to be the turn of entity <id>"
)


#global info about the match that isn't the map or entities
@registry.command("match_toplevel", usage="!match_toplevel", desc="Show active match summary (name/id/turn number).")
async def match_top_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    return await ctx.send(f"**{m.name}** `{m.id}`\nGame System: **{m.system_name}**\nCurrent Turn Number: **{m.turn_number}**\n")

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
    lines.append(f"Entities:")
    #add a right arrow to show which entity's turn it is right now
    for e in es:
        marker = "→" if e.id == active_id else "  "
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
#annonate subcommands next to the command itself:
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




# ---- Automated Help command (shows available commands----------------------------------------------------------
@registry.command("help", usage="!help [command [sub]]", desc="Show command usage. Try `!help ent` or `!help ent move`.")
async def help_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    title, body = registry.help_for(args)
    await ctx.send(f"**{title}**\n{body}")

