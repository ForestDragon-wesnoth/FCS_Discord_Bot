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
import random
import copy

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

    def _unknown_command_message(self, name: str, mgr: MatchManager,
                                  ctx: ReplyContext) -> str:
        """Build the 'unknown command' reply, optionally with did-you-mean
        suggestions. Looks at registered commands AND the active match's
        alias names so a typo'd alias gets a hint too."""
        import difflib as _difflib
        candidates = list(self._handlers.keys())
        active_mid = mgr.active_by_channel.get(ctx.channel_key)
        if active_mid is not None and active_mid in mgr.matches:
            candidates.extend(mgr.matches[active_mid].aliases.keys())
        # cutoff 0.55 is permissive enough to catch single-letter
        # typos (e.g. "ene" -> "ent") but tight enough not to suggest
        # every command for nonsense input.
        hits = _difflib.get_close_matches(name, candidates, n=3, cutoff=0.55)
        msg = f"❓ Unknown command `{name}`"
        if hits:
            msg += f" — did you mean: {', '.join(f'`{h}`' for h in hits)}?"
        return msg

    def _resolve_alias(self, name: str, args: List[str],
                       mgr: MatchManager, ctx: ReplyContext) -> Tuple[str, List[str]]:
        """One-shot alias expansion. Returns (name, args) — unchanged if
        no alias applies. Tokenized via shlex so a multi-word expansion
        like `dmg -> "ent hp this"` produces real argv tokens. We
        deliberately do NOT re-resolve recursively (that would let
        aliases loop) — the alias's first token must already be a real
        registered command, which is enforced at alias-def time."""
        mid = mgr.active_by_channel.get(ctx.channel_key)
        if mid is None or mid not in mgr.matches:
            return name, args
        expansion = mgr.matches[mid].aliases.get(name)
        if not expansion:
            return name, args
        import shlex as _shlex
        try:
            tokens = _shlex.split(expansion)
        except ValueError:
            tokens = expansion.split()
        if not tokens:
            return name, args
        return tokens[0], tokens[1:] + list(args)

    async def dispatch_no_snapshot(self, name: str, args: List[str],
                                    ctx: ReplyContext, mgr: MatchManager):
        """Alias-resolve, look up, and call a handler WITHOUT recording
        an autosave. Used by !batch and !run to fold many subcommands
        into one outer snapshot (the wrapping command's own snapshot).
        VTTError still surfaces as `❌ ...`; unexpected exceptions still
        propagate so the caller can decide whether to abort."""
        name, args = self._resolve_alias(name, args, mgr, ctx)
        h = self._handlers.get(name)
        if not h:
            await ctx.send(self._unknown_command_message(name, mgr, ctx))
            return
        try:
            return await h(ctx, args, mgr)
        except VTTError as e:
            await ctx.send(f"❌ {e}")
            return

    async def run(self, name: str, args: List[str], ctx: ReplyContext, mgr: MatchManager):
        # Alias resolution happens BEFORE handler lookup so an alias can
        # shadow a built-in name on this match.
        name, args = self._resolve_alias(name, args, mgr, ctx)
        h = self._handlers.get(name)
        if not h:
            # Did-you-mean: suggest close matches from registered
            # commands plus the active match's alias names. Keeps the
            # "❓ Unknown command" message short — at most three
            # suggestions, only if any are close.
            await ctx.send(self._unknown_command_message(name, mgr, ctx))
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
        "status_csv": ", ".join(sorted(e.status.keys())) if e.status else "",
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
    # Status flags carry data; show name(field=value, ...) if data exists.
    if e.status:
        status_parts = []
        for name in sorted(e.status.keys()):
            data = e.status[name]
            if isinstance(data, dict) and data:
                fields = ", ".join(f"{k}={v!r}" for k, v in sorted(data.items()))
                status_parts.append(f"{name}({fields})")
            else:
                status_parts.append(name)
        parts.append(f"Status: {', '.join(status_parts)}")
    else:
        parts.append("Status: (none)")
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
    # bool → int → float → str. Only EXACT "true"/"false" (case-
    # sensitive) becomes a Python bool — "yes"/"no" are conventionally
    # used as string values across existing scenarios (see scenario 10's
    # `entity[hero].resting == "yes"`), and case-insensitive matching
    # would collide with variable-name tokens like "True" or "False"
    # used as labels. If you want a string literal "true", quote the
    # token at the command layer: `!ent set_var x t "true"`.
    if token == "true":
        return True
    if token == "false":
        return False
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
    if sub in ("delete", "del", "remove", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 2, "system", "delete"):
            return
        name = args[1]
        mgr.delete_system(name)  # raises VTTError/NotFound on guard failure
        return await ctx.send(f"Deleted GameSystem `{name}`.")
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

    # ---- system alias <subcommand> -----------------------------------
    # System-level alias library. These don't affect any currently live
    # match (parallel to how `!system set` only refreshes rules via
    # refresh_match_rules); new matches in this system pick them up.
    if sub == "alias":
        if await return_help_if_not_enough_args(ctx, args, 3, "system", "alias"):
            return
        action = args[1].lower()
        sys_name = args[2]
        s = mgr.get_system(sys_name)

        if action == "list":
            if not s.aliases:
                return await ctx.send(
                    f"No aliases defined on system `{sys_name}`."
                )
            lines = [f"**Aliases on `{sys_name}`:**"]
            for aname in sorted(s.aliases.keys()):
                exp = s.aliases[aname]
                snippet = exp if len(exp) <= 60 else exp[:57] + "..."
                lines.append(f"- `!{aname}` → `!{snippet}`")
            return await ctx.send("\n".join(lines))

        if action == "info":
            if len(args) < 4:
                return await ctx.send(
                    "Usage: `!system alias info <system> <name>`."
                )
            aname = args[3]
            exp = s.aliases.get(aname)
            if exp is None:
                return await ctx.send(
                    f"❌ alias `{aname}` not defined on system `{sys_name}`."
                )
            return await ctx.send(f"**`!{aname}`** on `{sys_name}` → `!{exp}`")

        if action == "def":
            # `!system alias def <system> <name> <expansion>` mirrors
            # `!alias def` but on the system. We re-use the match-level
            # validator's contract (first token of expansion must be a
            # real registered command) — system-level aliases are NOT
            # allowed to chain into other aliases either.
            if len(args) < 5:
                return await ctx.send(
                    "Usage: `!system alias def <system> <name> <expansion>`."
                )
            aname = args[3]
            expansion = " ".join(args[4:]).strip()
            if not expansion:
                return await ctx.send("❌ alias expansion cannot be empty.")
            if not aname or any(c.isspace() for c in aname):
                return await ctx.send(
                    f"❌ invalid alias name `{aname}`: names must be a "
                    f"single whitespace-free token."
                )
            import shlex as _shlex
            try:
                tokens = _shlex.split(expansion)
            except ValueError as ex:
                return await ctx.send(f"❌ invalid alias expansion: {ex}")
            if not tokens:
                return await ctx.send("❌ alias expansion cannot be empty.")
            target = tokens[0]
            if target not in registry._handlers:
                return await ctx.send(
                    f"❌ alias expansion must start with a real command; "
                    f"`{target}` is not a registered command."
                )
            prior = s.aliases.get(aname)
            s.aliases[aname] = expansion
            if prior is not None:
                return await ctx.send(
                    f"Redefined alias `{aname}` on `{sys_name}` "
                    f"(was `!{prior}`, now `!{expansion}`)."
                )
            return await ctx.send(
                f"Defined alias `!{aname}` → `!{expansion}` on `{sys_name}`."
            )

        if action in ("del", "delete", "remove", "rm"):
            if len(args) < 4:
                return await ctx.send(
                    "Usage: `!system alias del <system> <name>`."
                )
            aname = args[3]
            if aname not in s.aliases:
                return await ctx.send(
                    f"❌ alias `{aname}` not defined on system `{sys_name}`."
                )
            del s.aliases[aname]
            return await ctx.send(
                f"Deleted alias `{aname}` from system `{sys_name}`."
            )

        return await ctx.send(
            f"Unknown `!system alias` action `{action}`. "
            f"Use one of: def, del, list, info."
        )

    title, body = registry.help_for(["system"])
    return await ctx.send(f"**{title}**\n{body}")
registry.annotate_sub("system", "list", usage="!system list", desc="List existing GameSystems.")
registry.annotate_sub("system", "info", usage="!system info <name>", desc="Show a GameSystem's settings.")
registry.annotate_sub("system", "rules", usage="!system rules", desc="List all available rules, their defaults, their types, and descriptions")
registry.annotate_sub("system", "new", usage="!system new <name>", desc="Create a GameSystem.")
registry.annotate_sub(
    "system", "delete",
    usage="!system delete <name>",
    desc=(
        "Delete a GameSystem (aliases: del/remove/rm). Guarded: the "
        "system must exist, at least one system must remain, it can't be "
        "the global default (reassign that first), and no live match may "
        "still be bound to it (rebind/delete those matches first). "
        "Per-server/channel default pointers at the deleted system are "
        "scrubbed and fall back to the global default."
    ),
)
registry.annotate_sub("system", "set", usage="!system set <name> <key> <value>", desc="Change a GameSystem setting (booleans/int auto-coerced). Use \"\" to clear a string rule.")
registry.annotate_sub(
    "system", "alias",
    usage=(
        "!system alias <def|del|list|info> <system> [<name> [<expansion>]]"
    ),
    desc=(
        "Manage a GameSystem's alias library. Aliases are copied into "
        "every NEW match created under this system (same inherit-at-"
        "create pattern as tile templates and formula functions); "
        "match-side `!alias` edits afterwards don't reach back here. "
        "Forms: `def <sys> <name> <expansion>`, `del <sys> <name>`, "
        "`list <sys>`, `info <sys> <name>`. The expansion's first "
        "token must be a real registered command (no alias chains)."
    ),
)
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

    # !ent status <id> <action> [args...]
    # Statuses now carry their own data dicts. Actions:
    #   list                       — names + brief data summary
    #   add <name>                 — add status (empty data) idempotently
    #   remove <name>              — remove the whole status
    #   clear                      — wipe all statuses
    #   info <name>                — pretty-print one status's data
    #   set <name> <path> <value>  — write a data field (dotted path,
    #                                 like !ent set_var)
    #   del <name> <path>          — remove a data field
    if sub == "status":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "status"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        e = m.entities[eid]
        action = args[2].lower()

        if action == "list":
            if not e.status:
                return await ctx.send(f"`{eid}` status: (none)")
            lines = [f"**`{eid}` statuses:**"]
            for name in sorted(e.status.keys()):
                data = e.status[name]
                if isinstance(data, dict) and data:
                    fields = ", ".join(f"{k}={v!r}" for k, v in sorted(data.items()))
                    lines.append(f"- `{name}` ({fields})")
                else:
                    lines.append(f"- `{name}` (no data)")
            return await ctx.send("\n".join(lines))

        if action == "clear":
            n = len(e.status)
            # Snapshot for event firing, then mutate. on_status_removed
            # fires once per status (in insertion order so reactions
            # have a stable ordering).
            removed = [(name, copy.deepcopy(data))
                       for name, data in e.status.items()]
            e.status.clear()
            event_log: List[str] = []
            for name, data in removed:
                event_log.extend(m._emit_status_diff(eid, name, data, None))
            tail = "\n" + "\n".join(event_log) if event_log else ""
            return await ctx.send(f"Cleared {n} status(es) from `{eid}`.{tail}")

        if action in ("add", "remove", "rm", "del-status",
                      "info", "set", "del"):
            if len(args) < 4:
                return await ctx.send(
                    f"❌ `!ent status {eid} {action}` needs a status name."
                )
            name = args[3]
            if action == "add":
                if name not in e.status:
                    before = None
                    e.status[name] = {}
                    after = copy.deepcopy(e.status[name])
                    event_log = m._emit_status_diff(eid, name, before, after)
                    msg = f"Added status `{name}` to `{eid}`."
                    if event_log:
                        msg += "\n" + "\n".join(event_log)
                    return await ctx.send(msg)
                return await ctx.send(
                    f"Status `{name}` already on `{eid}` (no change; use "
                    f"`set` to edit its data)."
                )
            if action in ("remove", "rm", "del-status"):
                if name in e.status:
                    before = copy.deepcopy(e.status[name])
                    del e.status[name]
                    event_log = m._emit_status_diff(eid, name, before, None)
                    msg = f"Removed status `{name}` from `{eid}`."
                    if event_log:
                        msg += "\n" + "\n".join(event_log)
                    return await ctx.send(msg)
                return await ctx.send(f"`{eid}` has no status `{name}`.")
            if action == "info":
                if name not in e.status:
                    return await ctx.send(f"`{eid}` has no status `{name}`.")
                data = e.status[name]
                if not data:
                    return await ctx.send(f"**`{eid}.{name}`** (no data)")
                body = json.dumps(data, indent=2, default=str)
                return await ctx.send(f"**`{eid}.{name}`**\n```{body}\n```")
            if action == "set":
                if len(args) < 6:
                    return await ctx.send(
                        f"❌ `!ent status {eid} set {name}` needs <path> "
                        f"and <value>."
                    )
                path = args[4]
                value = _parse_scalar(args[5])
                # Snapshot for the diff-based event firing. Use None for
                # the "status didn't exist" case so set acts as an
                # implicit add when applied to a fresh status.
                before = copy.deepcopy(e.status.get(name)) if name in e.status else None
                data = e.status.setdefault(name, {})
                # Walk the dotted path, creating intermediate dicts.
                parts_path = path.split(".")
                cur = data
                for i, key in enumerate(parts_path[:-1]):
                    existing = cur.get(key)
                    if existing is not None and not isinstance(existing, dict):
                        where = ".".join(parts_path[:i + 1])
                        return await ctx.send(
                            f"❌ `{eid}.{name}` value at '{where}' is "
                            f"{type(existing).__name__}, not a dict."
                        )
                    if key not in cur:
                        cur[key] = {}
                    cur = cur[key]
                cur[parts_path[-1]] = value
                after = copy.deepcopy(e.status[name])
                event_log = m._emit_status_diff(eid, name, before, after)
                msg = f"Set `{eid}.{name}.{path}` = {value!r}."
                if event_log:
                    msg += "\n" + "\n".join(event_log)
                return await ctx.send(msg)
            if action == "del":
                if len(args) < 5:
                    return await ctx.send(
                        f"❌ `!ent status {eid} del {name}` needs a "
                        f"<path>. (For removing the status itself use "
                        f"`!ent status {eid} remove {name}`.)"
                    )
                if name not in e.status:
                    return await ctx.send(f"`{eid}` has no status `{name}`.")
                path = args[4]
                before = copy.deepcopy(e.status[name])
                data = e.status[name]
                parts_path = path.split(".")
                chain = [data]
                for i, key in enumerate(parts_path[:-1]):
                    cur = chain[-1]
                    if not isinstance(cur, dict) or key not in cur:
                        where = ".".join(parts_path[:i + 1])
                        return await ctx.send(
                            f"❌ `{eid}.{name}` has no value at '{where}'."
                        )
                    chain.append(cur[key])
                leaf = chain[-1]
                if not isinstance(leaf, dict) or parts_path[-1] not in leaf:
                    return await ctx.send(
                        f"❌ `{eid}.{name}` has no value at '{path}'."
                    )
                del leaf[parts_path[-1]]
                # Prune empty intermediate dicts.
                for i in range(len(chain) - 1, 0, -1):
                    if chain[i]:
                        break
                    del chain[i - 1][parts_path[i - 1]]
                after = copy.deepcopy(e.status[name])
                event_log = m._emit_status_diff(eid, name, before, after)
                msg = f"Removed `{eid}.{name}.{path}`."
                if event_log:
                    msg += "\n" + "\n".join(event_log)
                return await ctx.send(msg)
        return await ctx.send(
            f"❌ unknown status action `{action}`. Use add / remove / "
            f"set / del / info / list / clear."
        )

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
        hook_log = m.entities[eid].set_initiative_entity(value)
        msg = f"Set initiative of `{eid}` to {value}."
        if hook_log:
            msg = msg + "\n" + "\n".join(hook_log)
        return await ctx.send(msg)
    # clone
    if sub == "clone":
        # !ent clone <src> <new_id> <x> <y> [<x> <y> ...]
        # One coordinate pair clones to a single cell with id <new_id>
        # (the original behavior). Multiple pairs clone once per cell,
        # auto-numbering the ids <new_id>1, <new_id>2, ... — handy for
        # dropping a squad of identical mooks without retyping !ent add.
        # All-or-nothing: every target cell and every generated id is
        # validated before any clone is created.
        if await return_help_if_not_enough_args(ctx, args, 5, "ent", "clone"):
            return
        src_id = _resolve_eid(m, args[1])
        new_id_base = args[2]
        coord_tokens = args[3:]
        if len(coord_tokens) % 2 != 0:
            return await ctx.send(
                "❌ coordinates must come in `x y` pairs."
            )
        try:
            nums = [int(t) for t in coord_tokens]
        except ValueError:
            return await ctx.send("❌ coordinates must be integers.")
        pairs = list(zip(nums[0::2], nums[1::2]))  # [(x1,y1),(x2,y2),...]
        if src_id not in m.entities:
            raise NotFound(f"Entity '{src_id}' not found.")
        single = (len(pairs) == 1)
        # Build the (id, x, y) plan and validate everything up front.
        plan: List[Tuple[str, int, int]] = []
        seen_cells = set()
        seen_ids = set()
        for i, (x, y) in enumerate(pairs, start=1):
            cid = new_id_base if single else f"{new_id_base}{i}"
            if cid in m.entities or cid in seen_ids:
                return await ctx.send(
                    f"❌ entity id `{cid}` already exists (or is "
                    f"duplicated in this batch)."
                )
            if not m.in_bounds(x, y):
                return await ctx.send(
                    f"❌ ({x},{y}) outside {m.grid_width}x{m.grid_height}."
                )
            if (x, y) in seen_cells or m.is_occupied(x, y):
                return await ctx.send(
                    f"❌ cell ({x},{y}) is already occupied (or targeted "
                    f"twice in this batch)."
                )
            seen_ids.add(cid)
            seen_cells.add((x, y))
            plan.append((cid, x, y))
        src = m.entities[src_id]
        logs: List[str] = []
        for cid, x, y in plan:
            payload = src.to_dict()
            payload.update({"id": cid, "x": x, "y": y})
            clone = Entity.from_dict(payload)
            _, spawn_log = clone.spawn(m, x, y, initiative=src.initiative)
            clone.facing = src.facing
            logs.extend(spawn_log)
        if single:
            cid, x, y = plan[0]
            msg = f"Cloned `{src_id}` → `{cid}` at ({x},{y})."
        else:
            placed = ", ".join(f"`{cid}`@({x},{y})" for cid, x, y in plan)
            msg = f"Cloned `{src_id}` → {len(plan)} copies: {placed}."
        if logs:
            msg += "\n" + "\n".join(logs)
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
    usage="!ent clone <id> <new_id> <x> <y> [<x> <y> ...]",
    desc=(
        "Copy <id> to one or more cells. With a single x y pair, the copy "
        "gets id <new_id> (the original behavior). With multiple pairs, "
        "one copy is made per cell with auto-numbered ids "
        "<new_id>1, <new_id>2, ... — e.g. `!ent clone goblin mob 5 5 6 6 "
        "7 7` drops three goblins. All-or-nothing: every target cell must "
        "be free and every generated id unused, or nothing is cloned."
    ),
)
registry.annotate_sub(
    "ent", "status",
    usage=(
        "!ent status <id> <add|remove|clear|list|info|set|del> [name] "
        "[path] [value]"
    ),
    desc=(
        "Manage an entity's statuses. Each status name maps to its own "
        "data dict — the 'what X does is stored in X' design rule. "
        "Actions: `add <name>` (idempotent, empty data), `remove <name>` "
        "(drop the whole status), `clear` (wipe all), `list` (names + "
        "field summary), `info <name>` (pretty-print one), `set <name> "
        "<path> <value>` (write a field — dotted path, value parses "
        "true/false/int/float/str), `del <name> <path>` (remove a field, "
        "pruning empty parent dicts). The engine recognizes ONE "
        "convention: `skips_turn: true` on any active status causes the "
        "bearer's turn to be skipped (see SCENARIO 240). All other data "
        "is GM-defined — auto-decay etc. is configured via the "
        "status_tick_when / status_tick_formula rules."
    ),
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

# ---- !find ----------------------------------------------------------
# Token-based entity query. Each arg is one predicate; all predicates
# AND. Predicate forms:
#   <var>=<value>      vars[var] == value  (numeric coerced when both sides parse)
#   <var>!=<value>     vars[var] != value
#   <var><<value>      vars[var] < value   (numeric; non-numeric vars never match)
#   <var><=<value>     vars[var] <= value
#   <var>><value>      vars[var] > value
#   <var>>=<value>     vars[var] >= value
#   status:<name>      entity.status has the named status
#   group:<name>       entity is a member of the named group
# Dotted var names address nested dicts (e.g. inventory.sword.damage).
_FIND_OPS = ("!=", "<=", ">=", "=", "<", ">")


def _parse_find_predicate(token: str) -> Tuple[str, str, Optional[str]]:
    """Return (kind, key, value). kind is one of:
        "status"  — status:NAME            (value is None, key is NAME)
        "group"   — group:NAME             (value is None, key is NAME)
        "<op>"    — one of =, !=, <, <=, >, >=  (key is var path, value is RHS)
    Raises VTTError on malformed input."""
    if token.startswith("status:"):
        name = token[len("status:"):]
        if not name:
            raise VTTError("`status:` predicate needs a status name.")
        return "status", name, None
    if token.startswith("group:"):
        name = token[len("group:"):]
        if not name:
            raise VTTError("`group:` predicate needs a group name.")
        return "group", name, None
    # Try operators longest-first so `<=` isn't misread as `<`.
    for op in _FIND_OPS:
        idx = token.find(op)
        if idx > 0:  # key must be non-empty
            return op, token[:idx], token[idx + len(op):]
    raise VTTError(
        f"Unrecognized find predicate `{token}`. Expected `key=value`, "
        f"`key<value`, `status:NAME`, or `group:NAME`."
    )


def _resolve_dotted_var(vars_dict: Dict[str, Any], path: str) -> Tuple[bool, Any]:
    """Walk dotted path through a vars dict. Returns (found, value)."""
    cur: Any = vars_dict
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def _coerce_for_compare(s: str) -> Any:
    """Best-effort coercion for find values: ints, floats, bools, then
    fall back to the raw string. Numeric inputs let `hp<20` work
    naturally; the bool form covers `team_active=true` style flags."""
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _find_match_entity(m: Match, e: Entity, predicates: List[Tuple[str, str, Optional[str]]]) -> bool:
    """Return True iff every predicate matches `e`. Predicates short-
    circuit on the first failure."""
    for kind, key, val in predicates:
        if kind == "status":
            if key not in e.status:
                return False
            continue
        if kind == "group":
            if e.id not in (m.groups.get(key) or []):
                return False
            continue
        # Var-comparison forms. Missing var never matches any operator
        # (including !=) — that keeps "team=red" from spuriously
        # matching entities that don't have a team set at all.
        found, lhs = _resolve_dotted_var(e.vars, key)
        if not found:
            return False
        rhs = _coerce_for_compare(val)
        # If lhs is numeric-ish and rhs coerced numeric, compare as numbers.
        # Otherwise the relational ops still attempt the comparison and a
        # TypeError is treated as "doesn't match".
        try:
            if kind == "=":
                if lhs != rhs:
                    return False
            elif kind == "!=":
                if lhs == rhs:
                    return False
            elif kind == "<":
                if not (lhs < rhs):
                    return False
            elif kind == "<=":
                if not (lhs <= rhs):
                    return False
            elif kind == ">":
                if not (lhs > rhs):
                    return False
            elif kind == ">=":
                if not (lhs >= rhs):
                    return False
        except TypeError:
            return False
    return True


@registry.command(
    "find",
    usage="!find <predicate> [<predicate> ...]",
    desc=(
        "Query entities by AND-ed predicates. Predicate forms: "
        "`var=value`, `var!=value`, `var<value`, `var<=value`, "
        "`var>value`, `var>=value` for vars; `status:NAME` for a status "
        "flag; `group:NAME` for group membership. Dotted var paths walk "
        "nested dicts (`inventory.sword.damage>5`). Example: "
        "`!find team=red hp<20 status:bleeding` lists all red-team "
        "entities below 20 HP that are bleeding."
    ),
    snapshot=False,
)
async def find_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["find"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    try:
        predicates = [_parse_find_predicate(t) for t in args]
    except VTTError as ex:
        return await ctx.send(f"❌ {ex}")
    hits = [e for e in m.entities_in_turn_order()
            if _find_match_entity(m, e, predicates)]
    if not hits:
        return await ctx.send("No entities match.")
    word = "match" if len(hits) == 1 else "matches"
    lines = [f"**{len(hits)} {word}:**"]
    for e in hits:
        lines.append(f"- {_entity_line(e)}")
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

    # ---- diff --------------------------------------------------------
    # `!history diff <selector_a> <selector_b>` — entity-focused diff
    # between two snapshots. Uses the same selector syntax as
    # export/import (manual:<name>, round:<N>, turn:<seq>, command:<seq>,
    # latest:<kind>).
    if sub == "diff":
        if await return_help_if_not_enough_args(ctx, args, 3, "history", "diff"):
            return
        snap_a = _resolve_snapshot_selector(m, args[1])
        snap_b = _resolve_snapshot_selector(m, args[2])
        lines = _format_snapshot_diff(snap_a, snap_b, args[1], args[2])
        return await ctx.send("\n".join(lines))

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


def _flatten_vars(prefix: str, val: Any, out: Dict[str, Any]) -> None:
    """Flatten a vars dict into dotted paths. We diff at the leaf level
    so a deeply nested var change (e.g. `inventory.sword.damage`) shows
    as a single line rather than burying the diff inside a serialized
    dict. Lists/tuples are treated as leaves — order matters and a
    structural list-diff would add noise without much signal here."""
    if isinstance(val, dict):
        if not val:
            out[prefix] = {}
            return
        for k, v in val.items():
            child = f"{prefix}.{k}" if prefix else str(k)
            _flatten_vars(child, v, out)
    else:
        out[prefix] = val


def _diff_entity(eid: str, ea: Dict[str, Any], eb: Dict[str, Any]) -> List[str]:
    """Per-entity diff lines. Reports changes to position, facing, vars
    (flattened), passives (id-level add/remove/change), and groups. Only
    returns lines for fields that actually differ — an unchanged entity
    contributes zero lines."""
    lines: List[str] = []
    for field_name in ("position", "facing", "groups"):
        va = ea.get(field_name)
        vb = eb.get(field_name)
        if va != vb:
            lines.append(f"    {field_name}: {va!r} -> {vb!r}")
    # Vars: flatten both sides, then diff leaf by leaf.
    fa: Dict[str, Any] = {}
    fb: Dict[str, Any] = {}
    _flatten_vars("", ea.get("vars", {}) or {}, fa)
    _flatten_vars("", eb.get("vars", {}) or {}, fb)
    all_keys = sorted(set(fa) | set(fb))
    for k in all_keys:
        if k not in fa:
            lines.append(f"    vars.{k}: + {fb[k]!r}")
        elif k not in fb:
            lines.append(f"    vars.{k}: - {fa[k]!r}")
        elif fa[k] != fb[k]:
            lines.append(f"    vars.{k}: {fa[k]!r} -> {fb[k]!r}")
    # Passives: id-level. Changing a passive's formula shows as a
    # before/after; we don't try to diff formula text itself.
    pa = ea.get("passives", {}) or {}
    pb = eb.get("passives", {}) or {}
    for pid in sorted(set(pa) | set(pb)):
        if pid not in pa:
            lines.append(f"    passive.{pid}: added")
        elif pid not in pb:
            lines.append(f"    passive.{pid}: removed")
        elif pa[pid] != pb[pid]:
            lines.append(f"    passive.{pid}: changed")
    return lines


def _format_snapshot_diff(snap_a: Snapshot, snap_b: Snapshot,
                          sel_a: str, sel_b: str) -> List[str]:
    """Build a human-readable diff between two snapshot states. Focuses
    on what GMs actually want to see — entity-level changes, round/turn
    deltas, and rule changes — rather than dumping a JSON patch."""
    sa = snap_a.state
    sb = snap_b.state
    lines = [f"**Diff `{sel_a}` -> `{sel_b}`**"]
    # Match-level scalars worth surfacing.
    if sa.get("round_number") != sb.get("round_number"):
        lines.append(
            f"- round_number: {sa.get('round_number')} -> "
            f"{sb.get('round_number')}"
        )
    if sa.get("active_index") != sb.get("active_index"):
        lines.append(
            f"- active_index: {sa.get('active_index')} -> "
            f"{sb.get('active_index')}"
        )
    # Rule changes: report at the key level (one line per changed rule).
    ra = sa.get("rules", {}) or {}
    rb = sb.get("rules", {}) or {}
    rule_keys = sorted(set(ra) | set(rb))
    rule_changes = []
    for rk in rule_keys:
        if rk not in ra:
            rule_changes.append(f"  + rule `{rk}` = {rb[rk]!r}")
        elif rk not in rb:
            rule_changes.append(f"  - rule `{rk}` (was {ra[rk]!r})")
        elif ra[rk] != rb[rk]:
            rule_changes.append(f"  ~ rule `{rk}`: {ra[rk]!r} -> {rb[rk]!r}")
    if rule_changes:
        lines.append("- Rules:")
        lines.extend(rule_changes)
    # Entities — the main event.
    ea = sa.get("entities", {}) or {}
    eb = sb.get("entities", {}) or {}
    # `entities` can be serialized as a dict (id -> entity_dict) or a
    # list of entity_dicts depending on Match.to_dict's format. Normalize.
    if isinstance(ea, list):
        ea = {e.get("id"): e for e in ea if isinstance(e, dict) and "id" in e}
    if isinstance(eb, list):
        eb = {e.get("id"): e for e in eb if isinstance(e, dict) and "id" in e}
    added = sorted(set(eb) - set(ea))
    removed = sorted(set(ea) - set(eb))
    both = sorted(set(ea) & set(eb))
    if added:
        lines.append(f"- Entities added: {', '.join(f'`{x}`' for x in added)}")
    if removed:
        lines.append(f"- Entities removed: {', '.join(f'`{x}`' for x in removed)}")
    entity_change_lines: List[str] = []
    for eid in both:
        ent_diff = _diff_entity(eid, ea[eid], eb[eid])
        if ent_diff:
            entity_change_lines.append(f"  `{eid}`:")
            entity_change_lines.extend(ent_diff)
    if entity_change_lines:
        lines.append("- Entities changed:")
        lines.extend(entity_change_lines)
    # Global passives.
    gpa = sa.get("global_passives", {}) or {}
    gpb = sb.get("global_passives", {}) or {}
    gp_changes = []
    for pid in sorted(set(gpa) | set(gpb)):
        if pid not in gpa:
            gp_changes.append(f"  + global passive `{pid}`")
        elif pid not in gpb:
            gp_changes.append(f"  - global passive `{pid}`")
        elif gpa[pid] != gpb[pid]:
            gp_changes.append(f"  ~ global passive `{pid}` changed")
    if gp_changes:
        lines.append("- Global passives:")
        lines.extend(gp_changes)
    if len(lines) == 1:
        lines.append("(no differences)")
    return lines


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
registry.annotate_sub(
    "history", "diff",
    usage="!history diff <selector_a> <selector_b>",
    desc=(
        "Compare two snapshots and report entity-level changes. "
        "Selectors use the same syntax as `!history export`: "
        "manual:<name>, round:<N>, turn:<seq>, command:<seq>, "
        "latest:<kind>. The diff is direction-aware (`a -> b`) so "
        "swapping the arguments inverts adds and removes."
    ),
)


@registry.command(
    "undo",
    usage=(
        "!undo turn [N] [confirm] | "
        "!undo round [N] [confirm] | "
        "!undo command [N] [confirm] | "
        "!undo to round <X> [confirm]"
    ),
    desc=(
        "Shortcut for `!history undo ...`. Forwards every arg to the "
        "history undo subcommand — same scopes (turn/round/command/to "
        "round), same confirmation thresholds, same outputs. Exists "
        "because `!history undo` is the most common destructive "
        "operation and typing it out four times in a row gets old."
    ),
    snapshot=False,
)
async def undo_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    # Prepend "undo" and dispatch to history_cmd. We don't recurse
    # through registry.run because that would also re-trigger
    # autosave bookkeeping and command-name resolution; a direct call
    # keeps the two paths interchangeable from the user's POV but
    # avoids double-bookkeeping under the hood.
    return await history_cmd(ctx, ["undo"] + list(args), mgr)


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
            validate_program(formula, known_funcs=frozenset(m.formula_functions.keys()))
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
            validate_program(formula, known_funcs=frozenset(m.formula_functions.keys()))
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
                validate_formula(formula_src, mode="exec", known_funcs=frozenset(m.formula_functions.keys()))
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
                validate_formula(formula_src, mode="exec", known_funcs=frozenset(m.formula_functions.keys()))
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

    # ---- place_area <template> <x1> <y1> <x2> <y2> [overrides...] ----
    # Bulk-fill every cell in the inclusive rectangle (x1,y1)..(x2,y2)
    # with a placement of <template>. The rectangle is normalized so
    # the GM can pass the corners in any order. Overrides apply to
    # EVERY placed instance (same as a single !tile place). Existing
    # tile data in the rectangle is replaced — matches single-place
    # semantics (which defaults to replace=True).
    if sub == "place_area":
        if await return_help_if_not_enough_args(ctx, args, 6, "tile", "place_area"):
            return
        name = args[1]
        try:
            x1 = int(args[2]); y1 = int(args[3])
            x2 = int(args[4]); y2 = int(args[5])
        except ValueError:
            return await ctx.send("❌ expected integer corner coordinates.")
        overrides: Dict[str, Any] = {}
        for tok in args[6:]:
            if "=" not in tok:
                return await ctx.send(
                    f"❌ override `{tok}` must be `key=value`."
                )
            k, v = tok.split("=", 1)
            overrides[k] = _parse_scalar(v)
        if name not in m.tile_templates:
            return await ctx.send(f"❌ template `{name}` not found.")
        # Normalize so (x1,y1) is the top-left and (x2,y2) the bottom-right.
        xa, xb = (x1, x2) if x1 <= x2 else (x2, x1)
        ya, yb = (y1, y2) if y1 <= y2 else (y2, y1)
        # Validate bounds BEFORE writing anything — if any corner is
        # off-grid we want an all-or-nothing failure, not a partial
        # fill that the GM has to clean up. The four corners are
        # representative because the rectangle is axis-aligned.
        for cx, cy in ((xa, ya), (xb, yb)):
            if not m.in_bounds(cx, cy):
                return await ctx.send(
                    f"❌ corner ({cx},{cy}) is outside the "
                    f"{m.grid_width}x{m.grid_height} grid."
                )
        placed = 0
        for px in range(xa, xb + 1):
            for py in range(ya, yb + 1):
                try:
                    m.place_tile_template(
                        name, px, py,
                        overrides=overrides or None,
                    )
                    placed += 1
                except (NotFound, OutOfBounds, VTTError) as ex:
                    # Should be unreachable given the bounds pre-check
                    # and the template-exists pre-check, but surface
                    # whatever it is rather than swallow.
                    return await ctx.send(
                        f"❌ failed at ({px},{py}): {ex}"
                    )
        rect_desc = f"({xa},{ya})..({xb},{yb})"
        msg = f"Placed template `{name}` at {placed} cells {rect_desc}."
        if overrides:
            override_str = ", ".join(f"{k}={v!r}" for k, v in overrides.items())
            msg = msg + f" Overrides: {override_str}."
        return await ctx.send(msg)

    # ---- find <template> ----
    # List every (x,y) where a tile carries the named template via its
    # `_template` field. Useful when the GM has placed many instances
    # via place_area and wants to audit "where ARE all the fires?".
    # Returns an empty-list ack rather than an error when no instances
    # exist, since "zero" is a valid answer to a "find" query.
    if sub == "find":
        if await return_help_if_not_enough_args(ctx, args, 2, "tile", "find"):
            return
        name = args[1]
        if name not in m.tile_templates:
            # Surface the typo loudly — a silent "0 found" would hide
            # a real mistake (defining 'fire' and searching 'flame').
            return await ctx.send(
                f"❌ template `{name}` not found. (Searching for a "
                f"template that doesn't exist is almost always a typo. "
                f"Use `!tile def list` to see available templates.)"
            )
        coords = sorted(
            (x, y) for (x, y), data in m.tiles.items()
            if isinstance(data, dict) and data.get("_template") == name
        )
        if not coords:
            return await ctx.send(
                f"No placed instances of template `{name}`."
            )
        coords_str = ", ".join(f"({x},{y})" for x, y in coords)
        return await ctx.send(
            f"`{name}` placed at {len(coords)} cell(s): {coords_str}"
        )

    # ---- place_random <template> <count> [overrides...] ----
    # Scatter `count` instances of a template across random EMPTY cells
    # (cells with no existing tile data). Entity-occupied cells are still
    # eligible — tiles and entities coexist. All-or-nothing: if fewer
    # than `count` empty cells exist, nothing is placed.
    if sub == "place_random":
        if await return_help_if_not_enough_args(ctx, args, 3, "tile", "place_random"):
            return
        name = args[1]
        try:
            count = int(args[2])
        except ValueError:
            return await ctx.send("❌ count must be an integer.")
        if count < 1:
            return await ctx.send("❌ count must be >= 1.")
        overrides: Dict[str, Any] = {}
        for tok in args[3:]:
            if "=" not in tok:
                return await ctx.send(f"❌ override `{tok}` must be `key=value`.")
            k, v = tok.split("=", 1)
            overrides[k] = _parse_scalar(v)
        if name not in m.tile_templates:
            return await ctx.send(f"❌ template `{name}` not found.")
        # Empty = in-bounds cell with no existing tile data.
        empty = [
            (x, y)
            for x in range(1, m.grid_width + 1)
            for y in range(1, m.grid_height + 1)
            if (x, y) not in m.tiles
        ]
        if len(empty) < count:
            return await ctx.send(
                f"❌ only {len(empty)} empty cell(s) available; cannot "
                f"place {count}. (place_random fills cells with no "
                f"existing tile data.)"
            )
        chosen = random.sample(empty, count)
        for px, py in chosen:
            m.place_tile_template(name, px, py, overrides=overrides or None)
        chosen.sort()
        coords_str = ", ".join(f"({x},{y})" for x, y in chosen)
        msg = f"Placed template `{name}` at {count} random cell(s): {coords_str}."
        if overrides:
            override_str = ", ".join(f"{k}={v!r}" for k, v in overrides.items())
            msg = msg + f" Overrides: {override_str}."
        return await ctx.send(msg)

    # ---- fill_pattern <x> <y> <legend> <row> [<row> ...] ----
    # Paint a multi-row ASCII stencil. `legend` maps single characters to
    # template names (e.g. "#=wall,~=water"); each row string places one
    # template per char at (x + col, y + row_index). Characters NOT in the
    # legend are skipped (left untouched) — that's how you punch holes,
    # conventionally with '.'. All-or-nothing on both validation fronts:
    # every legend template must exist and every non-skip cell must be
    # in-bounds before anything is placed.
    if sub == "fill_pattern":
        if await return_help_if_not_enough_args(ctx, args, 5, "tile", "fill_pattern"):
            return
        try:
            ax = int(args[1]); ay = int(args[2])
        except ValueError:
            return await ctx.send("❌ expected integer anchor x and y.")
        legend_token = args[3]
        rows = args[4:]
        # Parse legend: comma-separated char=template pairs.
        legend: Dict[str, str] = {}
        for pair in legend_token.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                return await ctx.send(
                    f"❌ legend entry `{pair}` must be `char=template`."
                )
            ch, tname = pair.split("=", 1)
            if len(ch) != 1:
                return await ctx.send(
                    f"❌ legend key `{ch}` must be exactly one character."
                )
            if tname not in m.tile_templates:
                return await ctx.send(f"❌ template `{tname}` not found.")
            legend[ch] = tname
        if not legend:
            return await ctx.send(
                "❌ legend is empty; provide at least one `char=template`."
            )
        # Collect the placements and bounds-check every non-skip cell first.
        placements: List[Tuple[int, int, str]] = []
        for row_idx, row in enumerate(rows):
            for col_idx, ch in enumerate(row):
                if ch not in legend:
                    continue  # skip char (hole)
                px = ax + col_idx
                py = ay + row_idx
                if not m.in_bounds(px, py):
                    return await ctx.send(
                        f"❌ pattern cell ({px},{py}) is outside the "
                        f"{m.grid_width}x{m.grid_height} grid."
                    )
                placements.append((px, py, legend[ch]))
        if not placements:
            return await ctx.send(
                "❌ pattern placed nothing (all characters were skips or "
                "the rows were empty)."
            )
        for px, py, tname in placements:
            m.place_tile_template(tname, px, py)
        # Summarize per-template counts.
        from collections import Counter
        counts = Counter(t for _, _, t in placements)
        summary = ", ".join(f"{c}× `{t}`" for t, c in sorted(counts.items()))
        return await ctx.send(
            f"Filled pattern at anchor ({ax},{ay}): {len(placements)} "
            f"cell(s) — {summary}."
        )

    # ---- test <x> <y> <hook_name> [--as <eid>] ----
    # Fire a tile's hook manually without requiring an entity transit.
    # Pure debugging UX: lets a GM verify a hook formula works (or test
    # a fire-while-you-edit cycle) without spawning a phantom entity
    # and teleporting it. --as binds `self` to a specific entity (same
    # surface as !eval --as).
    if sub == "test":
        if await return_help_if_not_enough_args(ctx, args, 4, "tile", "test"):
            return
        try:
            x = int(args[1]); y = int(args[2])
        except ValueError:
            return await ctx.send("❌ expected integer x and y.")
        hook_name = args[3]
        # Optional --as <eid>
        eid: Optional[str] = None
        rest = args[4:]
        if rest and rest[0] == "--as":
            if len(rest) < 2:
                return await ctx.send("❌ `--as` needs an entity id.")
            eid = _resolve_eid(m, rest[1])
            if eid not in m.entities:
                return await ctx.send(f"❌ entity `{eid}` not found.")
        from logic import TILE_HOOK_NAMES as _THN
        if hook_name not in _THN:
            allowed = ", ".join(sorted(_THN))
            return await ctx.send(
                f"❌ unknown tile hook `{hook_name}`. Allowed: {allowed}."
            )
        if not m.in_bounds(x, y):
            return await ctx.send(
                f"❌ ({x},{y}) outside {m.grid_width}x{m.grid_height}."
            )
        log = m.fire_tile_hook(hook_name, eid, x, y)
        if not log:
            return await ctx.send(
                f"tile ({x},{y}) has no `{hook_name}` hook to fire."
            )
        return await ctx.send("\n".join(log))

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
registry.annotate_sub(
    "tile", "place_area",
    usage="!tile place_area <template> <x1> <y1> <x2> <y2> [key=value ...]",
    desc=(
        "Bulk-fill the inclusive rectangle between (x1,y1) and (x2,y2) "
        "with placements of <template>. Corners may be given in any "
        "order. Existing tile data inside the rectangle is replaced — "
        "this is the multi-cell version of `!tile place` and has the "
        "same single-cell replace semantics. Optional `key=value` "
        "overrides apply to EVERY placed instance (e.g. "
        "`!tile place_area flame 1 1 5 5 burn_damage=10` paints a "
        "5x5 block of stronger-than-default flames). All-or-nothing: "
        "if any corner is off-grid the command fails without placing "
        "anything."
    ),
)
registry.annotate_sub(
    "tile", "find",
    usage="!tile find <template>",
    desc=(
        "List every coordinate where a tile was placed from the named "
        "template (via `!tile place` or `!tile place_area`). Coordinates "
        "are sorted in (x, y) order. Useful for auditing 'where are all "
        "the fires?' after bulk placements, or for confirming a "
        "template deletion really cleaned up. Returns an empty-list "
        "ack when zero instances exist; errors loudly if the template "
        "name isn't defined (treated as a typo since a missing-name "
        "search trivially has zero results)."
    ),
)
registry.annotate_sub(
    "tile", "place_random",
    usage="!tile place_random <template> <count> [key=value ...]",
    desc=(
        "Scatter `count` instances of a template across random EMPTY "
        "cells (cells with no existing tile data; entity-occupied cells "
        "are still eligible since tiles and entities coexist). "
        "All-or-nothing: if fewer than `count` empty cells exist, "
        "nothing is placed and the command reports how many were "
        "available. Optional `key=value` overrides apply to every "
        "placed instance, same as `!tile place`. The chosen cells are "
        "echoed so you can see where they landed."
    ),
)
registry.annotate_sub(
    "tile", "fill_pattern",
    usage='!tile fill_pattern <x> <y> <legend> <row> [<row> ...]',
    desc=(
        "Paint a multi-row ASCII stencil with the top-left at (x, y). "
        "`legend` maps single characters to templates, comma-separated: "
        '`"#=wall,~=water"`. Each following row string places one '
        "template per character at (x + column, y + row). Characters "
        "NOT in the legend are skipped (left untouched) — conventionally "
        "'.' is used to punch holes. Example: "
        '`!tile fill_pattern 2 2 "#=wall,~=water" "##~" "#.~" "~~~"` '
        "paints a 3x3 block. All-or-nothing: every legend template must "
        "exist and every non-skip cell must be in-bounds before any "
        "tile is placed."
    ),
)
registry.annotate_sub(
    "tile", "test",
    usage="!tile test <x> <y> <hook_name> [--as <eid>]",
    desc=(
        "Fire a tile's hook manually without requiring an entity transit. "
        "Pure debugging UX: verify a hook formula works (or test "
        "fire-while-you-edit) without spawning a phantom entity and "
        "teleporting it. `hook_name` is one of on_enter / on_exit / "
        "on_stop / on_round_start / on_round_end / on_turn_start / "
        "on_turn_end. `--as <eid>` binds `self` to that entity for the "
        "fire (same surface as !eval --as); omit it for hooks that "
        "don't reference self. Reports the hook's own log line if one "
        "fires, or 'no hook to fire' if the tile has no hook of that "
        "name registered."
    ),
)


@registry.command(
    "func",
    usage=("!func <def|del|list|info> ..."),
    desc=(
        "Define reusable formula functions callable by name from any "
        "formula on the active match. A function has a name, an ordered "
        "list of parameters, and a body (a full formula program — "
        "statements plus an optional trailing expression whose value is "
        "the return value). Inside the body, parameters are bound as "
        "plain identifiers and entity[self]/entity[this] resolve against "
        "the CALLER's context (functions are inline macros, not isolated "
        "frames). Functions may call other functions and recurse "
        "(bounded by the formula_function_recursion_limit rule). "
        "Subcommands: def, del, list, info."
    ),
)
async def func_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["func"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    # ---- def <name> <params> <body> ----
    # params is one token: comma-separated identifiers, or one of
    # "", "-", "none" for a zero-parameter function. The body is the
    # remaining args rejoined (so the GM can pass it unquoted if it has
    # no shell-significant characters, or quoted to preserve spaces).
    if sub == "def":
        if await return_help_if_not_enough_args(ctx, args, 3, "func", "def"):
            return
        name = args[1]
        params_token = args[2]
        body = " ".join(args[3:]).strip() if len(args) > 3 else ""
        if not body:
            return await ctx.send(
                "❌ function body cannot be empty. Usage: "
                "`!func def <name> <params> <body>`."
            )
        # Parse the params token. "", "-", and "none" all mean no params.
        if params_token.strip().lower() in ("", "-", "none"):
            params: List[str] = []
        else:
            params = [p.strip() for p in params_token.split(",") if p.strip()]
        # Validate the body BEFORE storing — known_funcs is the current
        # registry plus the name being defined (so a recursive function
        # validates), and known_params is this function's parameters.
        from formula import validate_program as _vp, FormulaError as _FE
        known = frozenset(m.formula_functions.keys()) | {name}
        try:
            # Build the body validator with params allowed. validate_program
            # doesn't take known_params, so use the lower-level path.
            from formula import FormulaEngine as _FEng
            _FEng._prepare(
                body, "exec",
                known_funcs=known,
                known_params=frozenset(params),
            )
        except _FE as ex:
            return await ctx.send(f"❌ invalid function body: {ex}")
        # Capture the prior definition (if any) so an overwrite can show
        # the GM what changed — silently replacing a function is exactly
        # the kind of edit that's easy to do by accident, so we surface
        # the before/after.
        prior = m.formula_functions.get(name)
        try:
            fn = m.define_formula_function(name, params, body)
        except (VTTError, DuplicateId) as ex:
            return await ctx.send(f"❌ {ex}")
        if prior is not None:
            return await ctx.send(
                f"Redefined formula function `{name}` (overwrote an "
                f"existing definition).\n"
                f"  was: `{prior.signature()}` → {prior.body}\n"
                f"  now: `{fn.signature()}` → {fn.body}"
            )
        return await ctx.send(f"Defined formula function `{fn.signature()}`.")

    # ---- del <name> ----
    if sub == "del":
        if await return_help_if_not_enough_args(ctx, args, 2, "func", "del"):
            return
        name = args[1]
        try:
            m.undefine_formula_function(name)
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(f"Deleted formula function `{name}`.")

    # ---- list ----
    if sub == "list":
        if not m.formula_functions:
            return await ctx.send("No formula functions defined.")
        lines = ["**Formula functions:**"]
        for fname in sorted(m.formula_functions.keys()):
            fn = m.formula_functions[fname]
            snippet = fn.body if len(fn.body) <= 60 else fn.body[:57] + "..."
            lines.append(f"- `{fn.signature()}` → {snippet}")
        return await ctx.send("\n".join(lines))

    # ---- info <name> ----
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "func", "info"):
            return
        name = args[1]
        fn = m.formula_functions.get(name)
        if fn is None:
            return await ctx.send(f"❌ formula function `{name}` not found.")
        return await ctx.send(
            f"**`{fn.signature()}`**\n```\n{fn.body}\n```"
        )

    title, body = registry.help_for(["func"])
    return await ctx.send(f"**{title}**\n{body}")


registry.annotate_sub(
    "func", "def",
    usage="!func def <name> <params> <body>",
    desc=(
        "Define (or overwrite) a formula function. `<params>` is a "
        "single token: comma-separated parameter names (e.g. "
        "`raw,armor`), or one of `-` / `none` / empty-string for no "
        "parameters. `<body>` is the rest of the line — a formula "
        "program whose trailing expression (if any) is the return "
        "value. Example: "
        "`!func def mitigated raw,armor \"max(raw - armor, 0)\"` then "
        "call it from any formula as `mitigated(entity[self].atk, 3)`. "
        "The body is validated at definition time; parameters and the "
        "function's own name (for recursion) are in scope. Redefining "
        "an existing name overwrites it."
    ),
)
registry.annotate_sub(
    "func", "del",
    usage="!func del <name>",
    desc=(
        "Delete a formula function. Formulas or other functions that "
        "still reference it will error at their next evaluation (the "
        "name simply stops resolving) — no dependency scan is done."
    ),
)
registry.annotate_sub(
    "func", "list",
    usage="!func list",
    desc=(
        "List all formula functions on the active match with their "
        "signatures and a body snippet. Use `!func info <name>` for the "
        "full body."
    ),
)
registry.annotate_sub(
    "func", "info",
    usage="!func info <name>",
    desc="Show a formula function's full signature and body.",
)


@registry.command(
    "alias",
    usage="!alias <def|del|list|info> ...",
    desc=(
        "Per-match command aliases. `!alias def dmg \"ent hp this\"` makes "
        "`!dmg -5` expand to `!ent hp this -5` on this match. Aliases are "
        "shorthand only — expansion is one-shot (an alias cannot resolve "
        "to another alias) and the expansion's first token must be a real "
        "registered command. Aliases are stored on the match (not the "
        "system) and persist through save/load. Subcommands: def, del, "
        "list, info."
    ),
    snapshot=False,
)
async def alias_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["alias"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    # ---- def <name> <expansion...> ----
    # The expansion is the rest of the line rejoined. We validate that
    # the expansion's first token is a real registered command — that's
    # the contract that makes aliases predictable (no silent typos and
    # no alias-to-alias chains, since resolution is one-shot in run()).
    if sub == "def":
        if await return_help_if_not_enough_args(ctx, args, 3, "alias", "def"):
            return
        name = args[1]
        expansion = " ".join(args[2:]).strip()
        if not expansion:
            return await ctx.send(
                "❌ alias expansion cannot be empty. Usage: "
                "`!alias def <name> <expansion>`."
            )
        # Reject names that would conflict with shell-significant
        # characters or are otherwise unusable as a command word.
        if not name or any(c.isspace() for c in name):
            return await ctx.send(
                f"❌ invalid alias name `{name}`: names must be a single "
                f"whitespace-free token."
            )
        # Validate that the expansion's first token is a registered
        # command. We deliberately don't accept other aliases as the
        # target — resolution is one-shot, so a chained alias would just
        # fail at dispatch with a confusing "Unknown command" error.
        import shlex as _shlex
        try:
            tokens = _shlex.split(expansion)
        except ValueError as ex:
            return await ctx.send(f"❌ invalid alias expansion: {ex}")
        if not tokens:
            return await ctx.send("❌ alias expansion cannot be empty.")
        target = tokens[0]
        if target not in registry._handlers:
            return await ctx.send(
                f"❌ alias expansion must start with a real command; "
                f"`{target}` is not a registered command."
            )
        prior = m.aliases.get(name)
        m.aliases[name] = expansion
        if prior is not None:
            return await ctx.send(
                f"Redefined alias `{name}` (overwrote an existing "
                f"definition).\n"
                f"  was: `!{name}` → `!{prior}`\n"
                f"  now: `!{name}` → `!{expansion}`"
            )
        return await ctx.send(f"Defined alias `!{name}` → `!{expansion}`.")

    # ---- del <name> ----
    if sub == "del":
        if await return_help_if_not_enough_args(ctx, args, 2, "alias", "del"):
            return
        name = args[1]
        if name not in m.aliases:
            return await ctx.send(f"❌ alias `{name}` not found.")
        del m.aliases[name]
        return await ctx.send(f"Deleted alias `{name}`.")

    # ---- list ----
    if sub == "list":
        if not m.aliases:
            return await ctx.send("No aliases defined.")
        lines = ["**Aliases:**"]
        for aname in sorted(m.aliases.keys()):
            exp = m.aliases[aname]
            snippet = exp if len(exp) <= 60 else exp[:57] + "..."
            lines.append(f"- `!{aname}` → `!{snippet}`")
        return await ctx.send("\n".join(lines))

    # ---- info <name> ----
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "alias", "info"):
            return
        name = args[1]
        exp = m.aliases.get(name)
        if exp is None:
            return await ctx.send(f"❌ alias `{name}` not found.")
        return await ctx.send(f"**`!{name}`** → `!{exp}`")

    title, body = registry.help_for(["alias"])
    return await ctx.send(f"**{title}**\n{body}")


registry.annotate_sub(
    "alias", "def",
    usage="!alias def <name> <expansion>",
    desc=(
        "Define (or overwrite) an alias on the active match. `<name>` is "
        "the new command word; `<expansion>` is the command line it "
        "expands to (its first token MUST be an existing registered "
        "command — aliases can shadow built-ins but can't chain into "
        "each other). Any args you pass to the alias are appended to the "
        "expansion. Example: `!alias def dmg \"ent hp this\"` then "
        "`!dmg -5` runs `!ent hp this -5`."
    ),
)
registry.annotate_sub(
    "alias", "del",
    usage="!alias del <name>",
    desc="Delete an alias from the active match.",
)
registry.annotate_sub(
    "alias", "list",
    usage="!alias list",
    desc=(
        "List all aliases defined on the active match, with their "
        "expansions."
    ),
)
registry.annotate_sub(
    "alias", "info",
    usage="!alias info <name>",
    desc="Show an alias's full expansion.",
)


# -- !batch ------------------------------------------------------------------
# `!batch cmd1 args1 ; cmd2 args2 ; ...` runs several commands as one
# undo unit: the dispatcher's outer pre/post snapshot for `!batch`
# captures the WHOLE sequence's state delta, and we use
# `dispatch_no_snapshot` for each subcommand so they don't each carve
# their own autosave. `;` is the separator — a literal `;` argument is
# spelled `";"` (shlex preserves the quotes around it).
def _split_batch(args: List[str], sep: str = ";") -> List[List[str]]:
    """Split a token list at every plain `sep` token. Empty subcommands
    (consecutive `;` or leading/trailing `;`) are dropped."""
    parts: List[List[str]] = []
    cur: List[str] = []
    for tok in args:
        if tok == sep:
            if cur:
                parts.append(cur)
                cur = []
        else:
            cur.append(tok)
    if cur:
        parts.append(cur)
    return parts


@registry.command(
    "batch",
    usage="!batch <cmd1> <args...> ; <cmd2> <args...> ; ...",
    desc=(
        "Run multiple commands as a single undo unit. The whole batch "
        "produces one history entry, so one `!history undo command` "
        "rolls back the entire sequence. Subcommands are separated by a "
        "bare `;` token (a literal semicolon argument would be quoted: "
        "`\";\"`). If a subcommand fails with an `❌ ...` error the "
        "batch continues with the next subcommand — the rollback is "
        "still one-shot because the outer snapshot was taken before "
        "any of them ran."
    ),
)
async def batch_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["batch"])
        return await ctx.send(f"**{title}**\n{body}")
    parts = _split_batch(args)
    if not parts:
        return await ctx.send(
            "❌ batch is empty — provide at least one subcommand."
        )
    # We rely on the outer dispatcher's snapshot for undo, so subcommands
    # use dispatch_no_snapshot. We surface a brief header so the GM can
    # tell the responses apart from a normal single-command reply.
    await ctx.send(f"Running batch of {len(parts)} command(s)...")
    for sub in parts:
        if not sub:
            continue
        await registry.dispatch_no_snapshot(sub[0], sub[1:], ctx, mgr)


# -- !run --------------------------------------------------------------------
@registry.command(
    "run",
    usage="!run <path>",
    desc=(
        "Read a file of commands (one per line, leading `!` optional, "
        "blank lines and `#`-prefixed comment lines ignored) and run "
        "them as a single batch — same one-snapshot undo semantics as "
        "`!batch`. Useful for replaying a saved sequence of setup "
        "commands at the start of a match."
    ),
)
async def run_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if await return_help_if_not_enough_args(ctx, args, 1, "run"):
        return
    path = args[0]
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return await ctx.send(f"❌ no such file: `{path}`")
    except OSError as ex:
        return await ctx.send(f"❌ cannot read `{path}`: {ex}")
    # Build subcommand argv lists. Each non-blank, non-comment line is
    # one subcommand. Leading `!` is optional — both forms are common in
    # human-written script files.
    import shlex as _shlex
    subcommands: List[List[str]] = []
    for ln in raw.splitlines():
        stripped = ln.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            stripped = stripped[1:]
        try:
            toks = _shlex.split(stripped)
        except ValueError as ex:
            return await ctx.send(
                f"❌ parse error in `{path}`: {ex} (line: `{ln[:60]}`)"
            )
        if toks:
            subcommands.append(toks)
    if not subcommands:
        return await ctx.send(
            f"No commands in `{path}` (file was empty or only "
            f"blank/comment lines)."
        )
    await ctx.send(
        f"Running {len(subcommands)} command(s) from `{path}`..."
    )
    for sub in subcommands:
        await registry.dispatch_no_snapshot(sub[0], sub[1:], ctx, mgr)


@registry.command(
    "eval",
    usage='!eval [--as <eid>] "<formula>" | !eval --as-passive <eid> <pid>',
    desc=("Evaluate a formula against the active match (for testing). "
          "`this` = current-turn entity. By default `self` is unbound; "
          "pass `--as <eid>` as the first two args to bind `self` (and "
          "entity[self]) to that entity, so you can test passive bodies "
          "interactively. Or pass `--as-passive <eid> <pid>` to look up "
          "an existing passive's formula and run it with `self` bound to "
          "`<eid>` — checks the entity's passives first, then global "
          "passives. Supports assignments and multi-statement bodies; "
          "if the source ends with an expression, its value is returned. "
          "Quote the whole formula to preserve spaces."),
)
async def eval_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["eval"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    # Optional `--as <eid>` prefix binds `self` for the evaluation, so a
    # GM can test a passive body (which references entity[self]) without
    # actually attaching it to an entity and advancing a turn.
    self_id = None
    src: str
    if args and args[0] == "--as-passive":
        # `--as-passive <eid> <pid>` — look up an existing passive by id
        # and run its formula. Saves the GM from copy-pasting a passive
        # body just to test it. We check the entity's own passives first
        # (the common case), then fall back to global_passives.
        if len(args) < 3:
            return await ctx.send(
                "❌ `--as-passive` needs an entity id and a passive id."
            )
        self_id = _resolve_eid(m, args[1])
        if self_id not in m.entities:
            raise NotFound(f"Entity '{self_id}' not found.")
        pid = args[2]
        e = m.entities[self_id]
        p = e.passives.get(pid) or m.global_passives.get(pid)
        if p is None:
            return await ctx.send(
                f"❌ passive `{pid}` not found on entity `{self_id}` or "
                f"in global passives."
            )
        src = p.formula
    elif args and args[0] == "--as":
        if len(args) < 3:
            return await ctx.send("❌ `--as` needs an entity id and a formula.")
        self_id = _resolve_eid(m, args[1])
        if self_id not in m.entities:
            raise NotFound(f"Entity '{self_id}' not found.")
        src = " ".join(args[2:])
    else:
        src = " ".join(args)  # rejoin in case shlex split on internal spaces
    eval_ctx = EvalCtx(this=m.current_entity_id(), target=self_id)
    engine = FormulaEngine(m)
    val = engine.eval_program(src, eval_ctx)
    # For side-effect-only formulas (e.g. `for eid in entities_within(...):
    # entity[eid].hp = entity[eid].hp - 7`) the trailing assignment makes
    # the return value None, which prints as a confusing "= `None`".
    # The engine tracked which entities were mutated; surface that
    # instead so the user sees what actually happened.
    if val is None and engine.affected_entities:
        ids = ", ".join(f"`{eid}`" for eid in engine.affected_entities)
        return await ctx.send(f"Affected {len(engine.affected_entities)} entit{'y' if len(engine.affected_entities) == 1 else 'ies'}: {ids}")
    return await ctx.send(f"= `{val!r}`")


# ---- Automated Help command (shows available commands----------------------------------------------------------
@registry.command("help", usage="!help [command [sub]]", desc="Show command usage. Try `!help ent` or `!help ent move`.")
async def help_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    title, body = registry.help_for(args)
    await ctx.send(f"**{title}**\n{body}")