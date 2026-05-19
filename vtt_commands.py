## vtt_commands.py (framework‑agnostic commands + registry)
# vtt_commands.py
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Protocol, Any, Tuple
from logic import MatchManager, Entity, VTTError, OutOfBounds, Occupied, NotFound, DuplicateId, ReservedId, _coerce_rule_value, _parse_bool

#used for Gamesystem-related commands
from logic import DEFAULT_SYSTEM_SETTINGS, ALLOWED_DIRECTIONS, RULE_SCHEMA, RULES_REGISTRY

# Passive system
from logic import Passive, HOOK_NAMES

# Clamp system
from logic import ClampSpec

# Formula engine (expression-only $(...) substitution here; full program eval used by !eval)
from formula import resolve_arg_token, FormulaEngine, EvalCtx, FormulaError, validate_program

import re
import json

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

# Template engine for entity_line_format / entity_info_format rules.
# Syntax:
#   {key}              substitute value (str()-formatted), missing -> "<?key?>" sentinel
#   {key.sub.sub}      dotted-path traversal through nested dicts in vars
#   {?key?}...{/?}     conditional section; renders inner only if value is truthy
#                      (None/""/{}/[]  are falsy; 0 and False are too — standard Python bool())
#   \n                 literal backslash-n in rule string becomes a real newline
# No nesting of conditionals in v1; sequential conditionals work fine.

import re as _re
_TMPL_COND_RE = _re.compile(r"\{\?([a-zA-Z_][\w.]*)\?\}(.*?)\{/\?\}", _re.DOTALL)
_TMPL_PLACE_RE = _re.compile(r"\{([a-zA-Z_][\w.]*)\}")


def _tmpl_fmt_value(v: Any) -> str:
    """Format a value for placeholder substitution."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, dict):
        if not v:
            return ""
        keys = list(v.keys())
        if len(keys) > 6:
            return "{" + ", ".join(keys[:6]) + f", ... ({len(keys)} total)" + "}"
        return "{" + ", ".join(keys) + "}"
    if isinstance(v, (list, tuple, set)):
        items = list(v)
        if not items:
            return ""
        if len(items) > 6:
            return "[" + ", ".join(str(x) for x in items[:6]) + f", ... ({len(items)} total)" + "]"
        return "[" + ", ".join(str(x) for x in items) + "]"
    return str(v)


def _tmpl_resolve(ctx: Dict[str, Any], path: str) -> Tuple[bool, Any]:
    """Resolve a dotted path. Returns (found, value)."""
    parts = path.split(".")
    cur: Any = ctx
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return False, None
    return True, cur


def _tmpl_truthy(found: bool, val: Any) -> bool:
    """Conditional truthiness — missing path always falsy; otherwise Python bool()."""
    if not found:
        return False
    if val is None:
        return False
    return bool(val)


def _render_template(tmpl: str, ctx: Dict[str, Any]) -> str:
    """Render a template string with placeholders and conditionals."""
    # Allow \n in the stored rule string to produce real newlines
    s = tmpl.replace("\\n", "\n").replace("\\t", "\t")

    # Pass 1: conditionals
    def _cond_sub(m):
        path, inner = m.group(1), m.group(2)
        found, val = _tmpl_resolve(ctx, path)
        if not _tmpl_truthy(found, val):
            return ""
        return inner
    # Run multiple times in case conditional bodies become eligible for more (rare).
    for _ in range(4):
        new_s = _TMPL_COND_RE.sub(_cond_sub, s)
        if new_s == s:
            break
        s = new_s

    # Pass 2: placeholders
    def _place_sub(m):
        path = m.group(1)
        found, val = _tmpl_resolve(ctx, path)
        if not found:
            return f"<?{path}?>"
        return _tmpl_fmt_value(val)
    return _TMPL_PLACE_RE.sub(_place_sub, s)


def _entity_template_context(e: Entity) -> Dict[str, Any]:
    """Build the placeholder context for an Entity."""
    hp_var, max_hp_var, init_var = e._vital_var_names()
    # Start with entity attributes (these win over var keys on collision).
    ctx: Dict[str, Any] = {
        "id": e.id,
        "name": e.name,
        "x": e.x,
        "y": e.y,
        "facing": e.facing,
        "team": e.team if e.team else "",
        "initiative": e.vars.get(init_var, 0),
        "hp": e.hp,
        "max_hp": e.max_hp if e.max_hp is not None else e.hp,
        "status_csv": ", ".join(sorted(e.status)) if e.status else "",
        "passives_csv": ", ".join(sorted(e.passives.keys())) if e.passives else "",
    }
    # Merge vars — vars win for keys NOT already in the well-known set above.
    # (So a user's `hp` var won't override the computed alias, since `hp` is in ctx.)
    for k, v in e.vars.items():
        if k not in ctx:
            ctx[k] = v
    return ctx


def _entity_line(e: Entity) -> str:
    """Single-line entity summary, rendered from the active match's entity_line_format rule."""
    tmpl = None
    if e._match is not None:
        tmpl = e._match.rules.get("entity_line_format")
    if not tmpl:
        # Fallback to the engine default (shouldn't happen if system rules are populated)
        tmpl = DEFAULT_SYSTEM_SETTINGS.get(
            "entity_line_format",
            "{name} ({id}): HP: {hp}/{max_hp} X,Y: {x},{y} facing {facing}",
        )
    return _render_template(tmpl, _entity_template_context(e))


def _entity_card(e: Entity) -> str:
    """Multi-line entity card, rendered from the active match's entity_info_format rule."""
    tmpl = None
    if e._match is not None:
        tmpl = e._match.rules.get("entity_info_format")
    if not tmpl:
        tmpl = DEFAULT_SYSTEM_SETTINGS.get("entity_info_format", "")
    return _render_template(tmpl, _entity_template_context(e))


def _entity_dump(e: Entity) -> str:
    """Raw 'show everything' view — template-free, complete state of the entity."""
    parts: List[str] = []
    parts.append(f"**{e.name}** (`{e.id}`)")
    parts.append(f"Position: ({e.x}, {e.y}) facing {e.facing}")
    parts.append(f"Team: {e.team if e.team else '(none)'}")
    parts.append(f"Status: {', '.join(sorted(e.status)) if e.status else '(none)'}")
    hp_var, max_hp_var, init_var = e._vital_var_names()
    parts.append(
        f"Vitals: hp_var=`{hp_var}` max_hp_var=`{max_hp_var}` turnorder_var=`{init_var}`"
    )
    parts.append("")
    parts.append("**vars (full json):**")
    parts.append(f"```{json.dumps(e.vars or {}, indent=2, sort_keys=True)}\n```")
    if e.passives:
        parts.append("**passives:**")
        for pid, p in e.passives.items():
            parts.append(f"- `{pid}` ({p.when}): `{p.formula}`")
    else:
        parts.append("**passives:** (none)")
    return "\n".join(parts)


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

# Note: the former _set_deep_key and _del_deep helpers used to live here.
# They've been removed because Entity.write_var and Entity.remove_var now
# do the equivalent work in logic.py — going through the chokepoint so that
# var hooks fire correctly. The command-layer subcommands (!ent set_var,
# !ent delete_var) now call those methods directly. If you need a raw deep
# set/delete that bypasses hooks for some new purpose, use the existing
# remove_var_silent escape hatch or add a write_var_silent if the need is
# strong enough to justify another escape API.


def _format_vars_blob(vars_dict: dict) -> str:
    if not vars_dict:
        return "**vars**: (empty)"
    try:
        blob = json.dumps(vars_dict, indent=2, ensure_ascii=False)
    except Exception:
        # Fallback if something unserializable sneaks in
        blob = str(vars_dict)
    return "**vars**:\n```json\n" + blob + "\n```"


def _format_passive_line(label: str, p) -> str:
    """One-line summary of a passive for `!passive list` / `!gpassive list`.

    For non-var hooks, omits target/scope (they're meaningless there) for
    a compact display. For var hooks, includes them so the GM can see at
    a glance what each passive watches.
    """
    # Late import to avoid module-level coupling with logic's hook sets
    from logic import VAR_HOOKS
    if p.when in VAR_HOOKS:
        return (f"- `{label}` ({p.when} on `{p.target or '(root)'}` "
                f"scope=`{p.scope}`): `{p.formula}`")
    return f"- `{label}` ({p.when}): `{p.formula}`"


def _parse_clamp_bound(raw: str) -> Any:
    """Parse a clamp bound from a command-line token.

    Strategy: try int, then float, then fall back to treating as a var path
    string. This means '50' → 50, '3.14' → 3.14, 'max_hp' → 'max_hp', and
    'inventory.gold.cap' → 'inventory.gold.cap'. The clamp engine resolves
    string paths against entity vars at write time (see _resolve_clamp_bound
    in logic.py); if the path doesn't exist or isn't numeric, the bound
    gracefully degrades to "no bound."
    """
    s = raw.strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s  # treat as path


def _parse_clamp_args(tokens: List[str]) -> Dict[str, Any]:
    """Parse [max=X] [min=X] [mode=hard|soft] tokens. Returns a dict with
    the named values present. Tokens unrelated to clamps are ignored (lets
    callers chain other args). Unknown clamp-namespace tokens raise."""
    out: Dict[str, Any] = {}
    for tok in tokens:
        if tok.startswith("max="):
            out["max"] = _parse_clamp_bound(tok[len("max="):])
        elif tok.startswith("min="):
            out["min"] = _parse_clamp_bound(tok[len("min="):])
        elif tok.startswith("mode="):
            mode = tok[len("mode="):].lower().strip()
            if mode not in ("soft", "hard"):
                raise VTTError(
                    f"Clamp mode must be 'soft' or 'hard', got '{mode}'."
                )
            out["mode"] = mode
        # Other tokens silently passed through — we don't error on unknown
        # so callers can interleave (e.g. !ent clamp add ... target=... in
        # the future if needed).
    return out


def _format_clamp_line(path: str, c: "ClampSpec") -> str:
    """One-line clamp summary for !clamp list / !gclamp list output."""
    parts = []
    if c.max is not None:
        parts.append(f"max={c.max!r}")
    if c.min is not None:
        parts.append(f"min={c.min!r}")
    parts.append(f"mode={c.mode}")
    return f"- `{path}`: {', '.join(parts)}"


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
        refreshed = mgr.refresh_match_rules(name)
        suffix = f" (refreshed {refreshed} live match{'es' if refreshed != 1 else ''})" if refreshed else ""
        return await ctx.send(f"`{name}`.{key} = {value!r}{suffix}")
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

    # ---- formula substitution ----------------------------------------------
    # args[1] (if present) is treated as the self-target for $() resolution in
    # args[2:]. args[1] may itself be a $() expression — resolved first with
    # self_id=None (self isn't bound yet at that point).
    args = list(args)
    if len(args) >= 2:
        args[1] = resolve_arg_token(args[1], m, self_id=None)

    self_id: Optional[str] = None
    if len(args) >= 2:
        try:
            candidate = _resolve_eid(m, args[1])
            if candidate in m.entities:
                self_id = candidate
        except (NotFound, VTTError):
            pass  # not a real entity reference; self stays None

    for i in range(2, len(args)):
        args[i] = resolve_arg_token(args[i], m, self_id)
    # ---- end formula substitution ------------------------------------------

    # add
    if sub == "add":# and len(args) >= 6:
        if await return_help_if_not_enough_args(ctx, args, 6, "ent", "add"):
            return
        eid, name, hp, x, y = args[1], args[2], int(args[3]), int(args[4]), int(args[5])
        init = int(args[6]) if len(args) >= 7 else None
        # Populate vars using the match's game-system variable names
        hp_var = m.rules.get("hp_var", "hp")
        max_hp_var = m.rules.get("max_hp_var", "max_hp")
        init_var = m.rules.get("turnorder_var", "initiative")
        initial_vars = {hp_var: hp, max_hp_var: hp}
        if init is not None:
            initial_vars[init_var] = init
        e = Entity(id=eid, name=name, x=x, y=y, vars=initial_vars)
        _, spawn_log = e.spawn(m, x, y)
        msg = f"Added `{name}` with id `{eid}` at ({x},{y})."
        if spawn_log:
            msg += "\n" + "\n".join(spawn_log)
        return await ctx.send(msg)

    # --- info (entity card per entity_info_format rule) ---
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "info"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        e = m.entities[eid]
        return await ctx.send(_entity_card(e))

    # --- dump (raw, template-free view of everything stored on the entity) ---
    if sub == "dump":
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "dump"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        return await ctx.send(_entity_dump(m.entities[eid]))

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
        # Optional bypass_clamp arg for overheal effects
        bypass_clamp = False
        for extra in args[3:]:
            if extra.startswith("bypass_clamp="):
                bypass_clamp = _parse_bool(extra[len("bypass_clamp="):])
        e = m.entities[eid]
        if bypass_clamp:
            # Direct write through chokepoint, skipping the heal/damage
            # clamp pipeline. The property setter would re-apply clamps,
            # so we go through write_var explicitly with the bypass flag.
            hp_var, _, _ = e._vital_var_names()
            new_hp = e.hp + delta
            hook_log = e.write_var(hp_var, new_hp, bypass_clamp=True)
            action = "Healed" if delta >= 0 else "Damaged"
            mag = delta if delta >= 0 else -delta
            ack = f"{action} `{eid}` by {mag} (clamp bypassed)."
            if hook_log:
                ack += "\n" + "\n".join(hook_log)
            return await ctx.send(ack)
        if delta >= 0:
            e.heal_entity(delta)
            return await ctx.send(f"Healed `{eid}` by {delta}.")
        else:
            e.damage_entity(-delta)
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
        _, spawn_log = clone.spawn(m, x, y, initiative=src.initiative)
        clone.facing = src.facing

        msg = f"Cloned `{src_id}` → `{new_id}` at ({x},{y})."
        if spawn_log:
            msg += "\n" + "\n".join(spawn_log)
        return await ctx.send(msg)
    # set_var
    if sub == "set_var":
        # Usage: !ent set_var <id> <key> <value> [bypass_clamp=yes]
        # Routes through Entity.write_var so on_var_created / on_var_changed /
        # on_var_written hooks fire for any passives watching this path.
        # bypass_clamp=yes skips clamp evaluation entirely — use for overheal
        # effects, GM debugging, or any deliberate override.
        if await return_help_if_not_enough_args(ctx, args, 4, "ent", "set_var"):
            return

        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")

        key_path = args[2]                # dotted path: e.g., "parts.effects.slow.duration"
        raw_value = args[3]               # use quotes for spaces: "burning aura"
        value = _parse_scalar(raw_value)  # int → float → str

        # Optional bypass_clamp named arg in any trailing position
        bypass_clamp = False
        for extra in args[4:]:
            if extra.startswith("bypass_clamp="):
                bypass_clamp = _parse_bool(extra[len("bypass_clamp="):])

        e = m.entities[eid]
        # write_var returns log lines from any passives that fired in response
        # to the resulting var events. We append them to the ack message so
        # the GM can see chain effects from a single write.
        hook_log = e.write_var(key_path, value, bypass_clamp=bypass_clamp)
        ack = f"`{eid}` vars.{key_path} = {value!r}"
        if bypass_clamp:
            ack += " (clamp bypassed)"
        if hook_log:
            ack += "\n" + "\n".join(hook_log)
        return await ctx.send(ack)
    # delete_var: removes a var and FIRES on_var_removed hooks (plus
    # on_var_written) for every level of the removed subtree (bottom-up).
    if sub == "delete_var":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "delete_var"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        key_path = args[2]
        e = m.entities[eid]
        # Protect vital variables (hp/max_hp/initiative equivalents) from deletion
        top_key = key_path.split(".")[0]
        protected = e.protected_var_names()
        if top_key in protected:
            hp_var, max_hp_var, init_var = e._vital_var_names()
            raise VTTError(
                f"Cannot delete '{key_path}': '{top_key}' is a vital variable "
                f"for the current game system (protected vars: {hp_var}, {max_hp_var}, {init_var})."
            )
        hook_log = e.remove_var(key_path)
        ack = f"Deleted `{eid}` vars.{key_path}"
        if hook_log:
            ack += "\n" + "\n".join(hook_log)
        return await ctx.send(ack)
    # delete_var_silent: escape hatch — removes a var WITHOUT firing any
    # hooks. Use sparingly; passives watching the path won't observe the
    # removal. Useful for cleanup operations where cascading effects would
    # be unwanted.
    if sub == "delete_var_silent":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "delete_var_silent"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        key_path = args[2]
        e = m.entities[eid]
        top_key = key_path.split(".")[0]
        protected = e.protected_var_names()
        if top_key in protected:
            hp_var, max_hp_var, init_var = e._vital_var_names()
            raise VTTError(
                f"Cannot delete '{key_path}': '{top_key}' is a vital variable "
                f"for the current game system (protected vars: {hp_var}, {max_hp_var}, {init_var})."
            )
        e.remove_var_silent(key_path)
        return await ctx.send(f"Deleted (silent) `{eid}` vars.{key_path}")
    

    # Fallback: show authoritative help for the root command
    title, body = registry.help_for(["ent"])
    return await ctx.send(f"**{title}**\n{body}")
#annonate subcommands next to the command itself:
registry.annotate_sub(
    "ent", "info",
    usage="!ent info <id>",
    desc=("Show an entity card per the game system's entity_info_format rule. "
          "For complete raw state regardless of template, use !ent dump."),
)
registry.annotate_sub(
    "ent", "dump",
    usage="!ent dump <id>",
    desc=("Show ALL state stored on an entity (raw, template-free): every "
          "attribute, full vars JSON, all passives. Useful for debugging."),
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
    usage="!ent hp <id> <±n> [bypass_clamp=yes]",
    desc=("Adjust HP by a signed amount; death/prone handled by rules. "
          "Optional bypass_clamp=yes lets a heal exceed max_hp for overheal effects.")
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
    usage="!ent set_var <id> <key> <value> [bypass_clamp=yes]",
    desc=(
        "Set a value in the entity's vars. <key> supports dotted paths (e.g. "
        "'!ent set_var adventurer inventory.sword.damage 10' creates "
        "inventory/sword sub-dicts as needed); value auto-coerces int/float/string. "
        "Fires `on_var_created` for any newly-created path levels and "
        "`on_var_changed` if the value differs from before. The catch-all "
        "`on_var_written` also fires alongside each. "
        "Optional bypass_clamp=yes skips clamp evaluation for this write — use "
        "for overheal effects or deliberate GM overrides."
    ),
)
registry.annotate_sub(
    "ent", "delete_var",
    usage="!ent delete_var <id> <key>",
    desc=(
        "Delete a variable from e.vars and fire `on_var_removed` for every "
        "level of the removed subtree (bottom-up: leaves first). Supports "
        "dotted keys for nested dicts. **Vital variables** (HP/MaxHP/initiative "
        "equivalents defined by the game system) are protected and cannot be "
        "deleted. To remove without firing events, use !ent delete_var_silent."
    ),
)
registry.annotate_sub(
    "ent", "delete_var_silent",
    usage="!ent delete_var_silent <id> <key>",
    desc=(
        "Escape hatch: remove a variable WITHOUT firing any var hooks. Use "
        "sparingly — passives watching the path won't observe the removal. "
        "Useful for cleanup operations where cascading effects would be "
        "unwanted (e.g. resetting an entity between scenarios). Vital "
        "variables are still protected from deletion."
    ),
)

@registry.command("turn", usage="!turn | !turn next | ...", desc="See/advance/set/etc. turns")
async def turn_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if not args:
        order_lines = []
        # Get the initiative var name for display
        init_label = m.rules.get("turnorder_var", "initiative")
        for idx, eid in enumerate(m.turn_order):
            mark = "➡️" if idx == m.active_index else "  "
            e = m.entities.get(eid)
            if e: order_lines.append(f"{mark} `{eid[:8]}` **{e.name}** ({init_label} {e.initiative})")
        return await ctx.send("Turn order:\n" + ("\n".join(order_lines) or "(empty)"))
    sub = args[0].lower()
    if sub == "next":
        eid, fire_log = m.next_turn()
        if not eid: return await ctx.send("No turn order yet.")
        e = m.entities[eid]
        out = f"It is now **{e.name}**'s turn (id `{eid[:8]}`)"
        if fire_log:
            out += "\n" + "\n".join(fire_log)
        return await ctx.send(out)
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
    parts = [
        f"**{m.name}** `{m.id}`",
        f"Game System: **{m.system_name}**",
        f"Current Turn Number: **{m.turn_number}**",
    ]
    if m.global_passives:
        parts.append("")
        parts.append("**Global passives:**")
        for pid, p in m.global_passives.items():
            parts.append(f"- `{pid}` ({p.when}): `{p.formula}`")
    return await ctx.send("\n".join(parts))

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




@registry.command(
    "passive",
    usage="!passive <subcommand> ...",
    desc=("Manage entity-level passives. Each passive fires its formula when "
          "the given hook triggers for the owning entity (or, for on_round_*, "
          "for every entity in turn order)."),
)
async def passive_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["passive"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    if sub == "hooks":
        return await ctx.send(
            "**Passive hooks:** " + ", ".join(f"`{h}`" for h in sorted(HOOK_NAMES))
        )

    if sub == "add":
        # Syntax:
        #   !passive add <eid> <pid> <when> [target=PATH] [scope=exact|children|deep] "<formula>"
        #
        # target/scope are optional and only meaningful for var hooks
        # (on_var_*). For other hooks they're stored but ignored at fire time.
        # If omitted, target defaults to "" and scope to "deep", which for
        # var hooks means "fire on any var event on this entity" (entity-wide).
        if await return_help_if_not_enough_args(ctx, args, 5, "passive", "add"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        pid = args[2]
        when = args[3].lower()

        # Walk args[4:] looking for target=/scope= named args; the rest is
        # the formula. We allow named args in any order, but they must all
        # come before any formula content. This keeps simple cases
        # (no target/scope) backward-compatible with the original syntax.
        target_val = ""
        scope_val = "deep"
        formula_parts: List[str] = []
        for tok in args[4:]:
            if not formula_parts and tok.startswith("target="):
                target_val = tok[len("target="):]
            elif not formula_parts and tok.startswith("scope="):
                scope_val = tok[len("scope="):].lower()
            else:
                formula_parts.append(tok)
        formula = " ".join(formula_parts).strip()

        if not formula:
            raise VTTError("Passive formula cannot be empty.")
        if when not in HOOK_NAMES:
            allowed = ", ".join(sorted(HOOK_NAMES))
            raise VTTError(f"Unknown hook '{when}'. Allowed: {allowed}")
        try:
            validate_program(formula)
        except FormulaError as ex:
            raise VTTError(f"Invalid passive formula: {ex}")
        e = m.entities[eid]
        if pid in e.passives:
            raise DuplicateId(f"Passive '{pid}' already exists on entity '{eid}'.")
        # Passive __post_init__ validates scope value, so a typo here will
        # surface as a clear error rather than silently storing garbage.
        e.passives[pid] = Passive(
            id=pid, when=when, formula=formula,
            target=target_val, scope=scope_val,
        )
        # Mention target/scope in the ack only for var hooks where they apply
        scope_note = ""
        from logic import VAR_HOOKS
        if when in VAR_HOOKS:
            scope_note = f" watching `{target_val or '(root)'}` scope=`{scope_val}`"
        return await ctx.send(f"Added passive `{pid}` ({when}){scope_note} to `{eid}`.")

    if sub in ("remove", "del", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 3, "passive", "remove"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        pid = args[2]
        e = m.entities[eid]
        if pid not in e.passives:
            raise NotFound(f"Passive '{pid}' not found on entity '{eid}'.")
        del e.passives[pid]
        return await ctx.send(f"Removed passive `{pid}` from `{eid}`.")

    if sub == "list":
        # Specific entity
        if len(args) >= 2:
            eid = _resolve_eid(m, args[1])
            if eid not in m.entities:
                raise NotFound(f"Entity '{eid}' not found.")
            e = m.entities[eid]
            if not e.passives:
                return await ctx.send(f"`{eid}` has no passives.")
            lines = [f"**Passives on `{eid}`:**"]
            for pid, p in e.passives.items():
                lines.append(_format_passive_line(pid, p))
            return await ctx.send("\n".join(lines))
        # All entities
        lines = ["**All entity passives in this match:**"]
        any_found = False
        for eid, e in m.entities.items():
            if not e.passives:
                continue
            any_found = True
            for pid, p in e.passives.items():
                lines.append(_format_passive_line(f"{eid}.{pid}", p))
        if not any_found:
            return await ctx.send("No entity passives in this match.")
        return await ctx.send("\n".join(lines))

    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 3, "passive", "info"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        pid = args[2]
        e = m.entities[eid]
        if pid not in e.passives:
            raise NotFound(f"Passive '{pid}' not found on entity '{eid}'.")
        p = e.passives[pid]
        from logic import VAR_HOOKS
        scope_block = ""
        if p.when in VAR_HOOKS:
            scope_block = (
                f"Target: `{p.target or '(root)'}`\n"
                f"Scope: `{p.scope}`\n"
            )
        return await ctx.send(
            f"**Passive `{eid}.{pid}`**\n"
            f"Hook: `{p.when}`\n"
            f"{scope_block}"
            f"Formula:\n```\n{p.formula}\n```"
        )

    title, body = registry.help_for(["passive"])
    return await ctx.send(f"**{title}**\n{body}")

registry.annotate_sub(
    "passive", "add",
    usage='!passive add <entity_id> <passive_id> <when> [target=PATH] [scope=exact|children|deep] "<formula>"',
    desc=(
        "Attach a passive to an entity. <when> must be one of the hook names "
        "(see `!passive hooks`). Formula is run as a program when the hook fires; "
        "inside it, `self` = the owning entity, `this` = current-turn entity. "
        "Quote the formula to preserve spaces. "
        "For VAR HOOKS (on_var_*) target+scope filter which events fire this passive: "
        "target=PATH (e.g. `inventory` or `inventory.sword`; empty=root); "
        "scope=exact (only this exact path), children (one segment deeper), "
        "or deep (this path or any descendant). Defaults: target=root, scope=deep "
        "(catches every var-event on the entity). Inside a var-hook formula you "
        "also have access to: changed_key, old_value, new_value, hook_name."
    ),
)
registry.annotate_sub(
    "passive", "remove",
    usage="!passive remove <entity_id> <passive_id>",
    desc="Remove a passive from an entity. Aliases: del, rm.",
)
registry.annotate_sub(
    "passive", "list",
    usage="!passive list [entity_id]",
    desc="List passives on a specific entity, or across all entities in the match.",
)
registry.annotate_sub(
    "passive", "info",
    usage="!passive info <entity_id> <passive_id>",
    desc="Show full info (hook + formula source) for a single passive.",
)
registry.annotate_sub(
    "passive", "hooks",
    usage="!passive hooks",
    desc="List the available passive hook names.",
)


@registry.command(
    "gpassive",
    usage="!gpassive <subcommand> ...",
    desc=("Manage global (match-level) passives. Each global passive fires once "
          "per entity in turn order on each matching hook event — `self` = the "
          "entity being iterated, `this` = current-turn entity."),
)
async def gpassive_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["gpassive"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    if sub == "hooks":
        return await ctx.send(
            "**Passive hooks:** " + ", ".join(f"`{h}`" for h in sorted(HOOK_NAMES))
        )

    if sub == "add":
        # Syntax:
        #   !gpassive add <pid> <when> [target=PATH] [scope=exact|children|deep] "<formula>"
        #
        # See passive_cmd `add` for the rationale on the named-arg parsing.
        # Globals follow the same per-entity iteration model — for var hooks,
        # the events fire on the entity whose vars changed, and target/scope
        # filter against that event's changed_key.
        if await return_help_if_not_enough_args(ctx, args, 4, "gpassive", "add"):
            return
        pid = args[1]
        when = args[2].lower()

        target_val = ""
        scope_val = "deep"
        formula_parts: List[str] = []
        for tok in args[3:]:
            if not formula_parts and tok.startswith("target="):
                target_val = tok[len("target="):]
            elif not formula_parts and tok.startswith("scope="):
                scope_val = tok[len("scope="):].lower()
            else:
                formula_parts.append(tok)
        formula = " ".join(formula_parts).strip()

        if not formula:
            raise VTTError("Passive formula cannot be empty.")
        if when not in HOOK_NAMES:
            allowed = ", ".join(sorted(HOOK_NAMES))
            raise VTTError(f"Unknown hook '{when}'. Allowed: {allowed}")
        try:
            validate_program(formula)
        except FormulaError as ex:
            raise VTTError(f"Invalid passive formula: {ex}")
        if pid in m.global_passives:
            raise DuplicateId(f"Global passive '{pid}' already exists.")
        m.global_passives[pid] = Passive(
            id=pid, when=when, formula=formula,
            target=target_val, scope=scope_val,
        )
        scope_note = ""
        from logic import VAR_HOOKS
        if when in VAR_HOOKS:
            scope_note = f" watching `{target_val or '(root)'}` scope=`{scope_val}`"
        return await ctx.send(f"Added global passive `{pid}` ({when}){scope_note}.")

    if sub in ("remove", "del", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 2, "gpassive", "remove"):
            return
        pid = args[1]
        if pid not in m.global_passives:
            raise NotFound(f"Global passive '{pid}' not found.")
        del m.global_passives[pid]
        return await ctx.send(f"Removed global passive `{pid}`.")

    if sub == "list":
        if not m.global_passives:
            return await ctx.send("No global passives in this match.")
        lines = ["**Global passives:**"]
        for pid, p in m.global_passives.items():
            lines.append(_format_passive_line(pid, p))
        return await ctx.send("\n".join(lines))

    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "gpassive", "info"):
            return
        pid = args[1]
        if pid not in m.global_passives:
            raise NotFound(f"Global passive '{pid}' not found.")
        p = m.global_passives[pid]
        from logic import VAR_HOOKS
        scope_block = ""
        if p.when in VAR_HOOKS:
            scope_block = (
                f"Target: `{p.target or '(root)'}`\n"
                f"Scope: `{p.scope}`\n"
            )
        return await ctx.send(
            f"**Global passive `{pid}`**\n"
            f"Hook: `{p.when}`\n"
            f"{scope_block}"
            f"Formula:\n```\n{p.formula}\n```"
        )

    title, body = registry.help_for(["gpassive"])
    return await ctx.send(f"**{title}**\n{body}")

registry.annotate_sub(
    "gpassive", "add",
    usage='!gpassive add <passive_id> <when> [target=PATH] [scope=exact|children|deep] "<formula>"',
    desc=(
        "Add a global passive. Fires once per entity in turn order on the given "
        "hook. Inside the formula: `self` = the entity being iterated, "
        "`this` = current-turn entity. "
        "For VAR HOOKS (on_var_*) target+scope filter which events fire this passive: "
        "target=PATH (e.g. `inventory` or `inventory.sword`; empty=root); "
        "scope=exact (fires only on exactly this path), children (one segment "
        "deeper), or deep (this path or any descendant). Defaults: target=root, "
        "scope=deep (catches every var-event)."
    ),
)
registry.annotate_sub(
    "gpassive", "remove",
    usage="!gpassive remove <passive_id>",
    desc="Remove a global passive. Aliases: del, rm.",
)
registry.annotate_sub(
    "gpassive", "list",
    usage="!gpassive list",
    desc="List all global passives in the active match.",
)
registry.annotate_sub(
    "gpassive", "info",
    usage="!gpassive info <passive_id>",
    desc="Show full info (hook + formula source) for a global passive.",
)
registry.annotate_sub(
    "gpassive", "hooks",
    usage="!gpassive hooks",
    desc="List the available passive hook names.",
)


# ==============================================================================
# Clamp CRUD commands
# ==============================================================================
# Clamps constrain numeric writes to vars. Two scopes:
#   - !clamp  (entity-level) — overrides system-level clamps on the same path
#   - !gclamp (system-level) — applies to all entities in matches using the
#                              active match's bound game system
# Both follow the same sub-command shape (add/remove/list).
#
# A clamp spec needs path + at least one of (max, min) + optional mode.
# max/min accept numeric literals OR var path strings — the latter resolves
# dynamically at write time against the entity's vars (so "hp clamped by
# max_hp" works regardless of what max_hp's value is at any given moment).
# Mode is "soft" (default) or "hard" — see ClampSpec docstring for semantics.
# ==============================================================================

@registry.command(
    "clamp",
    usage="!clamp <add|remove|list> ...",
    desc=("Manage entity-scoped clamps. Entity clamps override system-level "
          "default_clamps on the same path. See `!clamp add` for full syntax."),
)
async def clamp_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["clamp"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    if sub == "add":
        # !clamp add <eid> <path> [max=...] [min=...] [mode=hard|soft]
        if await return_help_if_not_enough_args(ctx, args, 4, "clamp", "add"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        path = args[2]
        parsed = _parse_clamp_args(args[3:])
        if "max" not in parsed and "min" not in parsed:
            raise VTTError(
                "Clamp must specify at least one of max=... or min=..."
            )
        # ClampSpec.__post_init__ validates types, min<=max, etc. Errors
        # surface as VTTError to the user.
        spec = ClampSpec(
            path=path,
            max=parsed.get("max"),
            min=parsed.get("min"),
            mode=parsed.get("mode", "soft"),
        )
        e = m.entities[eid]
        if path in e.clamps:
            raise DuplicateId(
                f"Entity '{eid}' already has a clamp on '{path}'. "
                f"Remove it first with !clamp remove, or replace by remove+add."
            )
        e.clamps[path] = spec
        return await ctx.send(
            f"Added clamp on `{eid}.{path}` ({_format_clamp_line(path, spec)})"
        )

    if sub in ("remove", "del", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 3, "clamp", "remove"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        path = args[2]
        e = m.entities[eid]
        if path not in e.clamps:
            raise NotFound(f"No clamp on '{path}' for entity '{eid}'.")
        del e.clamps[path]
        return await ctx.send(f"Removed clamp on `{eid}.{path}`.")

    if sub == "list":
        # Specific entity
        if len(args) >= 2:
            eid = _resolve_eid(m, args[1])
            if eid not in m.entities:
                raise NotFound(f"Entity '{eid}' not found.")
            e = m.entities[eid]
            if not e.clamps:
                return await ctx.send(f"`{eid}` has no entity-level clamps.")
            lines = [f"**Entity clamps on `{eid}`:**"]
            for path, c in e.clamps.items():
                lines.append(_format_clamp_line(path, c))
            return await ctx.send("\n".join(lines))
        # All entities
        any_found = False
        lines = ["**All entity-level clamps in this match:**"]
        for eid, e in m.entities.items():
            if not e.clamps:
                continue
            any_found = True
            for path, c in e.clamps.items():
                lines.append(_format_clamp_line(f"{eid}.{path}", c))
        if not any_found:
            return await ctx.send("No entity-level clamps in this match.")
        return await ctx.send("\n".join(lines))

    raise VTTError(f"Unknown !clamp subcommand: {sub}")


registry.annotate_sub(
    "clamp", "add",
    usage="!clamp add <eid> <path> [max=N|varpath] [min=N|varpath] [mode=soft|hard]",
    desc=(
        "Add an entity-level clamp. <path> is the var being clamped (e.g. "
        "'hp' or 'inventory.gold.coins'). At least one of max/min required; "
        "each can be a numeric literal (e.g. max=100) or a var path string "
        "(e.g. max=max_hp) that resolves dynamically at write time. "
        "Mode defaults to 'soft' (engages only when crossing the bound from "
        "the legal side); 'hard' always enforces. Entity-level clamps "
        "completely override system-level clamps on the same path."
    ),
)
registry.annotate_sub(
    "clamp", "remove",
    usage="!clamp remove <eid> <path>",
    desc="Remove an entity-level clamp. Aliases: del, rm.",
)
registry.annotate_sub(
    "clamp", "list",
    usage="!clamp list [eid]",
    desc="List entity-level clamps for one entity (if eid given) or all.",
)


@registry.command(
    "gclamp",
    usage="!gclamp <add|remove|list> ...",
    desc=("Manage system-level default clamps (stored in the active match's "
          "game system's `default_clamps` rule). Apply to every entity in "
          "matches using this system. See `!gclamp add` for full syntax."),
)
async def gclamp_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["gclamp"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    # Resolve the game system bound to the active match. Edits live on the
    # GameSystem so they persist across saves and apply to other matches
    # using the same system. We also refresh the active match's rules dict
    # so the change takes effect immediately on the current match (other
    # matches will pick it up next time their rules are refreshed; see
    # MatchManager.refresh_match_rules used by !system set).
    sys_name = m.system_name
    sysobj = mgr.get_system(sys_name)

    def _read_clamps() -> List[Dict[str, Any]]:
        """Read the current default_clamps list, copying so we mutate freely."""
        cur = sysobj.settings.get("default_clamps")
        if cur is None:
            # System never explicitly set; fall back to engine default
            return list(DEFAULT_SYSTEM_SETTINGS.get("default_clamps", []))
        return list(cur.value or [])

    def _write_clamps(new_list: List[Dict[str, Any]]) -> int:
        """Persist the new clamp list onto the system and refresh live matches."""
        sysobj.set("default_clamps", new_list)
        return mgr.refresh_match_rules(sys_name)

    if sub == "add":
        # !gclamp add <path> [max=...] [min=...] [mode=hard|soft]
        if await return_help_if_not_enough_args(ctx, args, 3, "gclamp", "add"):
            return
        path = args[1]
        parsed = _parse_clamp_args(args[2:])
        if "max" not in parsed and "min" not in parsed:
            raise VTTError("Clamp must specify at least one of max=... or min=...")
        spec = ClampSpec(
            path=path,
            max=parsed.get("max"),
            min=parsed.get("min"),
            mode=parsed.get("mode", "soft"),
        )
        existing = _read_clamps()
        if any(c.get("path") == path for c in existing):
            raise DuplicateId(
                f"System '{sys_name}' already has a default clamp on '{path}'. "
                f"Remove it first with !gclamp remove."
            )
        existing.append(spec.to_dict())
        refreshed = _write_clamps(existing)
        return await ctx.send(
            f"Added default clamp on `{path}` to system `{sys_name}` "
            f"({_format_clamp_line(path, spec)}) "
            f"[refreshed {refreshed} live match{'es' if refreshed != 1 else ''}]"
        )

    if sub in ("remove", "del", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 2, "gclamp", "remove"):
            return
        path = args[1]
        existing = _read_clamps()
        new_list = [c for c in existing if c.get("path") != path]
        if len(new_list) == len(existing):
            raise NotFound(
                f"No default clamp on '{path}' in system '{sys_name}'."
            )
        refreshed = _write_clamps(new_list)
        return await ctx.send(
            f"Removed default clamp on `{path}` from system `{sys_name}` "
            f"[refreshed {refreshed} live match{'es' if refreshed != 1 else ''}]"
        )

    if sub == "list":
        existing = _read_clamps()
        if not existing:
            return await ctx.send(
                f"System `{sys_name}` has no default clamps."
            )
        lines = [f"**System `{sys_name}` default clamps:**"]
        for cd in existing:
            try:
                spec = ClampSpec.from_dict(cd)
                lines.append(_format_clamp_line(spec.path, spec))
            except VTTError as ex:
                lines.append(f"- ⚠️ malformed: {cd!r} ({ex})")
        return await ctx.send("\n".join(lines))

    raise VTTError(f"Unknown !gclamp subcommand: {sub}")


registry.annotate_sub(
    "gclamp", "add",
    usage="!gclamp add <path> [max=N|varpath] [min=N|varpath] [mode=soft|hard]",
    desc=(
        "Add a system-level default clamp (persists on the GameSystem; "
        "applies to every entity in matches using this system). Syntax "
        "identical to !clamp add but without the <eid> argument."
    ),
)
registry.annotate_sub(
    "gclamp", "remove",
    usage="!gclamp remove <path>",
    desc="Remove a system-level default clamp. Aliases: del, rm.",
)
registry.annotate_sub(
    "gclamp", "list",
    usage="!gclamp list",
    desc="List default clamps for the active match's game system.",
)


@registry.command(
    "eval",
    usage='!eval "<formula>"',
    desc=("Evaluate a formula against the active match (for testing). "
          "`this` = current-turn entity; `self` is unbound here. "
          "Supports assignments and multi-statement bodies; if the source ends with "
          "an expression, its value is returned. Quote the whole formula to preserve spaces."),
)
async def eval_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["eval"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    src = " ".join(args)  # rejoin in case shlex split on internal spaces
    eval_ctx = EvalCtx(this=m.current_entity_id(), target=None)
    val = FormulaEngine(m).eval_program(src, eval_ctx)
    return await ctx.send(f"= `{val!r}`")


# ---- Automated Help command (shows available commands----------------------------------------------------------
@registry.command("help", usage="!help [command [sub]]", desc="Show command usage. Try `!help ent` or `!help ent move`.")
async def help_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    title, body = registry.help_for(args)
    await ctx.send(f"**{title}**\n{body}")