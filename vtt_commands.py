## vtt_commands.py (framework‑agnostic commands + registry)
# vtt_commands.py
from __future__ import annotations
from typing import Callable, Dict, List, Optional, Protocol, Any, Tuple
from logic import MatchManager, Match, Entity, VTTError, OutOfBounds, Occupied, NotFound, DuplicateId, ReservedId, _coerce_rule_value, _parse_bool
from match_history import MatchHistory, Snapshot, HistoryError

#used for Gamesystem-related commands
from logic import (
    DEFAULT_SYSTEM_SETTINGS, ALLOWED_DIRECTIONS, RULE_SCHEMA, RULES_REGISTRY,
    CARDINAL_DIRECTIONS, DIAGONAL_DIRECTIONS,
    normalize_direction, rotate_direction,
)

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
        # Per-command "should the dispatcher take a pre/post snapshot
        # of the active match's state and record a command autosave if
        # the state changed?" Default True for new commands. We turn it
        # off for commands whose mutations are better tracked through a
        # different undo lane (`turn next` snapshots round/turn itself)
        # or that aren't conceptually undoable (`history`, `store`).
        self._snapshot: Dict[str, bool] = {}
    def command(self, name: str, *, usage: Optional[str] = None,
                desc: Optional[str] = None, snapshot: bool = True):
        def deco(fn: Handler):
            self._handlers[name] = fn
            self._snapshot[name] = snapshot
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

        # Pre-dispatch snapshot of the active match's state. We only
        # bother if (a) the command opted into snapshotting (snapshot=
        # True, default), (b) there IS an active match on this channel,
        # and (c) the active match's autosave_command_retention_turns
        # rule isn't 0 (which means "command autosaves disabled").
        pre_state = None
        pre_active_mid = mgr.active_by_channel.get(ctx.channel_key)
        if (self._snapshot.get(name, True)
                and pre_active_mid is not None
                and pre_active_mid in mgr.matches):
            m_pre = mgr.matches[pre_active_mid]
            if int(m_pre.rules.get("autosave_command_retention_turns", 3)) != 0:
                pre_state = m_pre.to_dict(include_history=False)

        try:
            result = await h(ctx, args, mgr)
        except VTTError as e:
            await ctx.send(f"❌ {e}")
            # Command failed — don't record a snapshot for a no-op.
            return
        except Exception as e:
            await ctx.send(f"💥 Unexpected error: {e}")
            return

        # Post-dispatch: if we captured pre-state and the active match
        # still exists and its serialized state genuinely changed, the
        # command was mutating and we record a pre-state snapshot. The
        # snapshot represents "the state to roll back to if you undo
        # this command".
        if pre_state is not None and pre_active_mid in mgr.matches:
            m_post = mgr.matches[pre_active_mid]
            post_state = m_post.to_dict(include_history=False)
            if pre_state != post_state:
                # Build a short, human-meaningful label. The full args
                # list can be very long (multi-line passive formulas);
                # cap it so the !history list output stays readable.
                label = f"!{name} {' '.join(args)}".strip()
                if len(label) > 80:
                    label = label[:77] + "..."
                m_post.history.record_command(m_post, label, pre_state=pre_state)
        return result

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
    whose turn it currently is. Rejects `group:NAME` tokens — those
    must be handled by the calling subcommand via _resolve_targets
    (or refused with _reject_group_token for commands like tp/clone
    that don't have sensible group semantics).
    """
    if isinstance(token, str) and token.startswith("group:"):
        raise VTTError(
            f"This subcommand does not accept a group target "
            f"({token!r}). Provide a single entity id instead."
        )
    t = str(token).strip().lower()
    if t in {"current", "this"}:
        eid = m.current_entity_id()
        if not eid:
            raise NotFound("No current entity (turn order is empty).")
        return eid
    return token


def _resolve_targets(m, token: str) -> List[str]:
    """
    Resolve a target token to a list of entity ids. Accepts:
      - a literal entity id              → [id]
      - "current" / "this"               → [active-turn id]
      - "group:NAME"                     → all members of that group,
                                           in insertion order (may be
                                           empty if the group exists
                                           but has no members)
    Raises NotFound if the group doesn't exist. Caller decides what
    to do with an empty result (typically: send a benign "no-op"
    message rather than erroring, since deleting all members from a
    group is a reasonable mid-game state).
    """
    if isinstance(token, str) and token.startswith("group:"):
        name = token[len("group:"):]
        if not name:
            raise VTTError("Group target must be 'group:NAME' (name is empty).")
        return m.group_members(name)
    return [_resolve_eid(m, token)]


def _reject_group_token(token: str, subcommand: str) -> None:
    """Raise a clear error if a subcommand was handed a group: token
    but doesn't support groups. Used by tp / rename / clone where
    the operation is fundamentally per-entity (a unique target cell,
    a single new name, a single new id)."""
    if isinstance(token, str) and token.startswith("group:"):
        raise VTTError(
            f"!ent {subcommand} doesn't accept a group target — it "
            f"operates on a single entity. Got {token!r}."
        )




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

# ---- group fan-out helpers -------------------------------------------------
# Subcommands of !ent that sensibly iterate one-at-a-time over group members.
# !ent move is excluded because group movement is atomic (see Match.move_group_dirs).
# add/tp/rename/clone are excluded because they take a per-call unique target
# (a new id, a single destination cell, a single new name).
_ENT_GROUP_ITERABLE_SUBS = {
    "info", "dump", "remove", "del", "rm", "face",
    "hp", "init", "set_var", "delete_var", "delete_var_silent",
}
_ENT_GROUP_REJECTING_SUBS = {"add", "tp", "rename", "clone"}


async def _ent_group_dispatch(ctx, args, mgr, m, sub: str):
    """Handle a `group:NAME` target on the !ent command."""
    if sub in _ENT_GROUP_REJECTING_SUBS:
        return await ctx.send(
            f"❌ !ent {sub} doesn't accept a group target — it operates on "
            f"a single entity. Got {args[1]!r}."
        )
    if sub == "move":
        return await _ent_move_group(ctx, args, mgr, m)
    if sub in _ENT_GROUP_ITERABLE_SUBS:
        group_name = args[1][len("group:"):]
        try:
            targets = m.group_members(group_name)
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        if not targets:
            return await ctx.send(f"Group `{group_name}` is empty; no-op.")
        # Per-member dispatch via recursion. Each iteration resolves its own
        # formula substitutions (self_id = the iterated member). Per-entity
        # errors are surfaced but don't abort the rest of the iteration —
        # one bad apple shouldn't silently drop the others.
        for eid in targets:
            sub_args = list(args)
            sub_args[1] = eid
            try:
                await ent_cmd(ctx, sub_args, mgr)
            except VTTError as ex:
                await ctx.send(f"⚠️ `{eid}`: {ex}")
        return
    return await ctx.send(
        f"❌ !ent {sub} either doesn't support group targets, or isn't a "
        f"recognized subcommand."
    )


async def _ent_move_group(ctx, args, mgr, m):
    """Atomic !ent move group:NAME ..."""
    if await return_help_if_not_enough_args(ctx, args, 3, "ent", "move"):
        return
    group_name = args[1][len("group:"):]
    try:
        members = m.group_members(group_name)
    except NotFound as ex:
        return await ctx.send(f"❌ {ex}")
    if not members:
        return await ctx.send(f"Group `{group_name}` is empty; no-op.")
    # Reuse the same direction-token parser as the single-entity move.
    tokens = " ".join(args[2:]).replace(",", " ").split()
    if not tokens:
        title, body = registry.help_for(["ent", "move"])
        return await ctx.send(f"**{title}**\n{body}")
    moves: list[tuple[str, int]] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if normalize_direction(t) is not None:
            moves.append((t, 1)); i += 1
        else:
            try:
                n = int(t)
            except ValueError:
                return await ctx.send(f"Unexpected token '{t}'.")
            if i + 1 >= len(tokens):
                return await ctx.send("Count must be followed by a direction.")
            d = tokens[i + 1]
            if normalize_direction(d) is None:
                return await ctx.send(f"'{d}' is not a direction.")
            moves.append((d, n)); i += 2
    try:
        count, steps, hook_log = m.move_group_dirs(group_name, moves)
    except VTTError as ex:
        return await ctx.send(f"❌ {ex}")
    msg = (
        f"Moved group `{group_name}` ({count} "
        f"{'members' if count != 1 else 'member'}, {steps} step(s) each)."
    )
    if hook_log:
        msg = msg + "\n" + "\n".join(hook_log)
    return await ctx.send(msg)


async def _ent_group_subcmd(ctx, args, mgr, m):
    """Handle !ent group <action> ... — group registry management."""
    # args[0] is "group"; the action lives at args[1].
    if len(args) < 2:
        title, body = registry.help_for(["ent", "group"])
        return await ctx.send(f"**{title}**\n{body}")
    action = args[1].lower()

    if action == "list":
        if not m.groups:
            return await ctx.send("No groups defined in this match.")
        lines = ["**Groups in this match:**"]
        for name, members in m.groups.items():
            lines.append(f"- `{name}`: {len(members)} member(s)")
        return await ctx.send("\n".join(lines))

    if action == "info":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "group"):
            return
        name = args[2]
        try:
            members = m.group_members(name)
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        if not members:
            return await ctx.send(f"Group `{name}` exists but has no members.")
        lines = [f"**Group `{name}`** ({len(members)} member(s)):"]
        for eid in members:
            lines.append(f"- `{eid}` — {m.entities[eid].name}")
        return await ctx.send("\n".join(lines))

    if action == "new":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "group"):
            return
        name = args[2]
        if m.has_group(name):
            raise DuplicateId(f"Group '{name}' already exists.")
        # Create an empty group by ensuring the key exists. We sidestep
        # add_to_group (which requires an entity id) so empty groups are
        # explicitly representable.
        m.groups[name] = []
        return await ctx.send(f"Created empty group `{name}`.")

    if action == "add":
        if await return_help_if_not_enough_args(ctx, args, 4, "ent", "group"):
            return
        name = args[2]
        added: List[str] = []
        already: List[str] = []
        for raw in args[3:]:
            eid = _resolve_eid(m, raw)
            if eid not in m.entities:
                raise NotFound(f"Entity '{eid}' not found.")
            if m.add_to_group(name, eid):
                added.append(eid)
            else:
                already.append(eid)
        parts = [f"Group `{name}`:"]
        if added:
            parts.append(f"added {', '.join(f'`{e}`' for e in added)}")
        if already:
            parts.append(f"already members: {', '.join(f'`{e}`' for e in already)}")
        return await ctx.send(" ".join(parts))

    if action in ("remove", "rm", "del"):
        if await return_help_if_not_enough_args(ctx, args, 4, "ent", "group"):
            return
        name = args[2]
        if not m.has_group(name):
            raise NotFound(f"Group '{name}' not found.")
        removed: List[str] = []
        not_in: List[str] = []
        for raw in args[3:]:
            eid = _resolve_eid(m, raw)
            if m.remove_from_group(name, eid):
                removed.append(eid)
            else:
                not_in.append(eid)
        parts = [f"Group `{name}`:"]
        if removed:
            parts.append(f"removed {', '.join(f'`{e}`' for e in removed)}")
        if not_in:
            parts.append(f"not members: {', '.join(f'`{e}`' for e in not_in)}")
        return await ctx.send(" ".join(parts))

    if action == "delete":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "group"):
            return
        name = args[2]
        m.delete_group(name)
        return await ctx.send(f"Deleted group `{name}`.")

    # Fallback: show authoritative help for !ent group
    title, body = registry.help_for(["ent", "group"])
    return await ctx.send(f"**{title}**\n{body}")


@registry.command("ent", usage="!ent <subcommand> ...", desc="Manage entities in the active match, lots of available sub-commands. Note: <id> parameter also accepts 'this' or 'current', to target the entity whose turn it is right now")
async def ent_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    # Show authoritative help if no subcommand was given
    if not args:
        # if no subcommand: just show the authoritative help for !ent
        title, body = registry.help_for(["ent"])
        return await ctx.send(f"**{title}**\n{body}")

    m = active_match(mgr, ctx)

    sub = args[0].lower()#makes the first arg lowercase, so "ADD", "aDD", etc. become "add"

    # ---- group management subcommand ---------------------------------------
    # `!ent group <action> ...` manages the match's named entity groups.
    # Handled before formula substitution because the leading sub-action
    # ("new"/"add"/"remove"/...) isn't an entity id and shouldn't go through
    # the self-id resolver.
    if sub == "group":
        return await _ent_group_subcmd(ctx, args, mgr, m)

    # ---- group:NAME fan-out ------------------------------------------------
    # If the target slot is `group:NAME`, dispatch through the group path:
    #   - "move" gets atomic-all-or-nothing semantics via Match.move_group_dirs
    #   - subcommands that iterate cleanly (info/dump/remove/face/hp/init/
    #     set_var/delete_var/delete_var_silent) fan out one call per member
    #   - subcommands that need a unique per-call target (add/tp/rename/clone)
    #     reject the group token with a clear error
    # Formula substitution is deferred to the per-member recursive call so
    # $(entity[self].x) resolves freshly for each member rather than once
    # against an unbound self.
    if len(args) >= 2 and isinstance(args[1], str) and args[1].startswith("group:"):
        return await _ent_group_dispatch(ctx, args, mgr, m, sub)

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
        hook_log = m.entities[eid].tp(x, y)
        msg = f"Teleported `{eid}` to ({x},{y})."
        if hook_log:
            msg = msg + "\n" + "\n".join(hook_log)
        return await ctx.send(msg)

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

        # The parser accepts any alias normalize_direction handles
        # (cardinal + diagonal + compass + 1-2 letter forms). The
        # diagonal rule check lives in Entity.move_dirs — we just need
        # to recognize tokens here. A token is a direction if it
        # normalizes; otherwise it's a count to be followed by one.
        moves: list[tuple[str,int]] = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if normalize_direction(t) is not None:
                moves.append((t, 1)); i += 1
            else:
                try: n = int(t)
                except ValueError:
                    return await ctx.send(f"Unexpected token '{t}'.")
                if i + 1 >= len(tokens): return await ctx.send("Count must be followed by a direction.")
                d = tokens[i+1]
                if normalize_direction(d) is None:
                    return await ctx.send(f"'{d}' is not a direction.")
                moves.append((d, n)); i += 2

        total_steps = sum(max(1, int(n)) for _, n in moves)
        try:
            hook_log = m.entities[eid].move_dirs(moves)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        e = m.entities[eid]
        msg = f"Moved `{eid}` {total_steps} step(s) to ({e.x},{e.y}); facing {e.facing}."
        if hook_log:
            msg = msg + "\n" + "\n".join(hook_log)
        return await ctx.send(msg)

    # face — accepts either a direction (any alias) or a rotation token
    # (cw/ccw/clockwise/counterclockwise). Diagonal direction tokens are
    # gated by the allow_diagonal_facing rule; rotation tokens always
    # work, but their step size is 45° (8-way) or 90° (4-way) depending
    # on that same rule.
    if sub == "face":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "face"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        e = m.entities[eid]
        raw = args[2].strip().lower()
        eight_way_face = bool(m.rules.get("allow_diagonal_facing", False))

        rotation_aliases_cw = {"cw", "clockwise", "right_turn", "turn_right"}
        rotation_aliases_ccw = {"ccw", "counterclockwise", "anticlockwise",
                                "left_turn", "turn_left"}
        if raw in rotation_aliases_cw or raw in rotation_aliases_ccw:
            clockwise = raw in rotation_aliases_cw
            new_facing = rotate_direction(
                e.facing, clockwise=clockwise, eight_way=eight_way_face,
            )
            e.facing = new_facing
            label = "clockwise" if clockwise else "counterclockwise"
            return await ctx.send(
                f"Rotated `{eid}` {label}; now facing {new_facing}."
            )

        canon = normalize_direction(raw)
        if canon is None:
            return await ctx.send(
                "Use: up/down/left/right (or up_left/up_right/down_left/"
                "down_right when allow_diagonal_facing is enabled), or "
                "cw/ccw to rotate."
            )
        if canon in DIAGONAL_DIRECTIONS and not eight_way_face:
            raise VTTError(
                f"Diagonal facing '{raw}' is not allowed by the active "
                f"game system. Enable rule 'allow_diagonal_facing' to "
                f"permit it."
            )
        e.facing = canon
        return await ctx.send(f"Facing of `{eid}` set to {canon}.")

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
    "ent", "group",
    usage="!ent group <list|info|new|add|remove|delete> ...",
    desc=(
        "Manage named entity groups in the active match. An entity can "
        "belong to any number of groups; group membership is stored "
        "separately from entity vars. Once a group exists, most !ent "
        "subcommands accept `group:NAME` as the target to fan out across "
        "all members (`!ent hp group:swarm 10` heals everyone). "
        "!ent move on a group is atomic — if any member can't complete "
        "the move, nobody moves; fellow group members are treated as "
        "transparent during path validation. Actions: "
        "`list` (all groups), "
        "`info <name>` (members of one group), "
        "`new <name>` (create an empty group), "
        "`add <name> <eid> [eid ...]` (add entities), "
        "`remove <name> <eid> [eid ...]` (remove entities; aliases rm/del), "
        "`delete <name>` (drop the group entirely; members are unaffected)."
    ),
)
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
    usage="!ent move <id|group:NAME> <dir[,dir...]> | !ent move <id|group:NAME> <n> <dir> [<n> <dir> ...]",
    desc=(
        "Stepwise move; final cell must be free. Directions: up/down/left/"
        "right (aliases u/d/l/r, n/s/w/e). Diagonals up_left/up_right/"
        "down_left/down_right (aliases ul/ur/dl/dr, nw/ne/sw/se, or "
        "hyphen/no-separator variants like 'up-left' / 'upleft') are only "
        "accepted when the system rule 'allow_diagonal_movement' is True. "
        "With a `group:NAME` target, every member moves through the same "
        "sequence atomically: fellow members are invisible to the occupancy "
        "check (so a marching group doesn't trip on its own footprint), "
        "and if any member would leave the grid or hit a non-member "
        "obstacle, NO entity moves."
    ),
)
registry.annotate_sub(
    "ent", "face",
    usage="!ent face <id> <dir|cw|ccw>",
    desc=(
        "Set or rotate facing. <dir> accepts the same direction aliases as "
        "!ent move (cardinals always; diagonals only when "
        "'allow_diagonal_facing' is enabled). Use 'cw'/'clockwise' or "
        "'ccw'/'counterclockwise' to rotate one step — 90° in cardinal-"
        "only systems, 45° when diagonal facing is enabled."
    ),
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

@registry.command("turn", usage="!turn | !turn next | ...", desc="See/advance/set/etc. turns", snapshot=False)
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
        return await ctx.send(f"Active turn set to `{eid}` (round {m.round_number})")
    # Fallback: show authoritative help
    title, body = registry.help_for(["turn"])
    return await ctx.send(f"**{title}**\n{body}")
#annonate subcommands next to the command itself:
registry.annotate_sub(
    "turn", "next",
    usage="!turn next",
    desc=("Advance to the next entity's turn. When the cycle wraps "
          "(every entity has acted), increments the match's round_number "
          "and fires on_round_end / on_round_start hooks.")
)
registry.annotate_sub(
    "turn", "set",
    usage="!turn set <id>",
    desc="Set the current turn to be the turn of entity <id>"
)


#global info about the match that isn't the map or entities
@registry.command("match_toplevel", usage="!match_toplevel", desc="Show active match summary (name/id/round number).")
async def match_top_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    parts = [
        f"**{m.name}** `{m.id}`",
        f"Game System: **{m.system_name}**",
        f"Current Round Number: **{m.round_number}**",
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

# ---- !history -----------------------------------------------------------
# Per-match save history: manual saves, automatic round/turn/command
# snapshots, undo, and JSON export/import. snapshot=False so this command
# doesn't try to autosnapshot ITSELF — managing the history shouldn't
# clutter the history.
def _restore_snapshot(mgr: MatchManager, mid: str, snapshot: Snapshot) -> Match:
    """Replace mgr.matches[mid] with a Match restored from `snapshot`.

    The previous Match's MatchHistory is moved onto the new Match and
    truncated to drop autosaves with sequence > snapshot.sequence —
    those describe a timeline the user just abandoned. Manual saves
    survive (they're explicit bookmarks; the user owns their lifecycle).
    Returns the new Match instance.
    """
    if mid not in mgr.matches:
        raise NotFound(f"Match '{mid}' no longer exists.")
    old = mgr.matches[mid]
    new_match = Match.from_dict(snapshot.state)
    # Transfer history pointer first so the truncate below targets the
    # same object we just attached to the new match.
    new_match.history = old.history
    new_match.history.truncate_after(snapshot)
    mgr.matches[mid] = new_match
    return new_match


def _parse_int_or(default: int, token: Optional[str]) -> int:
    """Parse `token` as a positive int, falling back to `default` for
    None / non-numeric. Used to make `!history undo turn` and
    `!history undo turn 3` both work without the `confirm` token
    confusing the parser."""
    if token is None:
        return default
    try:
        return int(token)
    except (TypeError, ValueError):
        return default


def _confirmation_required(threshold: int, n: int) -> bool:
    """A threshold of -1 disables prompting; anything else compares
    against `n` and prompts when n >= threshold."""
    return threshold >= 0 and n >= threshold


def _confirm_message(action_label: str, n: int, scope: str,
                     re_run_args: str, removed_estimate: int) -> str:
    """Standard 'are you sure?' message body. Pulled out so all the
    undo subcommands give a consistent prompt."""
    return (
        f"⚠️ {action_label} would undo **{n} {scope}**. This will "
        f"discard the {removed_estimate} autosave(s) taken after that "
        f"restore point.\n"
        f"• To proceed:  `!history undo {re_run_args} confirm`\n"
        f"• To bookmark current state first:  "
        f"`!history save <name>`  then re-run the undo command."
    )


@registry.command(
    "history",
    usage=("!history <list|save|delete|restore|undo|export|import> ..."),
    desc=(
        "Per-match save & undo system. Three flavors of autosave are "
        "taken automatically — round-start (after on_round_start hooks), "
        "turn-start (after on_turn_start hooks), and pre-command (before "
        "each mutating command). Plus manual saves (`!history save "
        "<name>`) which never auto-prune. Retention and "
        "confirmation thresholds are set on the active game system "
        "via the `autosave_*` and `undo_confirmation_*` rules."
    ),
    snapshot=False,
)
async def history_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["history"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    mid = m.id
    sub = args[0].lower()

    # ---- list --------------------------------------------------------
    # `!history list` is the summary view. `!history list <kind>` shows
    # detail for one kind. The summary is intentionally compact (one
    # line per kind) because in a long match the lists can be huge.
    if sub == "list":
        if len(args) >= 2:
            kind = args[1].lower()
            if kind in ("rounds", "round"):
                if not m.history.round_saves:
                    return await ctx.send("No round autosaves retained.")
                lines = ["**Round autosaves:**"]
                for s in m.history.round_saves:
                    lines.append(f"- seq {s.sequence}: {s.short_summary()}  [{s.timestamp}]")
                return await ctx.send("\n".join(lines))
            if kind in ("turns", "turn"):
                if not m.history.turn_saves:
                    return await ctx.send("No turn autosaves retained.")
                lines = ["**Turn autosaves:**"]
                for s in m.history.turn_saves:
                    lines.append(f"- seq {s.sequence}: {s.short_summary()}  [{s.timestamp}]")
                return await ctx.send("\n".join(lines))
            if kind in ("commands", "command"):
                if not m.history.command_saves:
                    return await ctx.send("No command autosaves retained.")
                lines = ["**Command autosaves:**"]
                for s in m.history.command_saves:
                    lines.append(f"- seq {s.sequence}: {s.short_summary()}  [{s.timestamp}]")
                return await ctx.send("\n".join(lines))
            if kind in ("manual", "manuals"):
                if not m.history.manual_saves:
                    return await ctx.send("No manual saves.")
                lines = ["**Manual saves:**"]
                for name, s in m.history.manual_saves.items():
                    lines.append(f"- `{name}`: {s.short_summary()}  [{s.timestamp}]")
                return await ctx.send("\n".join(lines))
            return await ctx.send(
                f"Unknown list kind '{kind}'. Use one of: rounds, turns, commands, manual."
            )
        # Summary
        lines = [f"**Match `{mid}` history:**"]
        if m.history.round_saves:
            rounds = sorted({s.round_at_snapshot for s in m.history.round_saves})
            rng = f"{rounds[0]}–{rounds[-1]}" if len(rounds) > 1 else str(rounds[0])
            lines.append(f"- Rounds: {len(m.history.round_saves)} snapshot(s) (round {rng})")
        else:
            lines.append("- Rounds: none")
        if m.history.turn_saves:
            lines.append(f"- Turns: {len(m.history.turn_saves)} snapshot(s)")
        else:
            lines.append("- Turns: none")
        if m.history.command_saves:
            lines.append(f"- Commands: {len(m.history.command_saves)} snapshot(s)")
        else:
            lines.append("- Commands: none")
        if m.history.manual_saves:
            names = ", ".join(f"`{n}`" for n in m.history.manual_saves)
            lines.append(f"- Manual: {len(m.history.manual_saves)} ({names})")
        else:
            lines.append("- Manual: none")
        lines.append("Use `!history list <rounds|turns|commands|manual>` for detail.")
        return await ctx.send("\n".join(lines))

    # ---- save / delete / restore (manual) ----------------------------
    if sub == "save":
        if await return_help_if_not_enough_args(ctx, args, 2, "history", "save"):
            return
        name = args[1]
        s = m.history.save_manual(m, name)
        return await ctx.send(
            f"Saved manual `{name}` (round {s.round_at_snapshot}, "
            f"seq {s.sequence})."
        )

    if sub == "delete":
        if await return_help_if_not_enough_args(ctx, args, 2, "history", "delete"):
            return
        name = args[1]
        m.history.delete_manual(name)
        return await ctx.send(f"Deleted manual save `{name}`.")

    if sub == "restore":
        # Manual restore is a "big jump" — always confirms. The
        # threshold rules apply to undo-by-distance, not manual jumps.
        if await return_help_if_not_enough_args(ctx, args, 2, "history", "restore"):
            return
        name = args[1]
        confirmed = "confirm" in (a.lower() for a in args[2:])
        snap = m.history.get_manual(name)
        if not confirmed:
            # Estimate "what would be erased": any autosave with seq >
            # snap.sequence. Manual saves are preserved regardless.
            erased = (sum(1 for x in m.history.round_saves if x.sequence > snap.sequence)
                      + sum(1 for x in m.history.turn_saves if x.sequence > snap.sequence)
                      + sum(1 for x in m.history.command_saves if x.sequence > snap.sequence))
            return await ctx.send(
                f"⚠️ Restoring manual `{name}` will jump to round "
                f"{snap.round_at_snapshot} and discard {erased} autosave(s) "
                f"taken after that point.\n"
                f"• To proceed:  `!history restore {name} confirm`"
            )
        _restore_snapshot(mgr, mid, snap)
        return await ctx.send(
            f"Restored manual `{name}` (now at round {snap.round_at_snapshot})."
        )

    # ---- undo --------------------------------------------------------
    # Subcommands: undo turn [N] [confirm], undo round [N] [confirm],
    # undo command [N] [confirm], undo to round X [confirm].
    if sub == "undo":
        if len(args) < 2:
            title, body = registry.help_for(["history", "undo"])
            return await ctx.send(f"**{title}**\n{body}")
        scope = args[1].lower()

        # ---- undo to round X ----
        if scope == "to":
            # Form: !history undo to round X [confirm]
            if len(args) < 4 or args[2].lower() not in ("round", "rounds"):
                return await ctx.send(
                    "Usage: `!history undo to round <X> [confirm]`."
                )
            try:
                target_round = int(args[3])
            except ValueError:
                return await ctx.send(f"Round number must be an integer, got '{args[3]}'.")
            confirmed = "confirm" in (a.lower() for a in args[4:])
            snap = m.history.get_round_with_number(target_round)
            # `undo to round X` always prompts unless the round threshold
            # is disabled (-1). Conceptually it's a non-linear jump so
            # we treat it like a round undo of "however many rounds away."
            distance = max(1, m.round_number - target_round)
            threshold = int(m.rules.get("undo_confirmation_round_threshold", 1))
            if _confirmation_required(threshold, distance) and not confirmed:
                erased = sum(1 for x in m.history.round_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.turn_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.command_saves if x.sequence > snap.sequence)
                return await ctx.send(_confirm_message(
                    "Jump to round start", distance, "rounds back",
                    f"to round {target_round}", erased,
                ))
            _restore_snapshot(mgr, mid, snap)
            return await ctx.send(f"Restored start of round {target_round}.")

        # ---- undo {turn|round|command} [N] [confirm] ----
        if scope in ("turn", "turns"):
            n = _parse_int_or(1, args[2] if len(args) >= 3 else None)
            confirmed = "confirm" in (a.lower() for a in args[2:])
            snap = m.history.get_turn_nth_back(n)
            threshold = int(m.rules.get("undo_confirmation_turn_threshold", 3))
            if _confirmation_required(threshold, n) and not confirmed:
                erased = sum(1 for x in m.history.round_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.turn_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.command_saves if x.sequence > snap.sequence)
                return await ctx.send(_confirm_message(
                    "Undoing turns", n, "turn(s)",
                    f"turn {n}", erased,
                ))
            _restore_snapshot(mgr, mid, snap)
            return await ctx.send(
                f"Undid {n} turn(s). Now at start of "
                f"{snap.active_entity_id or 'no-active'}'s turn "
                f"(round {snap.round_at_snapshot})."
            )

        if scope in ("round", "rounds"):
            n = _parse_int_or(1, args[2] if len(args) >= 3 else None)
            confirmed = "confirm" in (a.lower() for a in args[2:])
            snap = m.history.get_round_nth_back(n)
            threshold = int(m.rules.get("undo_confirmation_round_threshold", 1))
            if _confirmation_required(threshold, n) and not confirmed:
                erased = sum(1 for x in m.history.round_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.turn_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.command_saves if x.sequence > snap.sequence)
                return await ctx.send(_confirm_message(
                    "Undoing rounds", n, "round(s)",
                    f"round {n}", erased,
                ))
            _restore_snapshot(mgr, mid, snap)
            return await ctx.send(
                f"Undid {n} round(s). Now at start of round "
                f"{snap.round_at_snapshot}."
            )

        if scope in ("command", "commands"):
            n = _parse_int_or(1, args[2] if len(args) >= 3 else None)
            confirmed = "confirm" in (a.lower() for a in args[2:])
            snap = m.history.get_command_nth_back(n)
            threshold = int(m.rules.get("undo_confirmation_command_threshold", -1))
            if _confirmation_required(threshold, n) and not confirmed:
                erased = sum(1 for x in m.history.round_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.turn_saves if x.sequence > snap.sequence) + \
                         sum(1 for x in m.history.command_saves if x.sequence > snap.sequence)
                return await ctx.send(_confirm_message(
                    "Undoing commands", n, "command(s)",
                    f"command {n}", erased,
                ))
            _restore_snapshot(mgr, mid, snap)
            return await ctx.send(
                f"Undid {n} command(s). Reverted past `{snap.label}`."
            )

        return await ctx.send(
            f"Unknown undo scope '{scope}'. Use turn/round/command/to."
        )

    # ---- export ------------------------------------------------------
    # `!history export <selector> <path>` writes one snapshot to JSON.
    # Selectors:
    #   manual:<name>      named manual save
    #   round:<N>          round-start snapshot for round N
    #   turn:<seq>         turn-start snapshot by sequence number
    #   command:<seq>      command snapshot by sequence number
    #   latest:round       most recent round save
    #   latest:turn        most recent turn save
    #   latest:command     most recent command save
    if sub == "export":
        if await return_help_if_not_enough_args(ctx, args, 3, "history", "export"):
            return
        selector = args[1]
        path = args[2]
        snap = _resolve_snapshot_selector(m, selector)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(snap.to_dict(), f, indent=2)
        except OSError as ex:
            raise VTTError(f"Failed to write '{path}': {ex}")
        return await ctx.send(
            f"Exported {snap.kind} snapshot (seq {snap.sequence}) to `{path}`."
        )

    # ---- import ------------------------------------------------------
    # `!history import <path> [as <name>]` reads a snapshot JSON and
    # adds it to manual_saves. `as <name>` overrides the imported
    # label; if omitted we keep the snapshot's existing label, or
    # fall back to "imported-<seq>" if the label is empty.
    if sub == "import":
        if await return_help_if_not_enough_args(ctx, args, 2, "history", "import"):
            return
        path = args[1]
        override_name = None
        if len(args) >= 4 and args[2].lower() == "as":
            override_name = args[3]
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = Snapshot.from_dict(json.load(f))
        except FileNotFoundError:
            raise VTTError(f"File not found: '{path}'")
        except (OSError, json.JSONDecodeError, KeyError) as ex:
            raise VTTError(f"Failed to import from '{path}': {ex}")
        name = override_name or snap.label or f"imported-{snap.sequence}"
        # The imported snapshot keeps its OLD sequence number, which
        # could collide with the current match's sequence space. That's
        # fine for the manual_saves dict (keyed by name, not seq), but
        # we re-stamp the kind to "manual" so a later !history list
        # categorizes it correctly.
        snap.kind = "manual"
        snap.label = name
        m.history.manual_saves[name] = snap
        return await ctx.send(
            f"Imported snapshot from `{path}` as manual save `{name}` "
            f"(round {snap.round_at_snapshot})."
        )

    # Fallback to help
    title, body = registry.help_for(["history"])
    return await ctx.send(f"**{title}**\n{body}")


def _resolve_snapshot_selector(m: Match, selector: str) -> Snapshot:
    """Map a selector string to a specific Snapshot, or raise."""
    if ":" not in selector:
        raise VTTError(
            f"Selector '{selector}' must be like manual:<name>, round:<N>, "
            f"turn:<seq>, command:<seq>, or latest:<kind>."
        )
    kind, _, value = selector.partition(":")
    kind = kind.lower()
    if kind == "manual":
        return m.history.get_manual(value)
    if kind == "latest":
        v = value.lower()
        if v == "round":
            if not m.history.round_saves:
                raise HistoryError("No round snapshots to export.")
            return m.history.round_saves[-1]
        if v == "turn":
            if not m.history.turn_saves:
                raise HistoryError("No turn snapshots to export.")
            return m.history.turn_saves[-1]
        if v == "command":
            if not m.history.command_saves:
                raise HistoryError("No command snapshots to export.")
            return m.history.command_saves[-1]
        raise VTTError(f"latest:<kind> expects round/turn/command, got '{value}'.")
    # round:<N> looks up by round_at_snapshot
    if kind == "round":
        try:
            n = int(value)
        except ValueError:
            raise VTTError(f"round:<N> expects an integer, got '{value}'.")
        return m.history.get_round_with_number(n)
    # turn / command lookups use sequence number for unambiguous reference
    if kind == "turn":
        try:
            seq = int(value)
        except ValueError:
            raise VTTError(f"turn:<seq> expects an integer, got '{value}'.")
        for s in m.history.turn_saves:
            if s.sequence == seq:
                return s
        raise HistoryError(f"No turn snapshot with sequence {seq}.")
    if kind == "command":
        try:
            seq = int(value)
        except ValueError:
            raise VTTError(f"command:<seq> expects an integer, got '{value}'.")
        for s in m.history.command_saves:
            if s.sequence == seq:
                return s
        raise HistoryError(f"No command snapshot with sequence {seq}.")
    raise VTTError(f"Unknown selector prefix '{kind}'.")


# Annotate the subcommands for help
registry.annotate_sub(
    "history", "list",
    usage="!history list [rounds|turns|commands|manual]",
    desc=(
        "Without an argument: one-line summary of each save category. "
        "With a category argument: detailed listing of that category."
    ),
)
registry.annotate_sub(
    "history", "save",
    usage="!history save <name>",
    desc=(
        "Create or overwrite a named manual save bookmarked at the "
        "current state. Manual saves never auto-prune; delete them "
        "explicitly with `!history delete <name>`."
    ),
)
registry.annotate_sub(
    "history", "delete",
    usage="!history delete <name>",
    desc="Drop a manual save by name."
)
registry.annotate_sub(
    "history", "restore",
    usage="!history restore <name> [confirm]",
    desc=(
        "Replace the active match state with the named manual save's "
        "state. Always requires `confirm` because the autosaves taken "
        "after the manual save's point will be erased."
    ),
)
registry.annotate_sub(
    "history", "undo",
    usage=(
        "!history undo turn [N] [confirm] | !history undo round [N] "
        "[confirm] | !history undo command [N] [confirm] | "
        "!history undo to round <X> [confirm]"
    ),
    desc=(
        "Roll the match back to an earlier autosave. N defaults to 1 "
        "for the turn/round/command forms; `undo to round X` jumps "
        "directly to that round's start. Confirmation thresholds are "
        "configurable via the undo_confirmation_* rules."
    ),
)
registry.annotate_sub(
    "history", "export",
    usage="!history export <selector> <path>",
    desc=(
        "Write one snapshot to a JSON file. Selectors: manual:<name>, "
        "round:<N>, turn:<seq>, command:<seq>, latest:round, "
        "latest:turn, latest:command."
    ),
)
registry.annotate_sub(
    "history", "import",
    usage="!history import <path> [as <name>]",
    desc=(
        "Read a snapshot JSON and add it to manual saves on the active "
        "match. `as <name>` overrides the imported label; otherwise the "
        "snapshot's existing label is used. The imported snapshot is "
        "always categorized as manual regardless of its original kind."
    ),
)


@registry.command("store", usage="!store save <path> | !store load <path>", desc="Save/load all matches and channel bindings.", snapshot=False)
async def store_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["store"])
        return await ctx.send(f"**{title}**\n{body}")
    sub = args[0].lower()
    if sub == "save":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "store", "save"):
            return
        # Optional `include_history=yes` toggle bundles every match's
        # autosave history into the save file. Off by default to keep
        # files small; on for full campaign backups.
        include_history = False
        for extra in args[2:]:
            if extra.startswith("include_history="):
                include_history = _parse_bool(extra[len("include_history="):])
        mgr.save(args[1], include_history=include_history)
        suffix = " (with autosave history)" if include_history else ""
        return await ctx.send(f"Saved to `{args[1]}`{suffix}")
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
    usage="!store save <path> [include_history=yes]",
    desc=(
        "Save all matches and channel bindings to a JSON file. By default "
        "excludes per-match autosave history (which can be large); pass "
        "`include_history=yes` to bundle the round/turn/command/manual "
        "saves for full campaign backup."
    ),
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
        # the formula. Named args may appear in any order but must all
        # come before any formula content so simple cases stay terse
        # (`!pas add ... <formula>` without typing target=/scope=).
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


# ---- !tile -------------------------------------------------------------
# Special-tile data store. Each (x, y) tile has its own free-form dict;
# this command surface manages reads/writes and serialization. Hooks
# (on_enter / on_exit / on_stop) and tile-attached passives are coming
# in follow-up PRs — this one ships the storage primitive plus the
# formula-side tile_get / tile_has accessors.
def _parse_xy(args: List[str], offset: int = 1) -> Tuple[int, int]:
    """Pull integer (x, y) starting at args[offset]. Raises VTTError
    with the standard '!ent tp'-style message so the error surface is
    consistent across tile commands."""
    try:
        x = int(args[offset])
        y = int(args[offset + 1])
    except (IndexError, ValueError):
        raise VTTError("expected integer x and y coordinates.")
    return x, y


@registry.command(
    "tile",
    usage=("!tile <set|del|info|list|clear> ..."),
    desc=(
        "Special-tile data store. Each (x, y) tile has a free-form "
        "data dict — set arbitrary nested keys with `set`, read with "
        "`info`, drop with `del`. A tile's \"glyph\" key (if set to "
        "a single character) overrides the default \".\" rendering "
        "in !map when no entity stands on the cell. Tile data is "
        "readable from formulas via tile_get(x, y, \"path\") and "
        "tile_has(x, y, \"path\")."
    ),
)
async def tile_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["tile"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    # ---- set <x> <y> <path> <value> ----
    if sub == "set":
        if await return_help_if_not_enough_args(ctx, args, 5, "tile", "set"):
            return
        x, y = _parse_xy(args)
        path = args[3]
        value = _parse_scalar(args[4])
        m.tile_set_path(x, y, path, value)
        return await ctx.send(f"tile ({x},{y}).{path} = {value!r}")

    # ---- del <x> <y> [<path>] ----
    if sub == "del":
        if await return_help_if_not_enough_args(ctx, args, 3, "tile", "del"):
            return
        x, y = _parse_xy(args)
        path = args[3] if len(args) >= 4 else None
        if (x, y) not in m.tiles:
            return await ctx.send(f"tile ({x},{y}) has no data.")
        m.tile_del_path(x, y, path)
        if path:
            return await ctx.send(f"Removed `{path}` from tile ({x},{y}).")
        return await ctx.send(f"Cleared tile ({x},{y}).")

    # ---- info <x> <y> ----
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 3, "tile", "info"):
            return
        x, y = _parse_xy(args)
        if (x, y) not in m.tiles:
            return await ctx.send(f"tile ({x},{y}): no data.")
        data = m.tiles[(x, y)]
        return await ctx.send(
            f"**tile ({x},{y})**\n```{json.dumps(data, indent=2, sort_keys=True)}\n```"
        )

    # ---- list ----
    if sub == "list":
        if not m.tiles:
            return await ctx.send("No special tiles in this match.")
        # Compact one-line summary per tile: list top-level feature
        # keys so the GM sees at a glance what's where. Use !tile info
        # for the full nested dump.
        lines = ["**Special tiles:**"]
        for (x, y) in sorted(m.tiles.keys()):
            features = ", ".join(sorted(m.tiles[(x, y)].keys()))
            lines.append(f"- ({x},{y}): {features}")
        return await ctx.send("\n".join(lines))

    # ---- clear [confirm] ----
    # Wipes ALL tile data. Destructive enough to require explicit
    # confirm, mirroring the !history undo confirmation pattern.
    if sub == "clear":
        confirmed = "confirm" in (a.lower() for a in args[1:])
        n = len(m.tiles)
        if n == 0:
            return await ctx.send("No tile data to clear.")
        if not confirmed:
            return await ctx.send(
                f"⚠️ This would clear all {n} special tile(s) in this match. "
                f"To proceed: `!tile clear confirm`."
            )
        m.tiles.clear()
        return await ctx.send(f"Cleared {n} tile(s).")

    # ---- hook add|del|list ----
    # Tile hooks are formulas stored under tiles[(x,y)]["hooks"][when]
    # that fire when an entity transits the tile. on_enter / on_exit
    # / on_stop are the three supported `when` values (TILE_HOOK_NAMES
    # in logic.py). One hook per (tile, when) — re-adding overwrites.
    # The formula context inside the hook binds self = the moving
    # entity, this = current_entity_id(), tile_x/tile_y = the firing
    # tile's coords, hook_name = the `when`.
    if sub == "hook":
        from logic import TILE_HOOK_NAMES
        from formula import validate_formula, FormulaError
        if len(args) < 2:
            title, body = registry.help_for(["tile", "hook"])
            return await ctx.send(f"**{title}**\n{body}")
        hsub = args[1].lower()

        if hsub == "add":
            # !tile hook add <x> <y> <when> <formula>
            if await return_help_if_not_enough_args(ctx, args, 6, "tile", "hook"):
                return
            x, y = _parse_xy(args, offset=2)
            when = args[4]
            formula_src = args[5]
            if when not in TILE_HOOK_NAMES:
                allowed = ", ".join(sorted(TILE_HOOK_NAMES))
                return await ctx.send(
                    f"❌ Unknown tile hook '{when}'. Allowed: {allowed}."
                )
            try:
                validate_formula(formula_src, mode="exec")
            except FormulaError as ex:
                return await ctx.send(f"❌ Invalid hook formula: {ex}")
            m.tile_set_path(x, y, f"hooks.{when}", formula_src)
            return await ctx.send(
                f"Set tile ({x},{y}) `{when}` hook."
            )

        if hsub == "del":
            # !tile hook del <x> <y> <when>
            if await return_help_if_not_enough_args(ctx, args, 5, "tile", "hook"):
                return
            x, y = _parse_xy(args, offset=2)
            when = args[4]
            if when not in TILE_HOOK_NAMES:
                allowed = ", ".join(sorted(TILE_HOOK_NAMES))
                return await ctx.send(
                    f"❌ Unknown tile hook '{when}'. Allowed: {allowed}."
                )
            try:
                m.tile_del_path(x, y, f"hooks.{when}")
            except NotFound:
                return await ctx.send(
                    f"tile ({x},{y}) has no `{when}` hook."
                )
            return await ctx.send(f"Removed tile ({x},{y}) `{when}` hook.")

        if hsub == "list":
            # !tile hook list <x> <y>
            if await return_help_if_not_enough_args(ctx, args, 4, "tile", "hook"):
                return
            x, y = _parse_xy(args, offset=2)
            data = m.tiles.get((x, y), {})
            hooks = data.get("hooks") if isinstance(data, dict) else None
            if not isinstance(hooks, dict) or not hooks:
                return await ctx.send(f"tile ({x},{y}): no hooks.")
            lines = [f"**tile ({x},{y}) hooks:**"]
            for when in sorted(hooks.keys()):
                src = hooks[when]
                # Truncate long formulas in the listing — the GM can
                # use !tile info to see the full source if needed.
                snippet = src if len(src) <= 80 else src[:77] + "..."
                lines.append(f"- `{when}`: {snippet}")
            return await ctx.send("\n".join(lines))

        title, body = registry.help_for(["tile", "hook"])
        return await ctx.send(f"**{title}**\n{body}")

    # ---- def: template management --------------------------------------
    # Templates are reusable tile-kind definitions (e.g. "flame",
    # "spike_trap"). Define once, then place at any coordinate. See the
    # SpecialTileTemplate class in logic.py for the storage shape and
    # Match.place_tile_template for the placement semantics (data is
    # deep-copied at place time, hooks are looked up live at fire
    # time).
    if sub == "def":
        from logic import TILE_HOOK_NAMES, TILE_RESERVED_KEYS
        from formula import validate_formula, FormulaError
        if len(args) < 2:
            title, body = registry.help_for(["tile", "def"])
            return await ctx.send(f"**{title}**\n{body}")
        dsub = args[1].lower()

        if dsub == "new":
            # !tile def new <name>
            if await return_help_if_not_enough_args(ctx, args, 3, "tile", "def"):
                return
            name = args[2]
            try:
                m.define_tile_template(name)
            except DuplicateId:
                return await ctx.send(
                    f"❌ template `{name}` already exists. "
                    f"Use `!tile def del {name}` first or pick a new name."
                )
            return await ctx.send(f"Defined empty tile template `{name}`.")

        if dsub == "data":
            # !tile def data <name> <path> <value>
            if await return_help_if_not_enough_args(ctx, args, 5, "tile", "def"):
                return
            name = args[2]
            path = args[3]
            if name not in m.tile_templates:
                return await ctx.send(f"❌ template `{name}` not found.")
            if path.split(".", 1)[0] in TILE_RESERVED_KEYS:
                return await ctx.send(
                    f"❌ template data may not use reserved keys "
                    f"({', '.join(sorted(TILE_RESERVED_KEYS))})."
                )
            value = _parse_scalar(args[4])
            tpl = m.tile_templates[name]
            # Reuse Match.tile_set_path's path-walking semantics by
            # operating directly on the template's data dict — we don't
            # want to round-trip through self.tiles. The walk is short
            # enough to inline here without pulling out a helper.
            parts = path.split(".")
            d = tpl.data
            for i, key in enumerate(parts[:-1]):
                existing = d.get(key)
                if existing is not None and not isinstance(existing, dict):
                    where = ".".join(parts[:i + 1])
                    return await ctx.send(
                        f"❌ template `{name}` value at '{where}' is "
                        f"{type(existing).__name__}, not a dict."
                    )
                if key not in d:
                    d[key] = {}
                d = d[key]
            d[parts[-1]] = value
            return await ctx.send(f"template `{name}`.{path} = {value!r}")

        if dsub == "del-data":
            # !tile def del-data <name> <path>
            if await return_help_if_not_enough_args(ctx, args, 4, "tile", "def"):
                return
            name = args[2]
            path = args[3]
            if name not in m.tile_templates:
                return await ctx.send(f"❌ template `{name}` not found.")
            tpl = m.tile_templates[name]
            parts = path.split(".")
            chain: List[Dict[str, Any]] = [tpl.data]
            for i, key in enumerate(parts[:-1]):
                d = chain[-1]
                if not isinstance(d, dict) or key not in d:
                    return await ctx.send(
                        f"❌ template `{name}` has no value at '{path}'."
                    )
                chain.append(d[key])
            leaf_parent = chain[-1]
            leaf_key = parts[-1]
            if not isinstance(leaf_parent, dict) or leaf_key not in leaf_parent:
                return await ctx.send(
                    f"❌ template `{name}` has no value at '{path}'."
                )
            del leaf_parent[leaf_key]
            # Prune empty intermediate dicts the same way tile_del_path does.
            for i in range(len(chain) - 1, 0, -1):
                if chain[i]:
                    break
                del chain[i - 1][parts[i - 1]]
            return await ctx.send(f"Removed template `{name}`.{path}.")

        if dsub == "hook":
            # !tile def hook <name> <when> <formula>
            if await return_help_if_not_enough_args(ctx, args, 5, "tile", "def"):
                return
            name = args[2]
            when = args[3]
            formula_src = args[4]
            if name not in m.tile_templates:
                return await ctx.send(f"❌ template `{name}` not found.")
            if when not in TILE_HOOK_NAMES:
                allowed = ", ".join(sorted(TILE_HOOK_NAMES))
                return await ctx.send(
                    f"❌ Unknown tile hook '{when}'. Allowed: {allowed}."
                )
            try:
                validate_formula(formula_src, mode="exec")
            except FormulaError as ex:
                return await ctx.send(f"❌ Invalid hook formula: {ex}")
            m.tile_templates[name].hooks[when] = formula_src
            return await ctx.send(f"Set template `{name}` `{when}` hook.")

        if dsub == "del-hook":
            # !tile def del-hook <name> <when>
            if await return_help_if_not_enough_args(ctx, args, 4, "tile", "def"):
                return
            name = args[2]
            when = args[3]
            if name not in m.tile_templates:
                return await ctx.send(f"❌ template `{name}` not found.")
            if when not in m.tile_templates[name].hooks:
                return await ctx.send(
                    f"template `{name}` has no `{when}` hook."
                )
            del m.tile_templates[name].hooks[when]
            return await ctx.send(f"Removed template `{name}` `{when}` hook.")

        if dsub == "del":
            # !tile def del <name> [confirm]
            if await return_help_if_not_enough_args(ctx, args, 3, "tile", "def"):
                return
            name = args[2]
            if name not in m.tile_templates:
                return await ctx.send(f"❌ template `{name}` not found.")
            confirmed = "confirm" in (a.lower() for a in args[3:])
            instance_count = sum(
                1 for t in m.tiles.values()
                if isinstance(t, dict) and t.get("_template") == name
            )
            if instance_count and not confirmed:
                return await ctx.send(
                    f"⚠️ template `{name}` still has {instance_count} "
                    f"placed instance(s). Deleting it leaves them "
                    f"orphaned (hooks will warn at fire time). "
                    f"To proceed: `!tile def del {name} confirm`."
                )
            orphaned = m.undefine_tile_template(name)
            extra = (
                f" ({orphaned} placed instance(s) now orphaned)"
                if orphaned else ""
            )
            return await ctx.send(f"Deleted template `{name}`{extra}.")

        if dsub == "list":
            if not m.tile_templates:
                return await ctx.send("No tile templates defined.")
            lines = ["**tile templates:**"]
            for tname in sorted(m.tile_templates.keys()):
                tpl = m.tile_templates[tname]
                placed = sum(
                    1 for t in m.tiles.values()
                    if isinstance(t, dict) and t.get("_template") == tname
                )
                hook_keys = ", ".join(sorted(tpl.hooks.keys())) or "—"
                lines.append(
                    f"- `{tname}` ({placed} placed; hooks: {hook_keys})"
                )
            return await ctx.send("\n".join(lines))

        if dsub == "info":
            if await return_help_if_not_enough_args(ctx, args, 3, "tile", "def"):
                return
            name = args[2]
            if name not in m.tile_templates:
                return await ctx.send(f"❌ template `{name}` not found.")
            tpl = m.tile_templates[name]
            placed = sum(
                1 for t in m.tiles.values()
                if isinstance(t, dict) and t.get("_template") == name
            )
            import json as _json
            body = _json.dumps(
                {"data": tpl.data, "hooks": tpl.hooks},
                indent=2, default=str,
            )
            return await ctx.send(
                f"**template `{name}`** ({placed} placed)\n```{body}\n```"
            )

        title, body = registry.help_for(["tile", "def"])
        return await ctx.send(f"**{title}**\n{body}")

    # ---- place <template> <x> <y> [k=v ...] ----------------------------
    # Instantiate a template at (x, y). Optional override tokens look
    # like key=value and merge over the template's defaults before the
    # tile is written. Replaces any existing tile data at (x, y) — the
    # GM is explicitly placing a new instance.
    if sub == "place":
        if await return_help_if_not_enough_args(ctx, args, 4, "tile", "place"):
            return
        name = args[1]
        x, y = _parse_xy(args, offset=2)
        overrides: Dict[str, Any] = {}
        for tok in args[4:]:
            if "=" not in tok:
                return await ctx.send(
                    f"❌ override `{tok}` must be `key=value`."
                )
            k, v = tok.split("=", 1)
            overrides[k] = _parse_scalar(v)
        try:
            m.place_tile_template(name, x, y, overrides=overrides or None)
        except (NotFound, OutOfBounds, VTTError) as ex:
            return await ctx.send(f"❌ {ex}")
        msg = f"Placed template `{name}` at ({x},{y})."
        if overrides:
            override_str = ", ".join(f"{k}={v!r}" for k, v in overrides.items())
            msg = msg + f" Overrides: {override_str}."
        return await ctx.send(msg)

    title, body = registry.help_for(["tile"])
    return await ctx.send(f"**{title}**\n{body}")


registry.annotate_sub(
    "tile", "set",
    usage="!tile set <x> <y> <path> <value>",
    desc=(
        "Set tile (x,y).path = value, creating intermediate dicts. "
        "Path uses dotted notation: `flame.burn_damage` sets the "
        "burn_damage key inside the flame feature. Value is parsed "
        "as int → float → string (use quotes for strings with spaces)."
    ),
)
registry.annotate_sub(
    "tile", "del",
    usage="!tile del <x> <y> [<path>]",
    desc=(
        "Delete one path from a tile (e.g. `flame.spreads`) or drop "
        "the entire tile entry if no path is given. Parent dicts that "
        "go empty as a result are pruned automatically — the sparse-"
        "dict invariant means !tile list never shows no-op coords."
    ),
)
registry.annotate_sub(
    "tile", "info",
    usage="!tile info <x> <y>",
    desc="Show one tile's data dict as pretty-printed JSON.",
)
registry.annotate_sub(
    "tile", "list",
    usage="!tile list",
    desc=(
        "List every tile that has data, one per line, with its top-"
        "level feature names. For the full nested data dump, use "
        "!tile info on a specific coordinate."
    ),
)
registry.annotate_sub(
    "tile", "clear",
    usage="!tile clear [confirm]",
    desc=(
        "Wipe ALL special-tile data from the active match. Requires "
        "the trailing `confirm` token; without it the command reports "
        "how many tiles would be cleared and bails."
    ),
)
registry.annotate_sub(
    "tile", "hook",
    usage=(
        "!tile hook add <x> <y> <when> <formula> | "
        "!tile hook del <x> <y> <when> | "
        "!tile hook list <x> <y>"
    ),
    desc=(
        "Per-tile ad-hoc hook formulas. The same `when` values as tile "
        "templates (on_enter / on_exit / on_stop) — see `!tile def` for "
        "the firing-time semantics. Ad-hoc hooks override the template's "
        "hook for that one `when` if the tile was placed from a "
        "template; on instances without a template they ARE the only "
        "behavior. Reach for templates first when the same behavior "
        "recurs at multiple coordinates; reach for !tile hook when a "
        "single tile needs unique behavior."
    ),
)
registry.annotate_sub(
    "tile", "def",
    usage=(
        "!tile def new <name> | "
        "!tile def data <name> <path> <value> | "
        "!tile def del-data <name> <path> | "
        "!tile def hook <name> <when> <formula> | "
        "!tile def del-hook <name> <when> | "
        "!tile def del <name> [confirm] | "
        "!tile def list | "
        "!tile def info <name>"
    ),
    desc=(
        "Manage reusable tile templates. A template defines a kind of "
        "tile (e.g. `flame`, `spike`, `treasure_chest`) with default "
        "data fields and hook formulas. Place instances at coordinates "
        "with `!tile place`. Template data is deep-copied into each "
        "instance at placement (per-instance edits don't propagate), "
        "while template hooks are looked up live at fire time (template "
        "hook edits propagate to all placed instances). Deleting a "
        "template that still has placed instances requires the "
        "trailing `confirm` token — orphaned instances continue to "
        "exist but their hook firings warn instead of running."
    ),
)
registry.annotate_sub(
    "tile", "place",
    usage="!tile place <template> <x> <y> [key=value ...]",
    desc=(
        "Instantiate a defined template at (x, y), discarding any tile "
        "data already there. The instance starts as a deep copy of the "
        "template's data with `_template` set to the template name. "
        "Optional `key=value` tokens after the coords override "
        "individual fields on this instance (e.g. "
        "`!tile place flame 3 3 burn_damage=20` to make this flame "
        "hit harder without redefining the template)."
    ),
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