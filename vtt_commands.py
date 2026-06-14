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
    EVENT_LOG_DEFAULT_FORMATS,
)

# Passive system
from logic import Passive, HOOK_NAMES

# Clamp system
from logic import ClampSpec

# Formula engine (expression-only $(...) substitution here; full program eval used by !eval)
from formula import resolve_arg_token, FormulaEngine, EvalCtx, FormulaError, validate_program, normalize_body_source, _get_path, _set_path

import re
import json
import random
import copy

# ---- Context abstraction -----------------------------------------------------
class ReplyContext(Protocol):
    channel_key: str  # unique per chat location (e.g., guild:channel). For CLI, just "CLI".
    # Identity of whoever sent the command. Discord sets these from the
    # message author; the CLI/harness use a switchable stand-in. Optional
    # for backward compat — a context without them disables host gating
    # (everyone is treated as privileged). See ctx_user / ctx_user_name.
    user_id: str
    user_name: str
    async def send(self, message: str) -> None: ...


def ctx_user(ctx: "ReplyContext") -> Optional[str]:
    """The sender's user id, or None when the surface doesn't carry one
    (which disables host gating for that surface)."""
    uid = getattr(ctx, "user_id", None)
    return uid if isinstance(uid, str) and uid else None


def ctx_user_name(ctx: "ReplyContext") -> str:
    """A human-friendly sender name for messages (falls back to the id,
    then to 'someone')."""
    name = getattr(ctx, "user_name", None)
    if isinstance(name, str) and name:
        return name
    return ctx_user(ctx) or "someone"

# ---- Command registry --------------------------------------------------------
Handler = Callable[[ReplyContext, List[str], MatchManager], Any]

# Subcommand names that are read-only across every command root, so a
# host-gated root invoked with one of these as its first argument is
# downgraded to "all" (anyone may run it). Conservative on purpose: these
# names never mutate state anywhere in the command surface, so the
# downgrade can't open a write hole. The per-match command_access rule
# can re-tighten any of them for fog-of-war matches.
READ_ONLY_SUBCOMMANDS: frozenset = frozenset({
    "list", "info", "cells", "diff", "channels", "hosts",
})
# `dump` is intentionally NOT in the set above: `!ent dump` reveals an
# entity's full var tree (including GM-hidden data), so it stays host-
# gated by default. A host can open it per match with
# `!host access set "ent dump" all`.

# Host-gated roots whose NO-ARGUMENT (view) form is read-only and should
# be player-available, even though their subcommands mutate (`!turn
# next`, `!history undo`). Only the bare invocation downgrades — `!undo`,
# whose bare form itself mutates, deliberately stays gated.
READ_ONLY_BARE_ROOTS: frozenset = frozenset({
    "turn", "history",
})
# The inverse of READ_ONLY_SUBCOMMANDS: an otherwise player-available
# ("all") command whose FIRST ARG here ELEVATES it to host-gated. Used by
# the visibility full-reveal: `!state` / `!map` / `!list` render from the
# channel's POV for anyone, but `... full` forces the omniscient view and
# so must be host-only (else a player could peek past fog of war).
ELEVATED_ARGS: Dict[str, frozenset] = {
    "state": frozenset({"full"}),
    # `map` renders for anyone from the channel POV, but `full` (omniscient),
    # `resize` (a grid mutation), and the `color`/`teamcolor` settings all
    # elevate to host-gated.
    "map": frozenset({"full", "resize", "color", "teamcolor"}),
    "list": frozenset({"full"}),
}

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
        # Per-command access level for the host/approval gate (see run()):
        #   "all"       anyone may run it (read-only / harmless)
        #   "host"      hosts run directly; a non-host's invocation is
        #               held for host approval (DEFAULT — mutating cmds)
        #   "host_only" hosts run directly; a non-host is rejected outright
        #               (no queue) — for the approval commands themselves
        #   "owner"     only the match owner may run it; others rejected
        # A host-gated root is downgraded to "all" when its first arg is a
        # known read-only subcommand (READ_ONLY_SUBCOMMANDS), and the
        # active match's command_access rule can override any of this.
        self._access: Dict[str, str] = {}
    def command(self, name: str, *, usage: Optional[str] = None,
                desc: Optional[str] = None, snapshot: bool = True,
                access: str = "host"):
        def deco(fn: Handler):
            self._handlers[name] = fn
            self._snapshot[name] = snapshot
            self._access[name] = access
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

    def _effective_access(self, name: str, args: List[str],
                          m: "Any") -> str:
        """Resolve the access level for this invocation: the command's
        base level, downgraded to 'all' for a read-only subcommand, then
        overridden by the active match's command_access rule. `m` is the
        active Match (or None)."""
        base = self._access.get(name, "host")
        if base == "host" and args and args[0].lower() in READ_ONLY_SUBCOMMANDS:
            base = "all"
        elif base == "host" and not args and name in READ_ONLY_BARE_ROOTS:
            # Bare view form of a root that mutates only via subcommands.
            base = "all"
        elif base == "all" and args and args[0].lower() in ELEVATED_ARGS.get(name, ()):
            # Full-reveal flag on a normally-open read -> host-gated.
            base = "host"
        if m is not None:
            sub_key = f"{name} {args[0].lower()}" if args else None
            # Per-match host overrides win over the system-level rule,
            # which wins over the default. Within each layer, a precise
            # "name sub" key beats a bare "name" key.
            for table in (getattr(m, "access_overrides", None),
                          m.rules.get("command_access")):
                if not isinstance(table, dict) or not table:
                    continue
                if sub_key is not None and sub_key in table:
                    return str(table[sub_key])
                if name in table:
                    return str(table[name])
        return base

    def _gate_decision(self, name: str, args: List[str],
                       ctx: ReplyContext, mgr: MatchManager) -> str:
        """Return one of: 'allow', 'queue', 'reject_owner', 'reject_host'.
        The gate only engages when there's an active match AND the surface
        carries a user identity."""
        mid = mgr.active_by_channel.get(ctx.channel_key)
        m = mgr.matches.get(mid) if mid is not None else None
        if m is None:
            return "allow"
        # Single-operator surfaces (the local CLI) carry no host-approval
        # infrastructure — there's no second person to approve a queued
        # request, so holding one is a dead end. Such a surface sets
        # `auto_approve`, and the gate becomes a full no-op: the operator
        # is effectively always authorized. (The scenario harness does NOT
        # set this, so the approval queue stays under test there.)
        if getattr(ctx, "auto_approve", False):
            return "allow"
        if m.owner is None:
            # No host system established on this match (legacy save /
            # API-created without an owner) — leave it fully open rather
            # than locking everyone out with no host able to approve.
            return "allow"
        user = ctx_user(ctx)
        if user is None:
            # Identity-less surface (shouldn't happen for our contexts) —
            # don't gate, preserving pre-host-system behavior.
            return "allow"
        access = self._effective_access(name, args, m)
        if access == "all":
            return "allow"
        is_owner = m.is_owner(user)
        is_host = m.is_host(user)
        if access == "owner":
            return "allow" if is_owner else "reject_owner"
        if access == "host_only":
            return "allow" if is_host else "reject_host"
        # access == "host": hosts run directly, everyone else is queued.
        return "allow" if is_host else "queue"

    async def _queue_request(self, name: str, args: List[str],
                             ctx: ReplyContext, mgr: MatchManager):
        """Hold a non-host's command for host approval. Stores the request
        on the active match and notifies — with clickable buttons if the
        surface supports them (Discord), else a text prompt."""
        mid = mgr.active_by_channel.get(ctx.channel_key)
        m = mgr.matches.get(mid)
        if m is None:  # defensive — gate already checked, but be safe
            await ctx.send("❌ No active match.")
            return
        req = m.add_pending_request(
            user=ctx_user(ctx), user_name=ctx_user_name(ctx),
            name=name, args=list(args), channel_key=ctx.channel_key,
        )
        sender = getattr(ctx, "send_approval", None)
        if callable(sender):
            return await sender(req)
        cmd = "!" + name + (" " + " ".join(args) if args else "")
        await ctx.send(
            f"🕓 `{cmd}` needs host approval (request `{req['id']}`, by "
            f"{req['user_name']}). A host can `!approve {req['id']}` or "
            f"`!deny {req['id']}`."
        )

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

        # Host / approval gate. Resolved against the channel's ACTIVE
        # match (the thing the command would act on). If there's no active
        # match, or the surface carries no identity, the gate is a no-op —
        # so match creation, help, and identity-less contexts all run
        # freely. Otherwise: owner/host commands run directly for the
        # right role, a non-host's mutating command is held for approval,
        # and owner_only / host_only violations are rejected outright.
        gate = self._gate_decision(name, args, ctx, mgr)
        if gate == "reject_owner":
            await ctx.send("❌ Only the match owner can do that.")
            return
        if gate == "reject_host":
            await ctx.send("❌ Only a host can do that.")
            return
        if gate == "queue":
            return await self._queue_request(name, args, ctx, mgr)
        # gate == "allow" -> fall through and run normally.

        # Pre-dispatch snapshot of the active match's state. We only
        # bother if (a) the command opted into snapshotting (snapshot=
        # True, default), (b) there IS an active match on this channel,
        # (c) the active match's autosave_command_retention_turns
        # rule isn't 0 (which means "command autosaves disabled"), AND
        # (d) we're NOT inside an action body — when an action is
        # executing (match._action_depth > 0) it owns the snapshot
        # for the whole transaction, so per-command snapshots inside
        # would clutter the undo history with synthetic mid-action
        # entries. Same suspension model as !batch/!run, but
        # flag-driven instead of routed through dispatch_no_snapshot.
        pre_state = None
        pre_active_mid = mgr.active_by_channel.get(ctx.channel_key)
        if (self._snapshot.get(name, True)
                and pre_active_mid is not None
                and pre_active_mid in mgr.matches):
            m_pre = mgr.matches[pre_active_mid]
            if (int(m_pre.rules.get("autosave_command_retention_turns", 3)) != 0
                    and getattr(m_pre, "_action_depth", 0) == 0):
                pre_state = m_pre.to_dict(include_history=False)

        # Reset the per-command summon budget on the active match.
        # summon_entity increments _summon_count and caps it at the
        # summon_event_limit rule; resetting here makes the budget
        # "per top-level command" (covering every hook fire and action
        # body the command triggers). Only the top-level run() resets —
        # cmd() inside an action uses dispatch_no_snapshot, which does
        # NOT re-enter run(), so a multi-summon action keeps its budget.
        if pre_active_mid is not None and pre_active_mid in mgr.matches:
            mgr.matches[pre_active_mid]._summon_count = 0

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


def _corpse_template_context(
    m: Match, x: int, y: int, eid: str, corpse: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the placeholder context for a corpse row. Same shape as
    _entity_template_context, but rooted in the corpse's stored
    snapshot rather than a live Entity — so the template engine sees
    every var/status/passive the snapshot carried.

    The x/y values come from the TILE coordinates (the authoritative
    position store), NOT the embedded snapshot's x/y. This matters
    if a (rare) GM operation ever moved a corpse — the template
    always shows where the corpse actually IS, not where the entity
    died.

    `died_round` is exposed as a top-level placeholder so a system
    can render `(fell on round {died_round})` without digging into
    storage."""
    ent = corpse.get("entity") or {}
    hp_var = m.rules.get("hp_var", "hp")
    max_hp_var = m.rules.get("max_hp_var", "max_hp")
    init_var = m.rules.get("turnorder_var", "initiative")
    ent_vars = ent.get("vars") or {}
    status = ent.get("status") or {}
    passives = ent.get("passives") or {}
    ctx: Dict[str, Any] = {
        "id": eid,
        "name": ent.get("name", eid),
        # Authoritative position from the tile, NOT the snapshot.
        "x": x,
        "y": y,
        "facing": ent.get("facing", ""),
        "team": ent_vars.get(m.rules.get("team_var", "team"), ""),
        "initiative": ent_vars.get(init_var, 0),
        "hp": ent_vars.get(hp_var, "?"),
        "max_hp": ent_vars.get(max_hp_var, "?"),
        "status_csv": ", ".join(sorted(status.keys())) if status else "",
        "passives_csv": ", ".join(sorted(passives.keys())) if passives else "",
        "died_round": corpse.get("died_round", "?"),
    }
    for k, v in ent_vars.items():
        if k not in ctx:
            ctx[k] = v
    return ctx


def _corpse_line(
    m: Match, x: int, y: int, eid: str, corpse: Dict[str, Any],
) -> str:
    """Single-line corpse summary, rendered from the active match's
    corpse_line_format rule. Falls back to the engine default if the
    rule is empty (shouldn't happen for normal systems)."""
    tmpl = m.rules.get("corpse_line_format")
    if not tmpl:
        tmpl = DEFAULT_SYSTEM_SETTINGS.get(
            "corpse_line_format",
            "{name} (`{id}`): HP: {hp}/{max_hp} X,Y: {x},{y} (corpse)",
        )
    return _render_template(tmpl, _corpse_template_context(m, x, y, eid, corpse))


def _part_status_str(m, p: Entity) -> str:
    """Compact one-line summary of a body part's hp + routing knobs, for
    `!part list`."""
    hp_var, mhp_var, _ = p._vital_var_names()
    hp = p.vars.get(hp_var, 0)
    mhp = p.vars.get(mhp_var, 0)
    flags: List[str] = []
    pct = p.vars.get("to_main_percent",
                     m.rules.get("part_to_main_percent_default", 0))
    flags.append(f"{pct}%→main")
    cap = p.vars.get("to_main_cap", m.rules.get("part_to_main_cap_default", "max_hp"))
    flags.append(f"cap={cap}")
    if m.is_indestructible(p):
        flags.append("indestructible")
    if bool(p.vars.get("vital", m.rules.get("part_vital_default", False))):
        flags.append("vital")
    if p.vars.get("__part_destroyed"):
        flags.append("DESTROYED")
    return f"{hp}/{mhp} hp, " + ", ".join(flags)


def _entity_dump(e: Entity) -> str:
    """Raw 'show everything' view — template-free, complete state of the entity."""
    parts: List[str] = []
    parts.append(f"**{e.name}** (`{e.id}`)")
    parts.append(f"Position: ({e.x}, {e.y}) facing {e.facing}")
    parts.append(f"Team: {e.team if e.team else '(none)'}")
    if e.part_of:
        parts.append(f"Body part of: `{e.part_of}`")
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
    # Body parts attached to this entity (locational damage).
    m = getattr(e, "_match", None)
    body = m.entity_parts(e.id) if m is not None else []
    if body:
        parts.append("**body parts:**")
        name_var = m.part_name_var()
        for bp in body:
            role = bp.vars.get(name_var, "?")
            parts.append(f"- `{bp.id}` ({role}): {_part_status_str(m, bp)}")
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
        mid = mgr.create_match(match_id, name, w, h, channel_key=ctx.channel_key, system_name=system_name, owner=ctx_user(ctx))
        return await ctx.send(f"Created match `{name}` with id `{mid}` using system `{mgr.get(mid).system_name}`.")
    if sub == "use":# and len(args) >= 2:
        if await return_help_if_not_enough_args(ctx, args, 2, "match", "use"):
            return
        mid = args[1]
        mgr.set_active_for_channel(ctx.channel_key, mid)
        # `use` makes this channel act on the match; treat that as binding
        # the channel too so the match's channel set stays accurate.
        m = mgr.matches.get(mid)
        if m is not None:
            m.bind_channel(ctx.channel_key)
        return await ctx.send(f"Active match is now **{m.name}** (`{mid}`, system `{m.system_name}`).")
    if sub == "bind":
        # !match bind [<match_id>] [label=<text>]
        # Bind THIS channel to a match (defaults to the channel's active
        # match) and make the match active here. Multiple channels can
        # bind one match (uncapped) — host channel + players channel, or
        # one channel per team. An optional label is stored for the
        # upcoming per-team / fog-of-war views.
        label = None
        pov = None
        positional = []
        for a in args[1:]:
            if a.startswith("label="):
                label = a[len("label="):]
            elif a.startswith("pov="):
                # pov=<team> restricts this channel to a team's view;
                # pov=omniscient (or pov=) clears it back to seeing all.
                pov = a[len("pov="):]
            else:
                positional.append(a)
        target_mid = positional[0] if positional else mgr.get_active_for_channel(ctx.channel_key)
        if not target_mid:
            return await ctx.send(
                "❌ No match to bind. Use `!match bind <match_id>` or set "
                "an active match first."
            )
        m = mgr.matches.get(target_mid)
        if not m:
            raise NotFound(f"Match '{target_mid}' not found.")
        newly = m.bind_channel(ctx.channel_key, label=label, pov=pov)
        mgr.set_active_for_channel(ctx.channel_key, target_mid)
        bits = []
        if label is not None:
            bits.append(f"label '{label}'")
        if pov is not None:
            eff = m.channel_pov(ctx.channel_key)
            bits.append(f"POV {eff}" if eff else "POV omniscient")
        tail = f" ({', '.join(bits)})" if bits else ""
        verb = "Bound this channel to" if newly else "Updated binding of this channel to"
        return await ctx.send(
            f"{verb} **{m.name}** (`{target_mid}`){tail}. "
            f"{len(m.bound_channels)} channel(s) bound."
        )
    if sub == "unbind":
        # !match unbind [<match_id>] — unbind THIS channel.
        target_mid = args[1] if len(args) >= 2 else mgr.get_active_for_channel(ctx.channel_key)
        if not target_mid:
            return await ctx.send("❌ No match to unbind from this channel.")
        m = mgr.matches.get(target_mid)
        if not m:
            raise NotFound(f"Match '{target_mid}' not found.")
        removed = m.unbind_channel(ctx.channel_key)
        if mgr.get_active_for_channel(ctx.channel_key) == target_mid:
            mgr.active_by_channel.pop(ctx.channel_key, None)
        if not removed:
            return await ctx.send(f"This channel was not bound to `{target_mid}`.")
        return await ctx.send(
            f"Unbound this channel from **{m.name}** (`{target_mid}`). "
            f"{len(m.bound_channels)} channel(s) remain."
        )
    if sub == "channels":
        # !match channels [<match_id>] — list bound channels.
        target_mid = args[1] if len(args) >= 2 else mgr.get_active_for_channel(ctx.channel_key)
        if not target_mid:
            return await ctx.send("❌ No active match. `!match channels <id>`.")
        m = mgr.matches.get(target_mid)
        if not m:
            raise NotFound(f"Match '{target_mid}' not found.")
        if not m.bound_channels:
            return await ctx.send(f"**{m.name}** (`{target_mid}`): no bound channels.")
        lines = [f"**{m.name}** (`{target_mid}`) bound channels:"]
        for ch in sorted(m.bound_channels.keys()):
            meta = m.bound_channels[ch]
            label = meta.get("label")
            pov = meta.get("pov")
            here = " ← here" if ch == ctx.channel_key else ""
            lab = f" — '{label}'" if label else ""
            povtag = f" [POV: {pov}]" if (pov and pov != "omniscient") else " [POV: omniscient]"
            lines.append(f"- `{ch}`{lab}{povtag}{here}")
        return await ctx.send("\n".join(lines))
    if sub == "hosts":
        # !match hosts [<match_id>] — show owner + co-hosts.
        target_mid = args[1] if len(args) >= 2 else mgr.get_active_for_channel(ctx.channel_key)
        if not target_mid:
            return await ctx.send("❌ No active match. `!match hosts <id>`.")
        m = mgr.matches.get(target_mid)
        if not m:
            raise NotFound(f"Match '{target_mid}' not found.")
        owner = _mention(m.owner)
        cohosts = ", ".join(_mention(c) for c in m.cohosts) if m.cohosts else "(none)"
        return await ctx.send(
            f"**{m.name}** (`{target_mid}`)\n"
            f"- owner: {owner}\n- co-hosts: {cohosts}"
        )
    if sub == "fog":
        # !match fog [on|off]            — fog-of-war toggle
        # !match fog memory [on|off]     — explored-memory toggle
        # A !match mutation, so host-gated; bare forms report state.
        target_mid = mgr.get_active_for_channel(ctx.channel_key)
        if not target_mid:
            return await ctx.send("❌ No active match on this channel.")
        m = mgr.matches.get(target_mid)
        if not m:
            raise NotFound(f"Match '{target_mid}' not found.")

        def _truthy(v):  # returns True/False/None (None = unrecognized)
            if v in ("on", "true", "yes", "enable", "enabled"):
                return True
            if v in ("off", "false", "no", "disable", "disabled"):
                return False
            return None

        # ---- memory sub-toggle ----
        if len(args) >= 2 and args[1].lower() == "memory":
            if len(args) < 3:
                return await ctx.send(
                    f"Fog memory is **{'on' if m.fog_memory else 'off'}** "
                    f"for **{m.name}** (fog itself is "
                    f"{'on' if m.fog_enabled else 'off'})."
                )
            want = _truthy(args[2].lower())
            if want is None:
                return await ctx.send("Usage: `!match fog memory on|off`.")
            m.fog_memory = want
            if want:
                # Seed explored from every present team's current vision so
                # memory starts from what's visible right now, then grows.
                for team in {e.team for e in m.entities.values() if e.team}:
                    m._record_vision(team)
            else:
                # Off = reset: drop remembered cells (render falls back to
                # current vision only).
                m.explored.clear()
            return await ctx.send(
                f"Fog memory **{'on' if m.fog_memory else 'off'}** for "
                f"**{m.name}**."
            )

        # ---- fog on/off ----
        if len(args) < 2:
            return await ctx.send(
                f"Fog of war is **{'on' if m.fog_enabled else 'off'}** "
                f"for **{m.name}** (memory "
                f"{'on' if m.fog_memory else 'off'})."
            )
        want = _truthy(args[1].lower())
        if want is None:
            return await ctx.send("Usage: `!match fog on|off` or `!match fog memory on|off`.")
        m.fog_enabled = want
        return await ctx.send(
            f"Fog of war **{'on' if m.fog_enabled else 'off'}** for "
            f"**{m.name}**."
        )
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
    # var: match-level scratchpad vars on the ACTIVE match. The command
    # twin of the `match.<path>` formula root / match_var_* functions —
    # global GM state (alarm_level, objective_progress, weather, ...) that
    # belongs to the match rather than any entity. Dotted paths supported.
    if sub == "var":
        if await return_help_if_not_enough_args(ctx, args, 2, "match", "var"):
            return
        m = active_match(mgr, ctx)
        vsub = args[1].lower()
        if vsub == "set":
            if await return_help_if_not_enough_args(ctx, args, 4, "match", "var"):
                return
            path = args[2]
            value = _parse_scalar(args[3])
            _set_path(m.vars, path, value)
            return await ctx.send(f"match var `{path}` = {value!r}")
        if vsub == "get":
            if await return_help_if_not_enough_args(ctx, args, 3, "match", "var"):
                return
            path = args[2]
            try:
                value = _get_path(m.vars, path)
            except FormulaError:
                raise NotFound(f"Match var '{path}' is not set.")
            return await ctx.send(f"match var `{path}` = {value!r}")
        if vsub in ("del", "delete", "rm", "remove"):
            if await return_help_if_not_enough_args(ctx, args, 3, "match", "var"):
                return
            path = args[2]
            keys = path.split(".")
            cur: Any = m.vars
            for k in keys[:-1]:
                if not isinstance(cur, dict) or k not in cur:
                    raise NotFound(f"Match var '{path}' is not set.")
                cur = cur[k]
            if not isinstance(cur, dict) or keys[-1] not in cur:
                raise NotFound(f"Match var '{path}' is not set.")
            del cur[keys[-1]]
            return await ctx.send(f"Removed match var `{path}`.")
        if vsub == "list":
            if not m.vars:
                return await ctx.send("No match vars set.")
            lines = [f"- `{k}` = {v!r}" for k, v in m.vars.items()]
            return await ctx.send("**Match vars:**\n" + "\n".join(lines))
        raise VTTError(
            f"Unknown !match var subcommand '{vsub}'. "
            f"Use set / get / del / list."
        )
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
    "match", "bind",
    usage="!match bind [<id>] [label=<text>] [pov=<team|omniscient>]",
    desc=(
        "Bind THIS channel to a match (defaults to the active one) and "
        "make it active here. Multiple channels can bind one match, "
        "uncapped — e.g. a host channel plus a players channel, or one "
        "channel per team. The optional label is free-form text. "
        "`pov=<team>` sets the channel's point-of-view team: !state / "
        "!map / !list then render only what that team can see (per the "
        "entity_visibility_condition rule). `pov=omniscient` (the "
        "default) clears it — the channel sees everything, which is "
        "what a host channel wants."
    ),
)
registry.annotate_sub(
    "match", "unbind",
    usage="!match unbind [<id>]",
    desc="Unbind THIS channel from a match (defaults to the active one).",
)
registry.annotate_sub(
    "match", "channels",
    usage="!match channels [<id>]",
    desc="List every channel bound to a match, with labels.",
)
registry.annotate_sub(
    "match", "hosts",
    usage="!match hosts [<id>]",
    desc="Show a match's owner and co-hosts.",
)
registry.annotate_sub(
    "match", "fog",
    usage="!match fog [on|off] | !match fog memory [on|off]",
    desc=(
        "Toggle fog of war for the active match (bare form shows state). "
        "Per-match — seeded at creation from fog_enabled_by_default, then "
        "independent of the game system (survives rule refreshes). When "
        "on, a channel rendering from a team POV (`!match bind pov=`) "
        "only sees what its team can: cells within vision range "
        "(fog_vision_radius_var, fog_range_mode) of any alive member; "
        "unseen cells show fog_glyph and anything in them is hidden. "
        "`!match fog memory on` keeps explored cells revealed after the "
        "team leaves vision (fog_memory_mode controls whether remembered "
        "cells show everything or terrain-only); off (default) resets to "
        "current vision. Host channels (omniscient) and `!… full` bypass."
    ),
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
registry.annotate_sub(
    "match", "var",
    usage="!match var <set <path> <value> | get <path> | del <path> | list>",
    desc=(
        "Match-level scratchpad vars on the active match — global GM state "
        "(alarm_level, objective_progress, weather, ...) that belongs to "
        "the match, not any entity. Dotted paths supported. The command "
        "twin of the reserved `match.<path>` formula root and the "
        "match_var_* formula functions. Values parse as int → float → str; "
        "quote multi-word string values (`!match var set note \"red alert\"`). "
        "Match vars do NOT fire var hooks."
    )
)

# ---- host (per-match host management) -----
@registry.command(
    "host", access="owner",
    usage="!host <add|remove|list|access> ...",
    desc=(
        "Manage the active match's host list. The match's CREATOR is its "
        "owner — full command privileges and the sole manager of this "
        "list. `add <user>` appoints a co-host (same command privileges, "
        "but they can't manage hosts); `remove <user>` revokes one; "
        "`list` shows everyone. On Discord, `<user>` is a mention or "
        "user id; at the CLI it's any string identity (see `!as`). "
        "Non-host commands against the match are held for host approval "
        "(see `!approve` / `!deny`)."
    ),
)
async def host_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if not args:
        sub = "list"
    else:
        sub = args[0].lower()

    if sub == "list":
        owner = _mention(m.owner)
        cohosts = ", ".join(_mention(c) for c in m.cohosts) if m.cohosts else "(none)"
        return await ctx.send(
            f"**{m.name}** hosts\n- owner: {owner}\n- co-hosts: {cohosts}"
        )

    if sub == "access":
        # !host access <set <cmd> <level> | clear <cmd> | list>
        # Per-match overrides of the command access gate. Lets the host
        # tighten a normally-free read (fog of war: `ent dump`, `find`,
        # `map`) or loosen a gated one. Keys are "command" or
        # "command sub"; levels are all / host / host_only / owner.
        VALID = {"all", "host", "host_only", "owner"}
        asub = args[1].lower() if len(args) >= 2 else "list"
        if asub == "list":
            if not m.access_overrides:
                return await ctx.send(
                    f"**{m.name}**: no command-access overrides (engine "
                    f"defaults apply)."
                )
            lines = [f"**{m.name}** command-access overrides:"]
            for k in sorted(m.access_overrides.keys()):
                lines.append(f"- `{k}` → {m.access_overrides[k]}")
            return await ctx.send("\n".join(lines))
        if asub == "set":
            if await return_help_if_not_enough_args(ctx, args, 4, "host", "access"):
                return
            key = args[2]
            level = args[3].lower()
            if level not in VALID:
                return await ctx.send(
                    f"❌ level must be one of: {', '.join(sorted(VALID))}."
                )
            m.access_overrides[key] = level
            return await ctx.send(f"Access override set: `{key}` → {level}.")
        if asub == "clear":
            if await return_help_if_not_enough_args(ctx, args, 3, "host", "access"):
                return
            key = args[2]
            if m.access_overrides.pop(key, None) is None:
                return await ctx.send(f"No override for `{key}`.")
            return await ctx.send(f"Cleared access override for `{key}`.")
        title, body = registry.help_for(["host", "access"])
        return await ctx.send(f"**{title}**\n{body}")

    if sub in ("add", "remove"):
        if await return_help_if_not_enough_args(ctx, args, 2, "host", sub):
            return
        target = _normalize_user_token(args[1])
        if sub == "add":
            if target == m.owner:
                return await ctx.send(f"{_mention(target)} is already the owner.")
            added = m.add_cohost(target)
            if not added:
                return await ctx.send(f"{_mention(target)} is already a co-host.")
            return await ctx.send(f"Appointed {_mention(target)} as a co-host of **{m.name}**.")
        # remove
        if target == m.owner:
            return await ctx.send(
                "❌ The owner can't be removed. Owner stays for the match's life."
            )
        removed = m.remove_cohost(target)
        if not removed:
            return await ctx.send(f"{_mention(target)} is not a co-host.")
        return await ctx.send(f"Removed co-host {_mention(target)} from **{m.name}**.")

    title, body = registry.help_for(["host"])
    return await ctx.send(f"**{title}**\n{body}")


def _normalize_user_token(tok: str) -> str:
    """Normalize a user reference. A Discord mention (<@123>, <@!123>)
    reduces to the bare id so it matches the user_id the dispatcher
    sees; anything else passes through unchanged (CLI string identities,
    raw ids)."""
    t = tok.strip()
    if t.startswith("<@") and t.endswith(">"):
        t = t[2:-1]
        if t.startswith("!"):
            t = t[1:]
    return t


def _mention(user_id: Optional[str]) -> str:
    """Render a user id for display. A numeric Discord snowflake becomes a
    `<@id>` mention — on Discord that shows as a clickable @username (and
    pings them), the traditional UX. A non-numeric identity (e.g. the CLI
    `"cli"` stand-in) or an empty/None value is shown verbatim so output
    still reads sensibly off Discord. Inverse of _normalize_user_token's
    display side."""
    if not user_id:
        return "(none)"
    s = str(user_id)
    return f"<@{s}>" if s.isdigit() else f"`{s}`"


# ---- as (CLI identity switch for previewing host vs player) -----
@registry.command(
    "as", access="all",
    usage="!as <host|player|owner> [<name>] | !as view <team|omniscient|clear>",
    desc=(
        "Switch your previewing identity OR point-of-view. CLI-only "
        "(on Discord both come from your account / the channel, so this "
        "is inert). IDENTITY: `!as owner`/`!as host` restores the owner "
        "identity 'cli'; `!as player [name]` becomes a player for "
        "previewing (the CLI has no host-approval infrastructure, so the "
        "command still runs — identity here is for seeing the game AS that "
        "player, not for gating). POV (visibility "
        "preview, a SEPARATE axis from identity): `!as view <team>` "
        "renders !state/!map/!list as that team sees them; `!as view "
        "omniscient` forces the full view; `!as view clear` drops the "
        "override and falls back to the channel's bound POV. Bare `!as` "
        "reports both your identity and your effective POV."
    ),
)
async def as_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not getattr(ctx, "cli_mutable", False):
        return await ctx.send(
            "❌ `!as` only works at the CLI. On Discord your identity and "
            "view come from your account and the channel."
        )
    if not args:
        ov = getattr(ctx, "pov_override", _NO_POV)
        if ov is _NO_POV:
            pov_desc = "channel default"
        elif ov is None or ov == "omniscient":
            pov_desc = "omniscient (override)"
        else:
            pov_desc = f"team `{ov}` (override)"
        return await ctx.send(
            f"You are `{ctx_user(ctx)}`; POV: {pov_desc}. "
            f"Use `!as host`/`!as player [name]` or `!as view <team>`."
        )
    role = args[0].lower()
    if role == "view":
        target = args[1].lower() if len(args) >= 2 else "omniscient"
        if target == "clear":
            if hasattr(ctx, "pov_override"):
                delattr(ctx, "pov_override")
            return await ctx.send("POV override cleared; using the channel's bound POV.")
        if target in ("omniscient", "all", "full"):
            ctx.pov_override = "omniscient"
            return await ctx.send("POV is now omniscient (you see everything).")
        # Preserve original casing of the team name (args[1], not lowered).
        team = args[1]
        ctx.pov_override = team
        return await ctx.send(f"POV is now team `{team}`.")
    if role in ("host", "owner"):
        ctx.user_id = "cli"
        ctx.user_name = "cli"
        return await ctx.send("You are now the owner identity `cli`.")
    if role == "player":
        name = args[1] if len(args) >= 2 else "player"
        ctx.user_id = name
        ctx.user_name = name
        tail = ("" if getattr(ctx, "auto_approve", False)
                else " — mutating commands are held for host approval")
        return await ctx.send(f"You are now player `{name}` (non-host){tail}.")
    # Treat any other token as a literal identity to assume.
    ctx.user_id = role
    ctx.user_name = role
    return await ctx.send(f"You are now `{role}`.")


# ---- approval queue (host approves / denies player commands) -----
@registry.command(
    "pending", access="host_only", snapshot=False,
    usage="!pending",
    desc=(
        "List the active match's pending approval requests — player "
        "commands awaiting a host's go-ahead. Each shows an id, the "
        "requester, and the command. Approve with `!approve <id>` or "
        "reject with `!deny <id>`. Host-only."
    ),
)
async def pending_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if not m.pending_requests:
        return await ctx.send("No pending requests.")
    lines = [f"**{m.name}** pending requests:"]
    for rid in sorted(m.pending_requests.keys(), key=lambda r: int(r[1:])):
        req = m.pending_requests[rid]
        cmd = "!" + req["name"] + (" " + " ".join(req["args"]) if req["args"] else "")
        lines.append(f"- `{rid}` by {req['user_name']}: `{cmd}`")
    return await ctx.send("\n".join(lines))


@registry.command(
    "approve", access="host_only", snapshot=False,
    usage="!approve <id|all>",
    desc=(
        "Approve a pending player command and run it now (with host "
        "authority). `!approve all` runs every queued request in order. "
        "The command executes exactly as the player typed it, against "
        "this match. Host-only."
    ),
)
async def approve_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if await return_help_if_not_enough_args(ctx, args, 1, "approve"):
        return
    target = args[0]
    if target.lower() == "all":
        if not m.pending_requests:
            return await ctx.send("No pending requests.")
        rids = sorted(m.pending_requests.keys(), key=lambda r: int(r[1:]))
        for rid in rids:
            await _run_approved(m.pop_pending_request(rid), ctx, mgr)
        return
    req = m.pop_pending_request(target)
    if req is None:
        return await ctx.send(f"❌ No pending request `{target}`.")
    await _run_approved(req, ctx, mgr)


async def _run_approved(req: dict, ctx: ReplyContext, mgr: MatchManager):
    """Re-dispatch an approved request through the normal path. The
    approver is a host, so the gate lets it run directly; it snapshots
    and behaves like any host command. We announce the approval first so
    the command's own output follows it."""
    cmd = "!" + req["name"] + (" " + " ".join(req["args"]) if req["args"] else "")
    await ctx.send(f"✅ Approved `{req['id']}` ({cmd}, by {req['user_name']}).")
    await registry.run(req["name"], req["args"], ctx, mgr)


@registry.command(
    "deny", access="host_only", snapshot=False,
    usage="!deny <id|all>",
    desc=(
        "Reject a pending player command without running it. `!deny all` "
        "clears the whole queue. Host-only."
    ),
)
async def deny_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if await return_help_if_not_enough_args(ctx, args, 1, "deny"):
        return
    target = args[0]
    if target.lower() == "all":
        n = len(m.pending_requests)
        if n == 0:
            return await ctx.send("No pending requests.")
        m.pending_requests.clear()
        return await ctx.send(f"Denied all {n} pending request(s).")
    req = m.pop_pending_request(target)
    if req is None:
        return await ctx.send(f"❌ No pending request `{target}`.")
    cmd = "!" + req["name"] + (" " + " ".join(req["args"]) if req["args"] else "")
    return await ctx.send(f"🚫 Denied `{req['id']}` ({cmd}, by {req['user_name']}).")


registry.annotate_sub(
    "host", "add",
    usage="!host add <user>",
    desc="Appoint a co-host (owner only). On Discord pass a mention or id.",
)
registry.annotate_sub(
    "host", "remove",
    usage="!host remove <user>",
    desc="Revoke a co-host (owner only). The owner can't be removed.",
)
registry.annotate_sub(
    "host", "list",
    usage="!host list",
    desc="Show the match owner and co-hosts.",
)
registry.annotate_sub(
    "host", "access",
    usage="!host access <set <cmd> <level> | clear <cmd> | list>",
    desc=(
        "Per-match overrides of the command-access gate (owner only). "
        "`set <cmd> <level>` keys on a command (\"find\") or "
        "command+sub (\"ent dump\"); levels: all / host / host_only / "
        "owner. Tighten a normally-free read so it needs host approval in "
        "a fog-of-war / invisibility match, or loosen a gated one. These "
        "override the system command_access rule and survive rule "
        "refreshes. `clear <cmd>` removes one; `list` shows them all."
    ),
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

        # Normalize `\n`/`\t` so a formula-body rule (status_tick_formula,
        # default_kill/revive_function_effects, ...) or a multi-line format
        # rule set at the CLI parses/renders with real newlines. The
        # harness pre-translates these; the raw CLI does not. No-op on
        # plain scalars (numbers/bools/strings without the escape).
        raw_value = normalize_body_source(raw_value)
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

    # ---- system access <system> <set|clear|list> ---------------------
    # Dedicated editor for the command_access RULE on a GameSystem. This
    # exists because command_access is a dict-valued rule, and `!system
    # set` deliberately refuses dict/list rules (you can't express a
    # nested value in one flat key=value token, and a partial edit would
    # clobber the structure) — see _coerce_rule_value's "structured dict"
    # branch in logic.py. So dict rules each get a dedicated command:
    # `!log format` edits event_log_format, and THIS edits command_access.
    #
    # WHY TWO ACCESS-OVERRIDE LAYERS (important for future readers):
    #   1. The command_access RULE (edited here) is the SYSTEM-WIDE
    #      default. It flows into a match's `rules` snapshot at creation
    #      and on every refresh_match_rules — so editing it here updates
    #      the baseline policy for every current and future match on the
    #      system (e.g. "on my Fog-of-War system, `find` and `ent dump`
    #      are host-gated out of the box").
    #   2. Match.access_overrides (edited via `!host access`) is a
    #      PER-MATCH host tweak. It lives in its OWN field, NOT in
    #      `rules`, precisely so a refresh_match_rules (triggered by ANY
    #      `!system set` / `!system access` on the system) does NOT wipe
    #      it. The gate (CommandRegistry._effective_access) checks
    #      access_overrides FIRST, then the command_access rule, then the
    #      built-in defaults — so a per-match host decision always beats
    #      the system default.
    # That separation is the whole reason the per-match overrides survive
    # rule refreshes; don't "simplify" by moving access_overrides into
    # rules, or `!system access` here would clobber every host's tweaks.
    if sub == "access":
        if await return_help_if_not_enough_args(ctx, args, 3, "system", "access"):
            return
        name = args[1]
        action = args[2].lower()
        s = mgr.get_system(name)
        VALID = {"all", "host", "host_only", "owner"}
        # The system's current command_access dict (copy so we don't
        # mutate the stored value in place before set()).
        current = s.get("command_access")
        current = dict(current) if isinstance(current, dict) else {}

        if action == "list":
            if not current:
                return await ctx.send(
                    f"System `{name}` has no command_access overrides "
                    f"(engine defaults apply: mutating commands are host, "
                    f"read-only inspects are open)."
                )
            lines = [f"**{name}** command_access (system-wide default):"]
            for k in sorted(current.keys()):
                lines.append(f"- `{k}` → {current[k]}")
            return await ctx.send("\n".join(lines))

        if action == "set":
            if await return_help_if_not_enough_args(ctx, args, 5, "system", "access"):
                return
            key = args[3]
            level = args[4].lower()
            if level not in VALID:
                return await ctx.send(
                    f"❌ level must be one of: {', '.join(sorted(VALID))}."
                )
            current[key] = level
            s.set("command_access", current)
            refreshed = mgr.refresh_match_rules(name)
            suffix = f" ({refreshed} live match{'es' if refreshed != 1 else ''} refreshed)" if refreshed else ""
            return await ctx.send(
                f"System `{name}` command_access: `{key}` → {level}{suffix}. "
                f"Note: per-match `!host access` overrides still win."
            )

        if action == "clear":
            if await return_help_if_not_enough_args(ctx, args, 4, "system", "access"):
                return
            key = args[3]
            if key not in current:
                return await ctx.send(f"System `{name}` has no override for `{key}`.")
            del current[key]
            s.set("command_access", current)
            refreshed = mgr.refresh_match_rules(name)
            suffix = f" ({refreshed} live match{'es' if refreshed != 1 else ''} refreshed)" if refreshed else ""
            return await ctx.send(
                f"Cleared `{key}` from system `{name}` command_access{suffix}."
            )

        title, body = registry.help_for(["system", "access"])
        return await ctx.send(f"**{title}**\n{body}")

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
    "system", "access",
    usage="!system access <system> <set <key> <level> | clear <key> | list>",
    desc=(
        "Edit a GameSystem's command_access rule — the SYSTEM-WIDE "
        "default access policy inherited by every match on the system "
        "(command_access is a dict rule, so `!system set` can't touch "
        "it). `set <key> <level>` keys on a command (\"find\") or "
        "command+sub (\"ent dump\"); levels: all / host / host_only / "
        "owner. Refreshes live matches. This is the BASELINE — per-match "
        "`!host access` overrides take precedence and survive this "
        "refresh. Use it for a system whose matches should default to "
        "fog-of-war read restrictions; use `!host access` for one match."
    ),
)
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

    # push (forced movement) — !ent push <id> <dir> [n]. Thin wrapper over
    # Match.push_entity (shared with the push_entity() formula primitive):
    # the geometry, occupancy stop, diagonal gating and per-step hook
    # firing all live there.
    if sub == "push":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "push"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        n = 1
        if len(args) >= 4:
            try:
                n = int(args[3])
            except ValueError:
                return await ctx.send(
                    f"Push distance must be an integer, got '{args[3]}'."
                )
        try:
            steps, hook_log = m.push_entity(eid, args[2], n)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        e = m.entities[eid]
        msg = f"Pushed `{eid}` {steps} cell(s) to ({e.x},{e.y})."
        if hook_log:
            msg = msg + "\n" + "\n".join(hook_log)
        return await ctx.send(msg)

    # pull — !ent pull <id> <x> <y> [n]. Forced movement TOWARD a point.
    # Wraps Match.pull_entity (shared with the pull_entity() formula
    # primitive): drags the entity up to n cells toward (x,y), stopping
    # at the first blocked cell or on reaching the target.
    if sub == "pull":
        if await return_help_if_not_enough_args(ctx, args, 4, "ent", "pull"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        try:
            tx = int(args[2]); ty = int(args[3])
        except ValueError:
            return await ctx.send(
                f"Pull target must be integer x and y, got "
                f"'{args[2]}' '{args[3]}'."
            )
        n = 1
        if len(args) >= 5:
            try:
                n = int(args[4])
            except ValueError:
                return await ctx.send(
                    f"Pull distance must be an integer, got '{args[4]}'."
                )
        try:
            steps, hook_log = m.pull_entity(eid, tx, ty, n)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        e = m.entities[eid]
        msg = f"Pulled `{eid}` {steps} cell(s) toward ({tx},{ty}) to ({e.x},{e.y})."
        if hook_log:
            msg = msg + "\n" + "\n".join(hook_log)
        return await ctx.send(msg)

    # swap — !ent swap <id1> <id2>. Wraps Match.swap_entities (shared with
    # the swap_entities() formula primitive).
    if sub == "swap":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "swap"):
            return
        aid = _resolve_eid(m, args[1])
        bid = _resolve_eid(m, args[2])
        if aid not in m.entities:
            raise NotFound(f"Entity '{aid}' not found.")
        if bid not in m.entities:
            raise NotFound(f"Entity '{bid}' not found.")
        try:
            swapped, hook_log = m.swap_entities(aid, bid)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        if not swapped:
            return await ctx.send(
                f"No swap: `{aid}` and `{bid}` are the same entity "
                f"or already share a cell."
            )
        ea = m.entities[aid]; eb = m.entities[bid]
        msg = (
            f"Swapped `{aid}` ↔ `{bid}`; now at "
            f"({ea.x},{ea.y}) and ({eb.x},{eb.y})."
        )
        if hook_log:
            msg = msg + "\n" + "\n".join(hook_log)
        return await ctx.send(msg)

    # kill — !ent kill <id>. Unconditional death (bypasses the death
    # CONDITION). Wraps Match.kill_entity (shared with the kill() formula
    # primitive): runs default_kill_function_effects then the death
    # pipeline (on_death, corpse-or-delete, turn-order rebuild).
    if sub == "kill":
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "kill"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        killed, hook_log = m.kill_entity(eid)
        msg = f"Killed `{eid}`." if killed else f"`{eid}` could not be killed."
        if hook_log:
            msg = msg + "\n" + "\n".join(hook_log)
        return await ctx.send(msg)

    # revive — !ent revive <corpse_id>. Resurrect a stored corpse. Wraps
    # Match.revive_corpse (shared with the revive() formula primitive):
    # spawns the entity back at the corpse tile, runs
    # default_revive_function_effects, fires on_revive. The id here is a
    # CORPSE id (not a live entity), so it's used verbatim — no
    # this/current resolution.
    if sub == "revive":
        if await return_help_if_not_enough_args(ctx, args, 2, "ent", "revive"):
            return
        cid = args[1]
        try:
            new_id, hook_log = m.revive_corpse(cid)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        msg = f"Revived corpse `{cid}` as `{new_id}`."
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
        # Clones inherit the source's vars, so a stackable source
        # produces stackable clones — they bypass the per-cell
        # occupancy check the same way Entity.spawn does.
        src_stackable = m.entities[src_id].is_cell_stackable
        # Honor the corpse_id_uniqueness rule: a clone's id is taken
        # if it's a live entity OR (under the default rule) a corpse.
        taken_ids = m.entities.keys() if not bool(m.rules.get("corpse_id_uniqueness", True)) else m._taken_entity_ids()
        # Build the (id, x, y) plan and validate everything up front.
        plan: List[Tuple[str, int, int]] = []
        seen_cells = set()
        seen_ids = set()
        for i, (x, y) in enumerate(pairs, start=1):
            cid = new_id_base if single else f"{new_id_base}{i}"
            if cid in taken_ids or cid in seen_ids:
                return await ctx.send(
                    f"❌ entity id `{cid}` already exists (or is "
                    f"duplicated in this batch)."
                )
            if not m.in_bounds(x, y):
                return await ctx.send(
                    f"❌ ({x},{y}) outside {m.grid_width}x{m.grid_height}."
                )
            if (x, y) in seen_cells or (not src_stackable and m.is_occupied(x, y)):
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
        # Advisory (non-blocking): the render `color` var is the top-level
        # `color`; an unrecognized name renders uncolored, so flag a likely
        # typo while still honoring the write (the value may be intended for
        # the GM's own formulas, not rendering).
        if key_path == "color" and isinstance(value, str) and value:
            from logic import TEXT_COLORS
            if value not in TEXT_COLORS:
                ack += f"\n⚠ `{value}` isn't a recognized render color (renders uncolored). " + _color_guide()
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

    # ---- !ent action <id> <name> [target] [k=v...] ----
    # Alias form for !action <id> <name> [...]. Routes through the
    # same _run_action_dispatch helper so behavior is identical —
    # this is purely a syntactic convenience for users who already
    # think in "ent <id> <verb>" patterns.
    if sub == "action":
        if await return_help_if_not_enough_args(ctx, args, 3, "ent", "action"):
            return
        actor_id = _resolve_eid(m, args[1])
        if actor_id not in m.entities:
            raise NotFound(f"Entity '{actor_id}' not found.")
        await _run_action_dispatch(
            ctx, mgr, m, actor_id=actor_id,
            requested_name=args[2], tail_tokens=list(args[3:]),
        )
        return

    # ---- !ent store_entity_into_var <src_id> <dest_id> <dest_path> ----
    # Snapshot a (configured) entity into a summon-ready template dict
    # and store it at dest's vars[dest_path]. This is the ergonomic way
    # to BUILD a summon template: configure a real "Fire Elemental"
    # entity once, then capture it into the summoner's vars. Sugar over
    # the entity_snapshot() + var_set() formula primitives. Snapshot
    # strips id/position so the stored dict is a clean blueprint.
    if sub in ("store_entity_into_var", "store_entity"):
        if await return_help_if_not_enough_args(ctx, args, 4, "ent", "store_entity_into_var"):
            return
        src_id = _resolve_eid(m, args[1])
        if src_id not in m.entities:
            raise NotFound(f"Source entity '{src_id}' not found.")
        dest_id = _resolve_eid(m, args[2])
        if dest_id not in m.entities:
            raise NotFound(f"Destination entity '{dest_id}' not found.")
        dest_path = args[3]
        template = m.entity_template_dict(m.entities[src_id])
        m.entities[dest_id].write_var(dest_path, template)
        return await ctx.send(
            f"Stored a template of `{src_id}` into `{dest_id}` "
            f"vars.{dest_path} (id/position stripped — ready to "
            f"`summon_from('{dest_path}', x, y)`)."
        )

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
    "ent", "push",
    usage="!ent push <id> <dir> [n]",
    desc=(
        "Forced movement: shove <id> up to n cells (default 1) in <dir>, "
        "stopping at the first wall or occupied cell. Honors "
        "'allow_diagonal_movement'. Walks cell by cell, firing per-step "
        "tile hooks — the command equivalent of the push_entity() formula "
        "primitive."
    ),
)
registry.annotate_sub(
    "ent", "pull",
    usage="!ent pull <id> <x> <y> [n]",
    desc=(
        "Forced movement TOWARD a point: drag <id> up to n cells (default "
        "1) toward (x,y), stopping at the first wall/occupied cell or on "
        "reaching the target. The point-directed twin of !ent push — the "
        "heading recomputes each step, so the path curves toward (x,y). "
        "Honors 'allow_diagonal_movement'. Walks cell by cell, firing "
        "per-step tile hooks. Command equivalent of the pull_entity() "
        "formula primitive."
    ),
)
registry.annotate_sub(
    "ent", "swap",
    usage="!ent swap <id1> <id2>",
    desc=(
        "Atomically exchange the positions of two entities (bypasses the "
        "occupancy check, since they occupy each other's cells). Fires "
        "on_exit/on_enter/on_stop and on_entity_moved for both. Command "
        "equivalent of the swap_entities() formula primitive."
    ),
)
registry.annotate_sub(
    "ent", "kill",
    usage="!ent kill <id>",
    desc=(
        "Unconditionally kill <id>, bypassing the death CONDITION. Runs "
        "the 'default_kill_function_effects' rule then the death pipeline "
        "(on_death, corpse-or-delete per 'death_result', turn-order "
        "rebuild). Command equivalent of the kill() formula primitive."
    ),
)
registry.annotate_sub(
    "ent", "revive",
    usage="!ent revive <corpse_id>",
    desc=(
        "Resurrect a stored corpse by its id: respawns the entity at the "
        "corpse tile, runs 'default_revive_function_effects', fires "
        "on_revive. The id is a CORPSE id (see `!list` Dead: section), not "
        "a live entity. Command equivalent of the revive() formula "
        "primitive."
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
    "ent", "action",
    usage="!ent action <id> <name> [target...] [k=v ...]",
    desc=(
        "Alias form for `!action <id> <name> [...]` — invokes a "
        "discovered action on `<id>`. See `!help action` for the "
        "full body language and target/args semantics."
    ),
)
registry.annotate_sub(
    "ent", "store_entity_into_var",
    usage="!ent store_entity_into_var <src_id> <dest_id> <dest_path>",
    desc=(
        "Snapshot `<src_id>` into a summon-ready template dict (id and "
        "position stripped) and store it at `<dest_id>`'s "
        "vars.<dest_path>. The ergonomic way to build a summon "
        "blueprint: configure a real entity, then capture it into the "
        "summoner's vars (or a tile via formulas). Afterwards a "
        "formula can instantiate copies with "
        "`summon_from('<dest_path>', x, y)` (template on `self`) or "
        "`summon(var_get('<dest_id>','<dest_path>'), x, y)`. Alias: "
        "`store_entity`."
    ),
)
registry.annotate_sub(
    "action", "list",
    usage="!action list <eid>",
    desc=(
        "Enumerate every discoverable action on `<eid>` — one line "
        "per action with its target type, container path, and "
        "description. Respects the action_container_* rules the "
        "same way the invocation path does."
    ),
)
registry.annotate_sub(
    "action", "info",
    usage="!action info <eid> <name>",
    desc=(
        "Full detail for one action on `<eid>` — path, container, "
        "target type, description, and the body source. Accepts a "
        "bare name (with disambiguation menu on collision) or a "
        "full vars path."
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
@registry.command("match_toplevel", access="all", usage="!match_toplevel", desc="Show active match summary (name/id/round number).")
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

# Sentinel distinguishing "no transient POV override on this ctx" from
# "override is set to omniscient" (both render everything, but absence
# means fall back to the channel binding).
_NO_POV = object()


def _view_pov(ctx: ReplyContext, m: "Match", args: List[str]) -> Optional[str]:
    """Resolve the POV team for a render/list command. Returns a team
    string, or None for an omniscient view (no filtering). Precedence:
      1. a `full` first-arg forces omniscient (host-gated by the access
         layer via ELEVATED_ARGS);
      2. a transient ctx POV override (CLI `!as view <team>`), which lets
         a tester preview a team's fog without mutating the binding;
      3. the channel's bound POV (Match.channel_pov)."""
    if args and args[0].lower() == "full":
        return None
    ov = getattr(ctx, "pov_override", _NO_POV)
    if ov is not _NO_POV:
        return None if (ov is None or ov == "omniscient") else str(ov)
    return m.channel_pov(ctx.channel_key)


def _map_block(ctx: ReplyContext, m, pov) -> str:
    """The fenced ASCII map. Colorizes only when the surface declares
    `supports_color` AND the match has color enabled; uses an ```ansi
    fence so Discord renders the ANSI codes (a no-op label on a terminal /
    the harness, which get plain glyphs anyway)."""
    colorize = bool(getattr(ctx, "supports_color", False)) and getattr(m, "color_enabled", True)
    body = m.render_ascii(pov, colorize=colorize)
    fence = "ansi" if colorize else ""
    return f"```{fence}\n{body}\n```"


def _color_guide() -> str:
    """Human-readable list of the supported render color names. The palette
    is deliberately the Discord-safe set (30-37 + bold), so every listed
    name renders on Discord's ansi blocks AND ANSI terminals — there's no
    'terminal-only' color to warn separately about."""
    from logic import TEXT_COLORS
    names = ", ".join(sorted(TEXT_COLORS))
    return (
        f"Supported colors: {names}. They all render in Discord ansi code "
        f"blocks and ANSI terminals (the `bright_*` names are bold variants, "
        f"since Discord's ansi has no separate bright range). An "
        f"unrecognized name renders uncolored."
    )


@registry.command("map", access="all", usage="!map [full] | !map resize <w> <h> [anchor] | !map color on|off | !map teamcolor <team> <color>|clear|list | !map colors", desc="Render the ASCII map for the active match, from this channel's POV. `!map full` (host-gated) forces the omniscient view. `!map resize <w> <h> [anchor]` (host-gated) changes the grid size. `!map color on|off` toggles colorized rendering for this match; `!map teamcolor <team> <color>` sets a team's text color (also: clear / list); `!map colors` lists the supported color names. Colors apply on color-capable surfaces (Discord/terminal); an entity's own `color` var overrides its team color.")
async def map_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if args and args[0].lower() == "colors":
        return await ctx.send(_color_guide())
    if args and args[0].lower() == "color":
        if len(args) < 2 or args[1].lower() not in ("on", "off"):
            return await ctx.send("Usage: `!map color on|off`.")
        m.color_enabled = (args[1].lower() == "on")
        return await ctx.send(
            f"Colorized rendering {'ON' if m.color_enabled else 'OFF'} for "
            f"**{m.name}** (applies on color-capable surfaces)."
        )
    if args and args[0].lower() == "teamcolor":
        from logic import TEXT_COLORS
        if len(args) < 2:
            return await ctx.send(
                "Usage: `!map teamcolor <team> <color>` | `clear <team>` | `list`.")
        op = args[1].lower()
        if op == "list":
            if not m.team_colors:
                return await ctx.send("No team colors set.")
            lines = ["Team colors:"] + [
                f"  `{t}` → {c}" for t, c in sorted(m.team_colors.items())]
            return await ctx.send("\n".join(lines))
        if op == "clear":
            if len(args) < 3:
                return await ctx.send("Usage: `!map teamcolor clear <team>`.")
            m.team_colors.pop(args[2], None)
            return await ctx.send(f"Cleared team color for `{args[2]}`.")
        # set: !map teamcolor <team> <color>
        if len(args) < 3:
            return await ctx.send("Usage: `!map teamcolor <team> <color>`.")
        team, color = args[1], args[2].lower()
        if color not in TEXT_COLORS:
            return await ctx.send(f"❌ unknown color `{color}`. {_color_guide()}")
        m.team_colors[team] = color
        return await ctx.send(f"Team `{team}` renders in {color}.")
    if args and args[0].lower() == "resize":
        if len(args) < 3:
            return await ctx.send(
                "Usage: `!map resize <width> <height> [anchor]` (anchor "
                "default top-left; e.g. center, bottom-right)."
            )
        try:
            new_w, new_h = int(args[1]), int(args[2])
        except ValueError:
            return await ctx.send("Width and height must be integers.")
        anchor = args[3] if len(args) >= 4 else "top-left"
        try:
            summary, log = m.resize_grid(new_w, new_h, anchor)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        msg = (f"Resized **{m.name}** to {new_w}x{new_h} "
               f"(anchor {summary['anchor']}).")
        notes: List[str] = []
        if summary["killed"]:
            notes.append("killed " + ", ".join(f"`{i}`" for i in summary["killed"]))
        if summary["dropped_tiles"]:
            notes.append(f"dropped {summary['dropped_tiles']} tile cell(s)")
        if summary["clipped_zone_cells"]:
            notes.append(f"clipped {summary['clipped_zone_cells']} zone cell(s)")
        if notes:
            msg = msg + " " + "; ".join(notes) + "."
        if log:
            msg = msg + "\n" + "\n".join(log)
        return await ctx.send(msg + "\n" + _map_block(ctx, m, _view_pov(ctx, m, [])))
    pov = _view_pov(ctx, m, args)
    return await ctx.send(_map_block(ctx, m, pov))

@registry.command("list", access="all", usage="!list [full]", desc="List entities (turn order) from this channel's POV, plus a Dead: section of corpses when show_corpses_in_entity_list is enabled. `!list full` (host-gated) ignores visibility.")
async def list_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    pov = _view_pov(ctx, m, args)
    es = m.entities_in_turn_order()
    active_id = m.turn_order[m.active_index] if m.turn_order else None
    lines: List[str] = []
    # Filter out entities hidden from this POV (omniscient pov=None keeps
    # all). Corpses (the Dead: section below) are filtered too, via
    # corpse_visible_to.
    visible = [e for e in es if m.entity_visible_to(e.id, pov)]
    if visible:
        lines.append("Entities:")
        for e in visible:
            marker = "→" if e.id == active_id else "  "
            lines.append(f"{marker} {_entity_line(e)}")
    # Dead: section. Each corpse rendered via _corpse_line which honors
    # the corpse_line_format rule (the corpse equivalent of
    # entity_line_format — no hardcoded shape). The tile coords (NOT
    # the embedded snapshot's x/y) are authoritative for position.
    # Gated by show_corpses_in_entity_list — when false we silently
    # omit the section (corpses still exist in tile data).
    if bool(m.rules.get("show_corpses_in_entity_list", True)):
        corpses = [(x, y, eid, c) for (x, y, eid, c) in m.all_corpses()
                   if m.corpse_visible_to(eid, c, x, y, pov)]
        if corpses:
            if lines:
                lines.append("")  # blank line separates living from dead
            lines.append("Dead:")
            for (x, y, eid, corpse) in corpses:
                lines.append(f"   {_corpse_line(m, x, y, eid, corpse)}")
    if not lines:
        return await ctx.send("(no entities)")
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
        "action"  — action:NAME            (value is None, key is NAME)
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
    if token.startswith("action:"):
        name = token[len("action:"):]
        if not name:
            raise VTTError("`action:` predicate needs an action name.")
        return "action", name, None
    # Try operators longest-first so `<=` isn't misread as `<`.
    for op in _FIND_OPS:
        idx = token.find(op)
        if idx > 0:  # key must be non-empty
            return op, token[:idx], token[idx + len(op):]
    raise VTTError(
        f"Unrecognized find predicate `{token}`. Expected `key=value`, "
        f"`key<value`, `status:NAME`, `group:NAME`, or `action:NAME`."
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
        if kind == "action":
            # Action discovery walks the vars tree and respects the
            # match's container-scope rules — same lookup the
            # !action handler uses, so a `!find action:slice` query
            # answer is consistent with "will `!action <eid> slice`
            # find anything?".
            from action import discover_actions, lookup_action
            actions = discover_actions(e, m.rules)
            if not lookup_action(actions, key, m.rules):
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
    "find", access="all",
    usage="!find <predicate> [<predicate> ...]",
    desc=(
        "Query entities by AND-ed predicates. Predicate forms: "
        "`var=value`, `var!=value`, `var<value`, `var<=value`, "
        "`var>value`, `var>=value` for vars; `status:NAME` for a status "
        "flag; `group:NAME` for group membership; `action:NAME` for "
        "discoverable-action availability. Dotted var paths walk "
        "nested dicts (`inventory.sword.damage>5`). Example: "
        "`!find team=red hp<20 status:bleeding action:slice` lists "
        "every red-team entity below 20 HP that is bleeding AND has "
        "a `slice` action available."
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


@registry.command("state", access="all", usage="!state [full]", desc="Show match summary, entities, and map from this channel's POV. `!state full` (host-gated) forces the omniscient view.")
async def state_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    # New behavior: show list (turn-order) then map. The `full` arg (when
    # present) flows through to list_cmd / map_cmd, which resolve it to
    # the omniscient POV; match_top_cmd ignores it.
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
        # Translate `\n`/`\t` so a multi-line body typed at the CLI
        # (where shlex preserves literal backslash-n) compiles. See
        # formula.normalize_body_source.
        formula = normalize_body_source(" ".join(formula_parts).strip())

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
        # Same `\n`/`\t` normalization as entity-scoped !passive add.
        formula = normalize_body_source(" ".join(formula_parts).strip())

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


@registry.command(
    "defpassive",
    usage="!defpassive <add|remove|list> ...",
    desc=(
        "Manage the active match's game-system `default_entity_passives` "
        "rule — passive specs copied onto EVERY entity at creation (via "
        "!ent add, summon, or revive). Injection only: removable from an "
        "individual entity afterward and not re-enforced (until that "
        "entity is re-spawned). The passive analog of !gclamp for "
        "default_clamps; edits persist on the GameSystem and apply to "
        "every match using it."
    ),
)
async def defpassive_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["defpassive"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    # Edits live on the GameSystem (persist across saves, apply to every
    # match on this system); we refresh live matches so the change takes
    # effect immediately. Same pattern as !gclamp.
    sys_name = m.system_name
    sysobj = mgr.get_system(sys_name)

    def _read_specs() -> List[Dict[str, Any]]:
        cur = sysobj.settings.get("default_entity_passives")
        if cur is None:
            return list(DEFAULT_SYSTEM_SETTINGS.get("default_entity_passives", []))
        return list(cur.value or [])

    def _write_specs(new_list: List[Dict[str, Any]]) -> int:
        sysobj.set("default_entity_passives", new_list)
        return mgr.refresh_match_rules(sys_name)

    if sub == "hooks":
        return await ctx.send(
            "**Passive hooks:** " + ", ".join(f"`{h}`" for h in sorted(HOOK_NAMES))
        )

    if sub == "add":
        # !defpassive add <id> <when> [target=PATH] [scope=...] "<formula>"
        # Body parsing mirrors !gpassive add exactly.
        if await return_help_if_not_enough_args(ctx, args, 4, "defpassive", "add"):
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
        formula = normalize_body_source(" ".join(formula_parts).strip())
        if not formula:
            raise VTTError("Passive formula cannot be empty.")
        try:
            validate_program(formula, known_funcs=frozenset(m.formula_functions.keys()))
        except FormulaError as ex:
            raise VTTError(f"Invalid passive formula: {ex}")
        # Construct the Passive to validate id / when / scope up front
        # (Passive.__post_init__ raises VTTError on a bad hook or scope),
        # then store its serialized form on the rule.
        spec = Passive(
            id=pid, when=when, formula=formula,
            target=target_val, scope=scope_val,
        )
        existing = _read_specs()
        if any(isinstance(s, dict) and s.get("id") == pid for s in existing):
            raise DuplicateId(
                f"System '{sys_name}' already has a default passive "
                f"'{pid}'. Remove it first with !defpassive remove."
            )
        existing.append(spec.to_dict())
        refreshed = _write_specs(existing)
        from logic import VAR_HOOKS
        scope_note = ""
        if when in VAR_HOOKS:
            scope_note = f" watching `{target_val or '(root)'}` scope=`{scope_val}`"
        return await ctx.send(
            f"Added default passive `{pid}` ({when}){scope_note} to system "
            f"`{sys_name}` [refreshed {refreshed} live "
            f"match{'es' if refreshed != 1 else ''}]. Applies to entities "
            f"created from now on."
        )

    if sub in ("remove", "del", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 2, "defpassive", "remove"):
            return
        pid = args[1]
        existing = _read_specs()
        new_list = [
            s for s in existing
            if not (isinstance(s, dict) and s.get("id") == pid)
        ]
        if len(new_list) == len(existing):
            raise NotFound(
                f"No default passive '{pid}' in system '{sys_name}'."
            )
        refreshed = _write_specs(new_list)
        return await ctx.send(
            f"Removed default passive `{pid}` from system `{sys_name}` "
            f"[refreshed {refreshed} live "
            f"match{'es' if refreshed != 1 else ''}]. Already-spawned "
            f"entities keep their copy."
        )

    if sub == "list":
        existing = _read_specs()
        if not existing:
            return await ctx.send(
                f"System `{sys_name}` has no default entity passives."
            )
        lines = [f"**System `{sys_name}` default entity passives:**"]
        for sd in existing:
            try:
                p = Passive.from_dict(sd)
                lines.append(_format_passive_line(p.id, p))
            except (VTTError, KeyError, TypeError) as ex:
                lines.append(f"- ⚠️ malformed: {sd!r} ({ex})")
        return await ctx.send("\n".join(lines))

    raise VTTError(f"Unknown !defpassive subcommand: {sub}")


registry.annotate_sub(
    "defpassive", "add",
    usage='!defpassive add <id> <when> [target=PATH] [scope=exact|children|deep] "<formula>"',
    desc=(
        "Add a default passive to the game system: it is copied onto every "
        "entity at creation (add / summon / revive), applied before "
        "on_entity_spawned fires. Same id/when/target/scope/formula syntax "
        "as !passive add but without the <entity_id> (it's not bound to one "
        "entity). <when> must be a hook name (see !defpassive hooks). "
        "Removable per-entity afterward; not re-enforced."
    ),
)
registry.annotate_sub(
    "defpassive", "remove",
    usage="!defpassive remove <id>",
    desc=(
        "Remove a default passive from the game system (stops injecting it "
        "into NEW entities; already-spawned entities keep their copy). "
        "Aliases: del, rm."
    ),
)
registry.annotate_sub(
    "defpassive", "list",
    usage="!defpassive list",
    desc="List the default entity passives for the active match's game system.",
)
registry.annotate_sub(
    "defpassive", "hooks",
    usage="!defpassive hooks",
    desc="List the available passive hook names.",
)


# ---- !defvar -----------------------------------------------------------
# Manage the game system's default_entity_vars rule: var defaults applied
# to every entity at creation, filling only missing vars. The var analog
# of !defpassive (default_entity_passives) / !gclamp (default_clamps);
# same GameSystem-persist + refresh-live-matches pattern.
@registry.command(
    "defvar",
    usage="!defvar <add|remove|list> ...",
    desc=(
        "Manage the active match's game-system `default_entity_vars` rule "
        "— var defaults applied to EVERY entity at creation (!ent add, "
        "summon, revive), filling only vars the entity doesn't already "
        "have. Applied before vital-var validation, so a default can even "
        "supply a required var like hp. The var analog of !defpassive / "
        "!gclamp; edits persist on the GameSystem and refresh live "
        "matches. Values coerce like !ent set_var (true/false -> bool, "
        "then int, float, else string); dotted paths nest."
    ),
)
async def defvar_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["defvar"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    # Edits live on the GameSystem (persist across saves, apply to every
    # match on this system); refresh live matches so it takes effect now.
    sys_name = m.system_name
    sysobj = mgr.get_system(sys_name)

    def _read_vars() -> Dict[str, Any]:
        cur = sysobj.settings.get("default_entity_vars")
        if cur is None:
            return dict(DEFAULT_SYSTEM_SETTINGS.get("default_entity_vars", {}))
        return dict(cur.value or {})

    def _write_vars(new_map: Dict[str, Any]) -> int:
        sysobj.set("default_entity_vars", new_map)
        return mgr.refresh_match_rules(sys_name)

    if sub == "add":
        # !defvar add <path> <value>  (quote a value with spaces)
        if await return_help_if_not_enough_args(ctx, args, 3, "defvar", "add"):
            return
        path = args[1]
        value = _parse_scalar(args[2])  # same coercion as !ent set_var
        existing = _read_vars()
        existing[path] = value
        refreshed = _write_vars(existing)
        return await ctx.send(
            f"Default var `{path}` = {value!r} on system `{sys_name}` "
            f"[refreshed {refreshed} live "
            f"match{'es' if refreshed != 1 else ''}]. Applies to entities "
            f"created from now on (fills only when the var is missing)."
        )

    if sub in ("remove", "del", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 2, "defvar", "remove"):
            return
        path = args[1]
        existing = _read_vars()
        if path not in existing:
            raise NotFound(f"No default var '{path}' in system '{sys_name}'.")
        del existing[path]
        refreshed = _write_vars(existing)
        return await ctx.send(
            f"Removed default var `{path}` from system `{sys_name}` "
            f"[refreshed {refreshed} live "
            f"match{'es' if refreshed != 1 else ''}]. Already-spawned "
            f"entities keep their value."
        )

    if sub == "list":
        existing = _read_vars()
        if not existing:
            return await ctx.send(
                f"System `{sys_name}` has no default entity vars."
            )
        lines = [f"**System `{sys_name}` default entity vars:**"]
        for k in sorted(existing.keys()):
            lines.append(f"- `{k}` = {existing[k]!r}")
        return await ctx.send("\n".join(lines))

    raise VTTError(f"Unknown !defvar subcommand: {sub}")


registry.annotate_sub(
    "defvar", "add",
    usage="!defvar add <path> <value>",
    desc=(
        "Set a default var on the game system: copied onto every entity "
        "at creation (add / summon / revive) that lacks it, before "
        "vital-var validation. Value coerces like !ent set_var "
        "(true/false -> bool, int, float, else string; quote for spaces). "
        "Dotted <path> nests (e.g. inventory.gold). Re-adding overwrites "
        "the default."
    ),
)
registry.annotate_sub(
    "defvar", "remove",
    usage="!defvar remove <path>",
    desc=(
        "Remove a default var from the game system (stops filling it into "
        "NEW entities; already-spawned entities keep their value). "
        "Aliases: del, rm."
    ),
)
registry.annotate_sub(
    "defvar", "list",
    usage="!defvar list",
    desc="List the default entity vars for the active match's game system.",
)


# ---- !schedule ---------------------------------------------------------
# Scheduled / delayed effects: a formula body queued to run at a future
# round (match-level) or after a future number of one entity's turns
# (entity-attached). Mostly driven from formulas via schedule() /
# schedule_on() / cancel_schedule(); this command is the GM-facing twin
# for creating, inspecting, and cancelling them by hand.
def _schedule_line(s: Dict[str, Any]) -> str:
    name = s.get("name", "?")
    body = s.get("body", "")
    if s.get("kind") == "round":
        return f"- `{name}` (match) @ round {s.get('fire_round')}: `{body}`"
    return (
        f"- `{name}` on `{s.get('eid')}` @ in {s.get('turns_left')} "
        f"turn(s): `{body}`"
    )


@registry.command(
    "schedule",
    usage="!schedule <list | cancel <name> | round <delay> <body> | turn <eid> <delay> <body>>",
    desc=(
        "Manage scheduled / delayed effects on the active match. `round "
        "<delay> <body>` queues a MATCH-level body to run at round-start "
        "<delay> rounds from now (no `self` bound). `turn <eid> <delay> "
        "<body>` queues an ENTITY-attached body to run at <eid>'s "
        "turn-start after <delay> of its turns (`self`=<eid>, dropped if "
        "it dies). `list` shows pending; `cancel <name>` removes by name. "
        "Command twin of the schedule() / schedule_on() / cancel_schedule() "
        "formula primitives. Use \\n in a body for multiple statements."
    ),
)
async def schedule_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["schedule"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    if sub == "list":
        if not m.scheduled:
            return await ctx.send("No scheduled effects pending.")
        lines = ["**Scheduled effects:**"]
        lines.extend(_schedule_line(s) for s in m.scheduled)
        return await ctx.send("\n".join(lines))

    if sub in ("cancel", "del", "rm", "remove"):
        if await return_help_if_not_enough_args(ctx, args, 2, "schedule", "cancel"):
            return
        n = m.cancel_scheduled(args[1])
        if n == 0:
            raise NotFound(f"No scheduled effect named '{args[1]}'.")
        return await ctx.send(
            f"Cancelled {n} scheduled effect(s) named `{args[1]}`."
        )

    if sub == "round":
        if await return_help_if_not_enough_args(ctx, args, 3, "schedule", "round"):
            return
        try:
            delay = int(args[1])
        except ValueError:
            return await ctx.send(f"Delay must be an integer, got '{args[1]}'.")
        body = " ".join(args[2:])
        try:
            name = m.add_scheduled(delay, body)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        return await ctx.send(
            f"Scheduled match effect `{name}` for round "
            f"{m.round_number + delay}."
        )

    if sub == "turn":
        if await return_help_if_not_enough_args(ctx, args, 4, "schedule", "turn"):
            return
        eid = _resolve_eid(m, args[1])
        try:
            delay = int(args[2])
        except ValueError:
            return await ctx.send(f"Delay must be an integer, got '{args[2]}'.")
        body = " ".join(args[3:])
        try:
            name = m.add_scheduled_on(eid, delay, body)
        except VTTError as e:
            return await ctx.send(f"❌ {e}")
        return await ctx.send(
            f"Scheduled effect `{name}` on `{eid}` for {delay} turn(s) "
            f"from now."
        )

    raise VTTError(
        f"Unknown !schedule subcommand '{sub}'. "
        f"Use list / cancel / round / turn."
    )


registry.annotate_sub(
    "schedule", "list",
    usage="!schedule list",
    desc="List pending scheduled effects (name, target, when, body).",
)
registry.annotate_sub(
    "schedule", "cancel",
    usage="!schedule cancel <name>",
    desc="Cancel every pending scheduled effect with this name. Aliases: del, rm.",
)
registry.annotate_sub(
    "schedule", "round",
    usage="!schedule round <delay> <body>",
    desc=(
        "Queue a MATCH-level effect: <body> runs at round-start <delay> "
        "rounds from now (delay>=1). No `self` bound — use explicit ids or "
        "`this`. Twin of the schedule() formula primitive."
    ),
)
registry.annotate_sub(
    "schedule", "turn",
    usage="!schedule turn <eid> <delay> <body>",
    desc=(
        "Queue an ENTITY-attached effect: <body> runs at <eid>'s "
        "turn-start after <delay> of its turns (delay>=1), with `self`= "
        "<eid>. Dropped if the entity dies/despawns first. Twin of the "
        "schedule_on() formula primitive."
    ),
)


# ---- !log --------------------------------------------------------------
# The structured event log (combat log). The engine appends curated
# events at its chokepoints and the log() formula primitive appends
# custom ones; this command renders them (per-type templates) and manages
# the format overrides + clearing.
def _render_event(m: Match, entry: Dict[str, Any]) -> str:
    """Render one event-log entry to text. Override template (from the
    event_log_format rule) wins over the built-in default; an unknown
    type with neither renders as a compact key=value dump."""
    etype = entry.get("type", "?")
    overrides = m.rules.get("event_log_format", {})
    tmpl = overrides.get(etype) if isinstance(overrides, dict) else None
    if not tmpl:
        tmpl = EVENT_LOG_DEFAULT_FORMATS.get(etype)
    if not tmpl:
        kv = " ".join(f"{k}={v}" for k, v in entry.items() if k != "type")
        return f"[{etype}] {kv}"
    return _render_template(tmpl, entry)


async def _log_format_sub(ctx, fmt_args, mgr, m):
    """Handle `!log format ...` — read/edit the event_log_format dict
    rule on the active match's game system (gclamp-style: persist on the
    system, refresh live matches)."""
    sys_name = m.system_name
    sysobj = mgr.get_system(sys_name)

    def _read() -> Dict[str, str]:
        cur = sysobj.settings.get("event_log_format")
        if cur is None:
            return dict(DEFAULT_SYSTEM_SETTINGS.get("event_log_format", {}) or {})
        return dict(cur.value or {})

    def _write(d: Dict[str, str]) -> int:
        sysobj.set("event_log_format", d)
        return mgr.refresh_match_rules(sys_name)

    overrides = _read()
    if not fmt_args:
        lines = ["**Event log templates** (★ = override):"]
        for t in EVENT_LOG_DEFAULT_FORMATS:
            ov = overrides.get(t)
            star = "★ " if ov else "  "
            lines.append(f"{star}`{t}`: {ov if ov else EVENT_LOG_DEFAULT_FORMATS[t]}")
        return await ctx.send("\n".join(lines))
    etype = fmt_args[0]
    if len(fmt_args) == 1:
        # Reset this type to its built-in default.
        if etype in overrides:
            del overrides[etype]
            refreshed = _write(overrides)
            return await ctx.send(
                f"Reset `{etype}` to its built-in template "
                f"[refreshed {refreshed} live match"
                f"{'es' if refreshed != 1 else ''}]."
            )
        return await ctx.send(f"`{etype}` has no override (already default).")
    template = " ".join(fmt_args[1:])
    overrides[etype] = template
    refreshed = _write(overrides)
    return await ctx.send(
        f"Set the `{etype}` event template [refreshed {refreshed} live "
        f"match{'es' if refreshed != 1 else ''}]."
    )


@registry.command(
    "log", access="all",
    usage="!log [n] | !log clear | !log format [<type> [<template>]]",
    desc=(
        "Show the active match's event log (the combat log). `!log` or "
        "`!log <n>` shows the most recent n entries (default 20), rendered "
        "via each event type's template. `!log clear` empties it. `!log "
        "format` lists the per-type templates; `!log format <type> "
        "<template>` overrides one (placeholder syntax like "
        "entity_line_format); `!log format <type>` resets it to the "
        "built-in default. What gets recorded is governed by the "
        "event_log_enabled / event_log_events / event_log_retention rules."
    ),
)
async def log_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    m = active_match(mgr, ctx)
    if args and args[0].lower() == "clear":
        n = len(m.event_log)
        m.event_log.clear()
        return await ctx.send(
            f"Cleared {n} event log entr{'y' if n == 1 else 'ies'}."
        )
    if args and args[0].lower() == "format":
        return await _log_format_sub(ctx, args[1:], mgr, m)
    n = 20
    if args:
        try:
            n = int(args[0])
        except ValueError:
            return await ctx.send(
                f"Expected a number, 'clear', or 'format'; got '{args[0]}'."
            )
    if not m.event_log:
        return await ctx.send("Event log is empty.")
    entries = m.event_log[-n:] if n > 0 else list(m.event_log)
    header = f"**Event log** (showing {len(entries)} of {len(m.event_log)}):"
    lines = [header]
    for e in entries:
        lines.append(f"`R{e.get('round')}` " + _render_event(m, e))
    return await ctx.send("\n".join(lines))


registry.annotate_sub(
    "log", "clear",
    usage="!log clear",
    desc="Empty the active match's event log.",
)
registry.annotate_sub(
    "log", "format",
    usage="!log format [<type> [<template>]]",
    desc=(
        "List the per-type render templates, or override/reset one. "
        "`!log format` lists; `!log format <type> <template>` sets an "
        "override (persists on the game system); `!log format <type>` "
        "resets it to the built-in default."
    ),
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
        ack = f"tile ({x},{y}).{path} = {value!r}"
        # Advisory for a likely-bad render color (same spirit as entity
        # set_var): the renderer reads the top-level `color` field, which may
        # be a palette name OR a color formula — so only nudge on a value
        # that's neither a known name nor formula-shaped.
        if path == "color" and isinstance(value, str) and value:
            from logic import TEXT_COLORS
            if value not in TEXT_COLORS and not any(c in value for c in "()[]. +-*/"):
                ack += (f"\n⚠ `{value}` isn't a recognized color (renders "
                        f"uncolored unless it's a valid color formula). "
                        + _color_guide())
        return await ctx.send(ack)

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
        # A tile hidden from this channel's POV reads as "no data" — we
        # don't even confirm it exists, so a player can't probe for a
        # hidden trap. `!tile info` carries no `full` flag; a host views
        # from an omniscient channel (or `!as view omniscient`).
        pov = _view_pov(ctx, m, args)
        if (x, y) not in m.tiles or not m.tile_visible_to(x, y, pov):
            return await ctx.send(f"tile ({x},{y}): no data.")
        data = m.tiles[(x, y)]
        return await ctx.send(
            f"**tile ({x},{y})**\n```{json.dumps(data, indent=2, sort_keys=True)}\n```"
        )

    # ---- list ----
    if sub == "list":
        # POV-filtered: hidden tiles are omitted entirely (no enumeration
        # of hidden traps from a player channel).
        pov = _view_pov(ctx, m, args)
        coords = [(x, y) for (x, y) in sorted(m.tiles.keys())
                  if m.tile_visible_to(x, y, pov)]
        if not coords:
            return await ctx.send("No special tiles in this match.")
        # Compact one-line summary per tile: list top-level feature
        # keys so the GM sees at a glance what's where. Use !tile info
        # for the full nested dump.
        lines = ["**Special tiles:**"]
        for (x, y) in coords:
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
            formula_src = normalize_body_source(" ".join(args[5:]).strip())
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
            formula_src = normalize_body_source(" ".join(args[4:]).strip())
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
    "zone",
    usage=("!zone <new|drop|add|remove|fill|shift|anchor|unanchor|set|del|clear|glyph|color|info|list|cells> ..."),
    desc=(
        "Named multi-cell regions. A zone is a SET of cells plus a "
        "free-form data dict, optional hooks, and an optional map glyph "
        "— think 'the gas cloud', 'the throne room', 'the lava field'. "
        "Unlike a tile (one cell), a zone spans many cells and can be "
        "reshaped (`add`/`remove`/`fill`) or moved wholesale (`shift`, "
        "for a drifting cloud). Data is set/read with dotted paths "
        "(`set`/`del`/`clear`/`info`) exactly like tile data, and is "
        "readable from formulas via zone_get/zone_has plus membership "
        "queries (zones_at, in_zone, entities_in_zone, ...). Subcommands: "
        "new, drop, add, remove, fill, shift, set, del, clear, glyph, "
        "info, list, cells, hook."
    ),
)
async def zone_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["zone"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    # ---- new <name> ----
    if sub == "new":
        if await return_help_if_not_enough_args(ctx, args, 2, "zone", "new"):
            return
        name = args[1]
        try:
            m.create_zone(name)
        except (VTTError, DuplicateId) as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(f"Created empty zone `{name}`.")

    # ---- drop <name> ----  (delete the whole zone)
    if sub == "drop":
        if await return_help_if_not_enough_args(ctx, args, 2, "zone", "drop"):
            return
        name = args[1]
        z = m.zones.get(name)
        if z is None:
            return await ctx.send(f"❌ Zone '{name}' not found.")
        n = len(z.get("cells") or ())
        m.delete_zone(name)
        return await ctx.send(f"Dropped zone `{name}` ({n} cell(s)).")

    # ---- add <name> <x> <y> ----  (add one cell; creates the zone)
    if sub == "add":
        if await return_help_if_not_enough_args(ctx, args, 4, "zone", "add"):
            return
        name = args[1]
        x, y = _parse_xy(args, offset=2)
        try:
            added = m.zone_add_cell(name, x, y)
        except (VTTError, OutOfBounds) as ex:
            return await ctx.send(f"❌ {ex}")
        verb = "Added" if added else "Already in zone:"
        prep = "to" if added else "for"
        return await ctx.send(
            f"{verb} ({x},{y}) {prep} `{name}` ({m.zone_size(name)} cell(s))."
        )

    # ---- remove <name> <x> <y> ----  (drop one cell)
    if sub == "remove":
        if await return_help_if_not_enough_args(ctx, args, 4, "zone", "remove"):
            return
        name = args[1]
        x, y = _parse_xy(args, offset=2)
        try:
            removed = m.zone_remove_cell(name, x, y)
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        if not removed:
            return await ctx.send(f"({x},{y}) was not in zone `{name}`.")
        return await ctx.send(
            f"Removed ({x},{y}) from `{name}` ({m.zone_size(name)} cell(s))."
        )

    # ---- fill <name> <x1> <y1> <x2> <y2> ----  (rect, creates the zone)
    if sub == "fill":
        if await return_help_if_not_enough_args(ctx, args, 6, "zone", "fill"):
            return
        name = args[1]
        x1, y1 = _parse_xy(args, offset=2)
        x2, y2 = _parse_xy(args, offset=4)
        try:
            added = m.zone_fill_rect(name, x1, y1, x2, y2)
        except VTTError as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(
            f"Filled `{name}` over ({x1},{y1})-({x2},{y2}): +{added} cell(s), "
            f"{m.zone_size(name)} total."
        )

    # ---- shift <name> <dx> <dy> ----  (translate the footprint)
    if sub == "shift":
        if await return_help_if_not_enough_args(ctx, args, 4, "zone", "shift"):
            return
        name = args[1]
        try:
            dx = int(args[2]); dy = int(args[3])
        except (ValueError, IndexError):
            return await ctx.send("❌ expected integer dx and dy.")
        z = m.zones.get(name)
        if z is None:
            return await ctx.send(f"❌ Zone '{name}' not found.")
        before = len(z.get("cells") or ())
        kept = m.zone_shift(name, dx, dy)
        dropped = before - kept
        tail = f" ({dropped} pushed off-grid)" if dropped else ""
        return await ctx.send(
            f"Shifted `{name}` by ({dx},{dy}): {kept} cell(s){tail}."
        )

    # ---- anchor <name> <eid> [radius] [metric] ----  (entity-anchored aura)
    if sub == "anchor":
        if await return_help_if_not_enough_args(ctx, args, 3, "zone", "anchor"):
            return
        name = args[1]
        eid = args[2]
        radius = 0
        metric = "square_radius"
        if len(args) >= 4:
            try:
                radius = int(args[3])
            except ValueError:
                return await ctx.send("❌ radius must be a non-negative integer.")
        if len(args) >= 5:
            metric = args[4]
        try:
            n = m.anchor_zone(name, eid, radius, metric)
        except (NotFound, VTTError) as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(
            f"Anchored `{name}` to `{eid}` (radius {radius}, {metric}): "
            f"{n} cell(s). It now follows `{eid}`."
        )

    # ---- unanchor <name> ----  (detach aura; leave cells static)
    if sub == "unanchor":
        if await return_help_if_not_enough_args(ctx, args, 2, "zone", "unanchor"):
            return
        name = args[1]
        try:
            was = m.unanchor_zone(name)
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        if not was:
            return await ctx.send(f"Zone `{name}` was not anchored.")
        return await ctx.send(
            f"Detached `{name}`'s anchor; its {m.zone_size(name)} cell(s) "
            f"are now static."
        )

    # ---- set <name> <path> <value> ----  (data set, dotted path)
    if sub == "set":
        if await return_help_if_not_enough_args(ctx, args, 4, "zone", "set"):
            return
        name = args[1]
        path = args[2]
        value = _parse_scalar(args[3])
        try:
            m.zone_set_path(name, path, value)
        except VTTError as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(f"zone `{name}`.{path} = {value!r}")

    # ---- del <name> <path> ----  (data delete, dotted path)
    if sub == "del":
        if await return_help_if_not_enough_args(ctx, args, 3, "zone", "del"):
            return
        name = args[1]
        path = args[2]
        try:
            m.zone_del_path(name, path)
        except (NotFound, VTTError) as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(f"Removed `{path}` from zone `{name}`.")

    # ---- clear <name> ----  (wipe all data; keeps cells + hooks)
    if sub == "clear":
        if await return_help_if_not_enough_args(ctx, args, 2, "zone", "clear"):
            return
        name = args[1]
        try:
            n = m.zone_clear_data(name)
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(f"Cleared {n} data key(s) from zone `{name}`.")

    # ---- glyph <name> <char|-> ----  (map rendering)
    if sub == "glyph":
        if await return_help_if_not_enough_args(ctx, args, 3, "zone", "glyph"):
            return
        name = args[1]
        z = m.zones.get(name)
        if z is None:
            return await ctx.send(f"❌ Zone '{name}' not found.")
        g = args[2]
        if g in ("-", "none", "clear"):
            z.pop("glyph", None)
            return await ctx.send(f"Cleared map glyph for zone `{name}`.")
        if len(g) != 1:
            return await ctx.send(
                "❌ glyph must be exactly one character (or `-` to clear)."
            )
        z["glyph"] = g
        return await ctx.send(f"Zone `{name}` map glyph set to `{g}`.")

    if sub == "color":
        if await return_help_if_not_enough_args(ctx, args, 3, "zone", "color"):
            return
        name = args[1]
        z = m.zones.get(name)
        if z is None:
            return await ctx.send(f"❌ Zone '{name}' not found.")
        val = args[2]
        if val in ("-", "none", "clear"):
            z.pop("color", None)
            return await ctx.send(f"Cleared color for zone `{name}`.")
        from logic import TEXT_COLORS
        # A bare palette name is a literal; anything else is a color formula
        # (resolved per render). Warn on a non-palette, non-formula-looking
        # value the same way entity colors do, but still store it (it may be
        # a formula). A clearly-bad literal (no formula chars) gets a nudge.
        if val not in TEXT_COLORS and not any(c in val for c in "()[]. +-*/"):
            z["color"] = val
            return await ctx.send(
                f"Zone `{name}` color set to `{val}`.\n⚠ `{val}` isn't a "
                f"recognized color name (renders uncolored unless it's a "
                f"valid color formula). {_color_guide()}"
            )
        z["color"] = val
        kind = "color" if val in TEXT_COLORS else "color formula"
        return await ctx.send(f"Zone `{name}` {kind} set to `{val}`.")

    # ---- cells <name> ----  (list the footprint)
    if sub == "cells":
        if await return_help_if_not_enough_args(ctx, args, 2, "zone", "cells"):
            return
        name = args[1]
        # A zone hidden from this POV reads as absent (don't reveal a
        # secret region exists to a player channel).
        if not m.zone_visible_to(name, _view_pov(ctx, m, args)):
            return await ctx.send(f"zone `{name}`: not found.")
        try:
            cells = m.zone_cell_list(name)
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        if not cells:
            return await ctx.send(f"zone `{name}`: no cells.")
        rendered = ", ".join(f"({x},{y})" for x, y in cells)
        return await ctx.send(
            f"**zone `{name}`** ({len(cells)} cell(s)): {rendered}"
        )

    # ---- info <name> ----
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "zone", "info"):
            return
        name = args[1]
        z = m.zones.get(name)
        if z is None or not m.zone_visible_to(name, _view_pov(ctx, m, args)):
            return await ctx.send(f"zone `{name}`: not found.")
        cells = m.zone_cell_list(name)
        view = {
            "cells": cells,
            "data": z.get("data") or {},
            "hooks": sorted((z.get("hooks") or {}).keys()),
        }
        if isinstance(z.get("glyph"), str):
            view["glyph"] = z["glyph"]
        if z.get("anchor"):
            view["anchor"] = {
                "entity": z["anchor"],
                "radius": z.get("anchor_radius", 0),
                "metric": z.get("anchor_metric", "square_radius"),
            }
        return await ctx.send(
            f"**zone `{name}`** ({len(cells)} cell(s))\n"
            f"```{json.dumps(view, indent=2, sort_keys=True)}\n```"
        )

    # ---- list ----
    if sub == "list":
        pov = _view_pov(ctx, m, args)
        names = [n for n in sorted(m.zones.keys()) if m.zone_visible_to(n, pov)]
        if not names:
            return await ctx.send("No zones in this match.")
        lines = ["**Zones:**"]
        for name in names:
            z = m.zones[name]
            n = len(z.get("cells") or ())
            hooks = sorted((z.get("hooks") or {}).keys())
            extras = []
            if z.get("data"):
                extras.append(f"data: {', '.join(sorted(z['data'].keys()))}")
            if hooks:
                extras.append(f"hooks: {', '.join(hooks)}")
            if isinstance(z.get("glyph"), str):
                extras.append(f"glyph '{z['glyph']}'")
            if z.get("anchor"):
                extras.append(
                    f"aura→`{z['anchor']}` r{z.get('anchor_radius', 0)}")
            tail = (" — " + "; ".join(extras)) if extras else ""
            lines.append(f"- `{name}`: {n} cell(s){tail}")
        return await ctx.send("\n".join(lines))

    # ---- hook add|del|list ----
    # Zone hooks are formulas under zones[name]["hooks"][when]. The two
    # movement families (boundary on_enter/exit/stop, per-cell
    # on_cell_enter/exit/stop) fire as entities move; the time hooks
    # (on_round/turn_*) fire at the lifecycle moments. Inside the hook
    # self = the moving/acting entity, zone_name = the zone, tile_x/tile_y
    # = the crossed/stepped cell (None for time hooks).
    if sub == "hook":
        from logic import ZONE_HOOK_NAMES
        from formula import validate_formula, FormulaError
        if len(args) < 2:
            title, body = registry.help_for(["zone", "hook"])
            return await ctx.send(f"**{title}**\n{body}")
        hsub = args[1].lower()

        if hsub == "add":
            # !zone hook add <name> <when> <formula>
            if await return_help_if_not_enough_args(ctx, args, 5, "zone", "hook"):
                return
            name = args[2]
            when = args[3]
            formula_src = normalize_body_source(" ".join(args[4:]).strip())
            if when not in ZONE_HOOK_NAMES:
                allowed = ", ".join(sorted(ZONE_HOOK_NAMES))
                return await ctx.send(
                    f"❌ Unknown zone hook '{when}'. Allowed: {allowed}."
                )
            if not formula_src:
                return await ctx.send("❌ hook formula cannot be empty.")
            try:
                validate_formula(
                    formula_src, mode="exec",
                    known_funcs=frozenset(m.formula_functions.keys()),
                )
            except FormulaError as ex:
                return await ctx.send(f"❌ Invalid hook formula: {ex}")
            z = m.zones.get(name)
            if z is None:
                z = m.create_zone(name)
            z["hooks"][when] = formula_src
            return await ctx.send(f"Set zone `{name}` `{when}` hook.")

        if hsub == "del":
            # !zone hook del <name> <when>
            if await return_help_if_not_enough_args(ctx, args, 4, "zone", "hook"):
                return
            name = args[2]
            when = args[3]
            z = m.zones.get(name)
            if z is None:
                return await ctx.send(f"❌ Zone '{name}' not found.")
            if when not in (z.get("hooks") or {}):
                return await ctx.send(f"zone `{name}` has no `{when}` hook.")
            del z["hooks"][when]
            return await ctx.send(f"Removed zone `{name}` `{when}` hook.")

        if hsub == "list":
            # !zone hook list <name>
            if await return_help_if_not_enough_args(ctx, args, 3, "zone", "hook"):
                return
            name = args[2]
            z = m.zones.get(name)
            if z is None:
                return await ctx.send(f"❌ Zone '{name}' not found.")
            hooks = z.get("hooks") or {}
            if not hooks:
                return await ctx.send(f"zone `{name}`: no hooks.")
            lines = [f"**zone `{name}` hooks:**"]
            for when in sorted(hooks.keys()):
                src = hooks[when]
                snippet = src if len(src) <= 80 else src[:77] + "..."
                lines.append(f"- `{when}`: {snippet}")
            return await ctx.send("\n".join(lines))

        title, body = registry.help_for(["zone", "hook"])
        return await ctx.send(f"**{title}**\n{body}")

    title, body = registry.help_for(["zone"])
    return await ctx.send(f"**{title}**\n{body}")


@registry.command(
    "status",
    usage=("!status <def|drop|tick|when|stack|maxlevel|data|list|info|apply> ..."),
    desc=(
        "Status DEFINITIONS — self-describing statuses. Define a status "
        "once (its per-tick effect, when it ticks, how it stacks, its max "
        "level, default data); applying it to entities instantiates from "
        "the definition. A status instance on an entity resolves its "
        "behavior from the definition of the SAME name. `tick` is a formula "
        "run at the definition's `when` (default turn_end) with self=the "
        "bearer and status_name bound — it's where the DoT/regen effect AND "
        "any duration decrement / self-removal live (auto-decay is "
        "deliberately not built in). Apply with `!status apply <eid> <name> "
        "[level] [duration]` (or the status_apply formula primitive), which "
        "honors the definition's stack mode (else the status_default_stack "
        "rule). Raw per-entity instance editing stays on `!ent status`. "
        "Subcommands: def, drop, tick, when, stack, maxlevel, data, list, "
        "info, apply."
    ),
)
async def status_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["status"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()
    _TICK_WHENS = ("turn_end", "turn_start", "round_start", "round_end", "never")
    _STACK_MODES = ("refresh", "add_level", "extend", "replace", "none")

    if sub == "def":
        if await return_help_if_not_enough_args(ctx, args, 2, "status", "def"):
            return
        name = args[1]
        try:
            m.define_status(name)
        except (VTTError, DuplicateId) as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(
            f"Defined status `{name}` (tick_when=turn_end, no tick yet). "
            f"Set its effect with `!status tick {name} \"<formula>\"`."
        )

    if sub == "drop":
        if await return_help_if_not_enough_args(ctx, args, 2, "status", "drop"):
            return
        try:
            m.remove_status_def(args[1])
        except NotFound as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(
            f"Removed status definition `{args[1]}`. Existing instances "
            f"keep their data but lose tick behavior."
        )

    if sub == "tick":
        if await return_help_if_not_enough_args(ctx, args, 3, "status", "tick"):
            return
        name = args[1]
        if name not in m.status_definitions:
            return await ctx.send(f"❌ No status definition `{name}`. Use `!status def {name}` first.")
        body = normalize_body_source(" ".join(args[2:]).strip())
        if body:
            from formula import validate_formula
            try:
                validate_formula(
                    body, mode="exec",
                    known_funcs=frozenset(m.formula_functions.keys()),
                )
            except FormulaError as ex:
                return await ctx.send(f"❌ Invalid tick formula: {ex}")
        m.status_definitions[name]["tick"] = body
        return await ctx.send(f"Set `{name}` tick formula.")

    if sub == "when":
        if await return_help_if_not_enough_args(ctx, args, 3, "status", "when"):
            return
        name = args[1]
        when = args[2].lower()
        if name not in m.status_definitions:
            return await ctx.send(f"❌ No status definition `{name}`.")
        if when not in _TICK_WHENS:
            return await ctx.send(f"❌ when must be one of: {', '.join(_TICK_WHENS)}.")
        m.status_definitions[name]["tick_when"] = when
        return await ctx.send(f"`{name}` ticks at {when}.")

    if sub == "stack":
        if await return_help_if_not_enough_args(ctx, args, 3, "status", "stack"):
            return
        name = args[1]
        mode = args[2].lower()
        if name not in m.status_definitions:
            return await ctx.send(f"❌ No status definition `{name}`.")
        if mode not in _STACK_MODES:
            return await ctx.send(f"❌ stack mode must be one of: {', '.join(_STACK_MODES)}.")
        m.status_definitions[name]["stack"] = mode
        return await ctx.send(f"`{name}` stack mode = {mode}.")

    if sub == "maxlevel":
        if await return_help_if_not_enough_args(ctx, args, 3, "status", "maxlevel"):
            return
        name = args[1]
        if name not in m.status_definitions:
            return await ctx.send(f"❌ No status definition `{name}`.")
        try:
            n = int(args[2])
        except ValueError:
            return await ctx.send("❌ maxlevel must be an integer (0 = uncapped).")
        m.status_definitions[name]["max_level"] = max(0, n)
        return await ctx.send(f"`{name}` max_level = {max(0, n)}.")

    if sub == "data":
        if await return_help_if_not_enough_args(ctx, args, 4, "status", "data"):
            return
        name = args[1]
        if name not in m.status_definitions:
            return await ctx.send(f"❌ No status definition `{name}`.")
        path = args[2]
        value = _parse_scalar(args[3])
        d = m.status_definitions[name].setdefault("data", {})
        try:
            _set_path(d, path, value)
        except (VTTError, KeyError, TypeError) as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(f"`{name}` default data.{path} = {value!r}")

    if sub == "list":
        if not m.status_definitions:
            return await ctx.send("No status definitions in this match.")
        lines = ["**Status definitions:**"]
        for name in sorted(m.status_definitions.keys()):
            d = m.status_definitions[name]
            bits = [f"when={d.get('tick_when', 'turn_end')}"]
            if d.get("stack"):
                bits.append(f"stack={d['stack']}")
            if d.get("max_level"):
                bits.append(f"max_lvl={d['max_level']}")
            if not (isinstance(d.get("tick"), str) and d["tick"].strip()):
                bits.append("no tick")
            lines.append(f"- `{name}` ({', '.join(bits)})")
        return await ctx.send("\n".join(lines))

    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "status", "info"):
            return
        name = args[1]
        d = m.status_definitions.get(name)
        if d is None:
            return await ctx.send(f"No status definition `{name}`.")
        return await ctx.send(
            f"**status def `{name}`**\n```{json.dumps(d, indent=2, sort_keys=True)}\n```"
        )

    if sub == "apply":
        if await return_help_if_not_enough_args(ctx, args, 3, "status", "apply"):
            return
        eid = _resolve_eid(m, args[1])
        name = args[2]
        level = duration = None
        if len(args) >= 4:
            try:
                level = int(args[3])
            except ValueError:
                return await ctx.send("❌ level must be an integer.")
        if len(args) >= 5:
            try:
                duration = int(args[4])
            except ValueError:
                return await ctx.send("❌ duration must be an integer.")
        try:
            event_log = m.apply_status(eid, name, level, duration)
        except (VTTError, NotFound) as ex:
            return await ctx.send(f"❌ {ex}")
        inst = m.entities[eid].status.get(name, {})
        tail = ("\n" + "\n".join(event_log)) if event_log else ""
        return await ctx.send(
            f"Applied `{name}` to `{eid}` "
            f"(level={inst.get('level', '?')}, "
            f"duration={inst.get('duration', '∞')}).{tail}"
        )

    title, body = registry.help_for(["status"])
    return await ctx.send(f"**{title}**\n{body}")


@registry.command(
    "part",
    usage="!part <add|attach|detach|remove|list|info> ...",
    desc=(
        "Body parts for locational damage. A part is a REAL entity attached "
        "to a parent (it gets hp/vars/statuses/passives/death for free) but "
        "rides on the parent's cell and is hidden from the map roster. "
        "Damage routes via the damage_part formula primitive (HD2 'damage to "
        "main' model — see the part_to_main_* rules + per-part to_main_percent "
        "/ to_main_cap / vital vars); WHERE a hit lands via hit_location. "
        "Config lives in the part's vars (set with `!ent set_var <part> ...`); "
        "destroy effects are passives on the part (`!ent passive add <part> "
        "on_death ...`). A 0/0 part is an indestructible passthrough zone "
        "(head/chest). Subcommands: add <parent> <part_id> <name> <hp> <maxhp> "
        "[k=v ...]; attach <parent> <part_id>; detach <part_id>; remove "
        "<part_id>; list <parent>; info <part_id>."
    ),
)
async def part_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["part"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()

    if sub == "add":
        if await return_help_if_not_enough_args(ctx, args, 6, "part", "add"):
            return
        parent = _resolve_eid(m, args[1])
        part_id, name = args[2], args[3]
        try:
            hp, maxhp = int(args[4]), int(args[5])
        except ValueError:
            return await ctx.send("❌ hp and maxhp must be integers.")
        try:
            e, log = m.create_part(parent, part_id, name, hp, maxhp)
        except (NotFound, DuplicateId, VTTError) as ex:
            return await ctx.send(f"❌ {ex}")
        applied: List[str] = []
        for tok in args[6:]:
            if "=" not in tok:
                return await ctx.send(
                    f"❌ extra var `{tok}` must be in key=value form.")
            k, _, v = tok.partition("=")
            if not k:
                return await ctx.send(f"❌ extra var `{tok}` has an empty key.")
            e.write_var(k, _parse_scalar(v))
            applied.append(k)
        extra = f" Set: {', '.join(applied)}." if applied else ""
        tail = ("\n" + "\n".join(log)) if log else ""
        return await ctx.send(
            f"Attached body part `{part_id}` ({name}) to `{parent}` "
            f"({hp}/{maxhp} hp).{extra}{tail}"
        )

    if sub == "attach":
        if await return_help_if_not_enough_args(ctx, args, 3, "part", "attach"):
            return
        parent = _resolve_eid(m, args[1])
        part_id = _resolve_eid(m, args[2])
        try:
            m.attach_part(parent, part_id)
        except (NotFound, VTTError) as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(f"`{part_id}` is now a body part of `{parent}`.")

    if sub == "detach":
        if await return_help_if_not_enough_args(ctx, args, 2, "part", "detach"):
            return
        part_id = _resolve_eid(m, args[1])
        try:
            p = m.detach_part(part_id)
        except (NotFound, VTTError) as ex:
            return await ctx.send(f"❌ {ex}")
        return await ctx.send(
            f"Detached `{part_id}` — it's now a free entity at "
            f"({p.x},{p.y})."
        )

    if sub in ("remove", "del", "rm"):
        if await return_help_if_not_enough_args(ctx, args, 2, "part", "remove"):
            return
        part_id = _resolve_eid(m, args[1])
        if part_id not in m.entities:
            return await ctx.send(f"❌ Entity `{part_id}` not found.")
        m.entities[part_id].remove()
        return await ctx.send(f"Removed body part `{part_id}`.")

    if sub == "list":
        if await return_help_if_not_enough_args(ctx, args, 2, "part", "list"):
            return
        parent = _resolve_eid(m, args[1])
        if parent not in m.entities:
            return await ctx.send(f"❌ Entity `{parent}` not found.")
        parts = m.entity_parts(parent)
        if not parts:
            return await ctx.send(f"`{parent}` has no body parts.")
        name_var = m.part_name_var()
        lines = [f"Body parts of `{parent}`:"]
        for p in parts:
            role = p.vars.get(name_var, "?")
            lines.append(f"  `{p.id}` ({role}): {_part_status_str(m, p)}")
        return await ctx.send("\n".join(lines))

    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 2, "part", "info"):
            return
        part_id = _resolve_eid(m, args[1])
        if part_id not in m.entities:
            return await ctx.send(f"❌ Entity `{part_id}` not found.")
        p = m.entities[part_id]
        if not p.part_of:
            return await ctx.send(f"`{part_id}` is not a body part.")
        return await ctx.send(_entity_dump(p))

    title, body = registry.help_for(["part"])
    return await ctx.send(f"**{title}**\n{body}")


@registry.command(
    "mod",
    access="all",
    usage="!mod show <eid> <stat> [base] [tag ...]",
    desc=(
        "Inspect a derived/effective stat — the active modifiers and, given "
        "a numeric base, the folded result. Modifiers are NOT edited here: "
        "they live in their sources (a status's `modifiers`, an equipped "
        "item's `modifiers`, the entity's direct `modifiers` slot) and are "
        "set with `!ent set_var` / status data. The engine combines them via "
        "the apply_mods / list_mods formula primitives; this is the readout. "
        "Context-dependent modifiers (condition reads target/attacker/etc.) "
        "only show when their condition resolves without that context."
    ),
)
async def mod_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if not args:
        title, body = registry.help_for(["mod"])
        return await ctx.send(f"**{title}**\n{body}")
    m = active_match(mgr, ctx)
    sub = args[0].lower()
    if sub == "show":
        if await return_help_if_not_enough_args(ctx, args, 3, "mod", "show"):
            return
        eid = _resolve_eid(m, args[1])
        if eid not in m.entities:
            return await ctx.send(f"❌ Entity `{eid}` not found.")
        stat = args[2]
        rest = args[3:]
        base = None
        tags: List[str] = []
        for tok in rest:
            if base is None:
                try:
                    base = float(tok)
                    continue
                except ValueError:
                    pass
            tags.append(tok)
        mods = m.gather_modifiers(eid, stat, tags, {})
        tagstr = f" [{', '.join(tags)}]" if tags else ""
        lines = [f"Modifiers on `{eid}` for `{stat}`{tagstr}:"]
        if not mods:
            lines.append("  (none active)")
        for md in mods:
            tg = (" tags=" + ",".join(md["tags"])) if md["tags"] else ""
            ntg = (" not=" + ",".join(md["not_tags"])) if md["not_tags"] else ""
            src = f" [{md['source']}]" if md.get("source") else ""
            lines.append(
                f"  {md['op']} {md['value']:g} (pri {md['priority']:g}){tg}{ntg}{src}")
        if base is not None:
            result = m.apply_modifiers(eid, stat, base, tags, {})
            lines.append(f"  → base {base:g} becomes {result:g}")
        return await ctx.send("\n".join(lines))

    title, body = registry.help_for(["mod"])
    return await ctx.send(f"**{title}**\n{body}")


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
        # Normalize `\n`/`\t` so a multi-line body typed at the CLI
        # compiles. See formula.normalize_body_source.
        body = normalize_body_source(" ".join(args[3:]).strip() if len(args) > 3 else "")
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
    "zone", "new",
    usage="!zone new <name>",
    desc="Create an empty zone (no cells). Errors if the name is taken.",
)
registry.annotate_sub(
    "zone", "drop",
    usage="!zone drop <name>",
    desc="Delete a zone entirely (cells, data, hooks, glyph).",
)
registry.annotate_sub(
    "zone", "add",
    usage="!zone add <name> <x> <y>",
    desc=(
        "Add a single cell to a zone, creating the zone if it doesn't "
        "exist yet. Off-grid coordinates are rejected. No-op (reported) "
        "if the cell is already in the zone."
    ),
)
registry.annotate_sub(
    "zone", "remove",
    usage="!zone remove <name> <x> <y>",
    desc=(
        "Remove a single cell from a zone. The zone keeps existing even "
        "if this empties it (its data/hooks survive, so a later `shift` "
        "or `add` can repopulate it)."
    ),
)
registry.annotate_sub(
    "zone", "fill",
    usage="!zone fill <name> <x1> <y1> <x2> <y2>",
    desc=(
        "Add every in-bounds cell of the rectangle spanned by the two "
        "corners (in any order) to a zone, creating it if needed. The "
        "fast way to paint a rectangular region; combine with `remove` "
        "to carve out holes."
    ),
)
registry.annotate_sub(
    "zone", "shift",
    usage="!zone shift <name> <dx> <dy>",
    desc=(
        "Translate the entire zone footprint by (dx, dy). Cells pushed "
        "off the grid are dropped (the zone can drift off an edge). The "
        "drifting-gas-cloud primitive — pair with a turn/round hook to "
        "move a hazard each round."
    ),
)
registry.annotate_sub(
    "zone", "set",
    usage="!zone set <name> <path> <value>",
    desc=(
        "Set a dotted-path key in the zone's free-form data dict "
        "(creating the zone + intermediate dicts as needed). Same "
        "semantics as `!tile set`. Read it from formulas with "
        "zone_get(name, path)."
    ),
)
registry.annotate_sub(
    "zone", "del",
    usage="!zone del <name> <path>",
    desc=(
        "Delete a dotted-path key from the zone's data, pruning any "
        "parent dicts left empty. Errors if the path is absent."
    ),
)
registry.annotate_sub(
    "zone", "clear",
    usage="!zone clear <name>",
    desc=(
        "Wipe ALL data from a zone (keeps its cells, hooks, and glyph). "
        "Reports how many top-level data keys were removed."
    ),
)
registry.annotate_sub(
    "zone", "glyph",
    usage="!zone glyph <name> <char|->",
    desc=(
        "Set the zone's single-character map glyph (shown in !map on "
        "every zone cell that has no tile glyph or entity on top), or "
        "pass `-` / `none` to clear it. Zone glyphs are the lowest "
        "render layer."
    ),
)
registry.annotate_sub(
    "zone", "color",
    usage="!zone color <name> <color|formula|->",
    desc=(
        "Set the zone's render color — a palette name (see `!map colors`) "
        "OR a color formula resolved per render (bindings: zone_name; e.g. "
        "`zone_get(zone_name, 'tint')`). Colors the zone glyph, and tints "
        "empty (glyph-less) zone cells. `-` / `none` clears it. Applies on "
        "color-capable surfaces; a tile or entity on top owns its cell."
    ),
)
registry.annotate_sub(
    "zone", "cells",
    usage="!zone cells <name>",
    desc="List every cell in the zone as (x,y) pairs.",
)
registry.annotate_sub(
    "zone", "info",
    usage="!zone info <name>",
    desc=(
        "Show a zone's full state: its cells, data dict, registered hook "
        "names, and glyph (if any)."
    ),
)
registry.annotate_sub(
    "zone", "list",
    usage="!zone list",
    desc=(
        "List every zone on the active match with cell counts and a "
        "summary of its data keys, hooks, and glyph."
    ),
)
registry.annotate_sub(
    "zone", "hook",
    usage="!zone hook <add|del|list> <name> [<when>] [<formula>]",
    desc=(
        "Manage a zone's hook formulas. `add <name> <when> <formula>` "
        "stores (or overwrites) a hook, creating the zone if needed; "
        "`del <name> <when>` removes one; `list <name>` shows them. "
        "Movement hooks come in two families: BOUNDARY "
        "(on_enter / on_exit / on_stop) fire ONCE when an entity crosses "
        "the zone's edge — entering from outside, leaving to outside, or "
        "stopping inside; moving cell-to-cell within the zone fires none "
        "of them. PER-CELL (on_cell_enter / on_cell_exit / on_cell_stop) "
        "fire for EVERY zone cell stepped into / out of / stopped on, "
        "even while moving within the zone — the 'gas damages you each "
        "cell you cross' case. TIME hooks (on_round_start / on_round_end "
        "/ on_turn_start / on_turn_end) fire once per zone at the "
        "lifecycle moment. Inside the body: self = the moving/acting "
        "entity (or current-turn entity for time hooks), zone_name = the "
        "firing zone, tile_x/tile_y = the crossed/stepped cell (None for "
        "time hooks). Example: "
        "`!zone hook add gas on_cell_enter \"entity[self].hp = "
        "entity[self].hp - zone_get(zone_name, 'dmg')\"`."
    ),
)
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
    # Normalize `\n`/`\t` so a multi-statement program typed at the CLI
    # (where the line is one physical string) parses — the harness
    # pre-translates these, the raw CLI does not. Idempotent on text that
    # has no literal escape sequence (incl. an already-stored passive
    # formula from the --as-passive branch). See normalize_body_source.
    src = normalize_body_source(src)
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


# ---- !action ---------------------------------------------------------
# Top-level invocation surface for the action system. The runner /
# proxies / discovery live in action.py; this handler is the user-
# facing parser + dispatcher: it resolves the action by name on the
# specified actor entity, parses the target tokens according to the
# action's declared target_type, parses key=value args, then hands
# off to action.run_action which captures pre-state, runs the body,
# and rolls back on failure.

@registry.command(
    "action",
    usage=(
        "!action <eid> <name> [target...] [k=v ...] | "
        "!action list <eid> | !action info <eid> <name>"
    ),
    desc=(
        "Invoke an action discovered anywhere in `<eid>`'s vars tree, "
        "or introspect with `list` / `info`. Actions are dicts at any "
        "path ending in `actions.<name>` with a `body` (a formula "
        "program), optional `description`, and optional `target` "
        "(entity/location/entity_list/location_list/none). The leading "
        "tokens after the action name are the target (shape depends "
        "on the action's declared type); any `key=value` tokens after "
        "that go into the `args` dict the body can read. On a clean "
        "`fail(reason, msg)` the runner rolls back and replies "
        "`❌ action <name>: [<reason>] <msg>`; on a successful run, "
        "`on_action_used` + per-target `on_action_used_on_target` "
        "fire. `<eid>` accepts `self`/`this`/`current`. When two "
        "actions share a name the handler shows a numbered "
        "disambiguation menu — re-issue with the full path."
    ),
)
async def action_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    if await return_help_if_not_enough_args(ctx, args, 2, "action"):
        return
    m = active_match(mgr, ctx)
    # `list` / `info` are introspection subcommands. Recognized by
    # the LEADING token, which makes them mutually exclusive with
    # the bare-invocation form (`!action <eid> <name> ...`) — no
    # entity can be named `list` because of entity-id sanity checks
    # at !ent add time, and even if one were, the user types the
    # subcommand explicitly. We branch BEFORE the entity-existence
    # check so `!action list` (no eid) shows a usage error rather
    # than an "entity 'list' not found".
    sub = args[0].lower()
    if sub == "list":
        if await return_help_if_not_enough_args(ctx, args, 2, "action", "list"):
            return
        actor_id = _resolve_eid(m, args[1])
        if actor_id not in m.entities:
            raise NotFound(f"Entity '{actor_id}' not found.")
        return await _action_list(ctx, m, actor_id)
    if sub == "info":
        if await return_help_if_not_enough_args(ctx, args, 3, "action", "info"):
            return
        actor_id = _resolve_eid(m, args[1])
        if actor_id not in m.entities:
            raise NotFound(f"Entity '{actor_id}' not found.")
        return await _action_info(ctx, m, actor_id, args[2])

    actor_id = _resolve_eid(m, args[0])
    if actor_id not in m.entities:
        raise NotFound(f"Entity '{actor_id}' not found.")
    requested = args[1]
    tail = list(args[2:])
    await _run_action_dispatch(
        ctx, mgr, m, actor_id=actor_id,
        requested_name=requested, tail_tokens=tail,
    )


async def _action_list(ctx: ReplyContext, m: Match, actor_id: str) -> None:
    """!action list <eid>: enumerate every discovered action on the
    entity. One line per action, showing name, target type, container
    path (where the action lives in vars — useful when the same name
    appears in multiple containers), and the GM's description if any.
    Empty entries hint at the available subcommands so a confused
    user sees a way forward."""
    from action import discover_actions
    actor = m.entities[actor_id]
    actions = discover_actions(actor, m.rules)
    if not actions:
        return await ctx.send(
            f"No actions discoverable on `{actor_id}`. Add one by "
            f"setting `<container>.actions.<name>.body` (and "
            f"optionally `.target`, `.description`) in the entity's "
            f"vars. See `!help action`."
        )
    lines = [f"**Actions on `{actor_id}`** "
             f"({sum(len(v) for v in actions.values())} total):"]
    for name in sorted(actions.keys()):
        for act in actions[name]:
            container = act.container_path or "(entity root)"
            desc = f" — {act.description}" if act.description else ""
            lines.append(
                f"- `{act.name}` (target: `{act.target_type}`, at "
                f"`{container}`){desc}"
            )
    await ctx.send("\n".join(lines))


async def _action_info(
    ctx: ReplyContext, m: Match, actor_id: str, requested: str,
) -> None:
    """!action info <eid> <name>: full detail for one action,
    including the body source. Accepts either a bare name (with
    disambiguation menu on collision) or a full vars path."""
    from action import discover_actions, lookup_action
    actor = m.entities[actor_id]
    actions = discover_actions(actor, m.rules)
    # Full-path first (matches the !action invocation precedence).
    matches: List[Any] = []
    for act_list in actions.values():
        for act in act_list:
            if act.full_path == requested:
                matches = [act]
                break
        if matches:
            break
    if not matches:
        matches = lookup_action(actions, requested, m.rules)
    if not matches:
        avail = sorted(actions.keys())
        hint = (" Available: " + ", ".join(f"`{n}`" for n in avail)) if avail else ""
        return await ctx.send(
            f"❌ no action `{requested}` on `{actor_id}`.{hint}"
        )
    if len(matches) > 1:
        lines = [
            f"There are {len(matches)} actions named `{requested}` on "
            f"`{actor_id}`. Use the full path with `!action info`:"
        ]
        for i, act in enumerate(matches, start=1):
            lines.append(f"  {i}. `{act.full_path}`")
        return await ctx.send("\n".join(lines))
    act = matches[0]
    container = act.container_path or "(entity root)"
    desc = act.description or "(no description)"
    lines = [
        f"**`{act.name}`** on `{actor_id}`",
        f"Path:        `{act.full_path}`",
        f"Container:   `{container}`",
        f"Target type: `{act.target_type}`",
        f"Description: {desc}",
        "Body:",
        "```",
        act.body,
        "```",
    ]
    await ctx.send("\n".join(lines))


async def _run_action_dispatch(
    ctx: ReplyContext, mgr: MatchManager, m: Match, *,
    actor_id: str, requested_name: str, tail_tokens: List[str],
) -> None:
    """Shared resolution path for both !action and !ent <id> action.
    Owns: discovery, disambiguation menu, target parsing, args
    parsing, runner invocation, and the reply for the !command
    (success ✓ vs ❌ <msg>)."""
    from action import (
        discover_actions, lookup_action, parse_target, parse_args_tokens,
        run_action,
    )
    actor = m.entities[actor_id]
    actions = discover_actions(actor, m.rules)
    # First: full-path match. The disambiguation menu directs users
    # to type the full path (e.g. `inventory.sword.actions.slice`),
    # so we resolve that BEFORE falling through to the bare-name
    # lookup — otherwise the full path would still hit the multi-
    # match menu (or miss entirely when the bare name is shared).
    action = None
    for act_list in actions.values():
        for act in act_list:
            if act.full_path == requested_name:
                action = act
                break
        if action is not None:
            break
    if action is None:
        # Bare-name lookup. Apply the case-sensitivity rule via
        # lookup_action so a `slice` named action matches the user's
        # request consistently with discover_actions's casing.
        matches = lookup_action(actions, requested_name, m.rules)
        if not matches:
            avail = sorted(actions.keys())
            if avail:
                hint = " Available: " + ", ".join(f"`{n}`" for n in avail) + "."
            else:
                hint = ""
            return await ctx.send(
                f"❌ no action `{requested_name}` on `{actor_id}`.{hint}"
            )
        if len(matches) > 1:
            # Disambiguation menu: list all locations, the user re-
            # issues with the full path.
            lines = [
                f"There are {len(matches)} actions named "
                f"`{requested_name}` on `{actor_id}`. Pick one by its "
                f"full path:"
            ]
            for i, act in enumerate(matches, start=1):
                desc = f" — {act.description}" if act.description else ""
                lines.append(
                    f"  {i}. `{act.full_path}`{desc}"
                )
            lines.append(
                f"Re-issue as: "
                f"`!action {actor_id} <full_path>`"
            )
            return await ctx.send("\n".join(lines))
        action = matches[0]
    # Pull out any pre-supplied mid-body choice answers FIRST. `answer=`
    # tokens feed choose()/choose_number() in order (a repeatable token,
    # so they bypass the last-write-wins args dict); the rest go through
    # normal target + args parsing. The reserved 'cancel' value aborts at
    # that prompt.
    answers: List[Any] = []
    pruned: List[str] = []
    for tok in tail_tokens:
        if tok.startswith("answer="):
            answers.append(tok.split("=", 1)[1])
        else:
            pruned.append(tok)
    # Parse the target tokens off the front, then args off the rest.
    try:
        target_value, remaining = parse_target(
            action.target_type, pruned, m,
        )
        args_dict = parse_args_tokens(remaining)
    except VTTError as ex:
        return await ctx.send(f"❌ action `{action.name}`: {ex}")
    # The use_action formula primitive needs ctx/mgr to find a
    # dispatcher; stash them on the match as transient runtime
    # attributes for the duration of this dispatch chain. They're
    # cleared after the runner returns to avoid leaking dispatcher
    # state into unrelated formula evaluations.
    prev_ctx = getattr(m, "_runtime_ctx", None)
    prev_mgr = getattr(m, "_runtime_mgr", None)
    m._runtime_ctx = ctx
    m._runtime_mgr = mgr
    from action import ActionEngineFault
    try:
        ok, fail_info = await run_action(
            action, actor_id=actor_id, target=target_value,
            args=args_dict, match=m, mgr=mgr, ctx=ctx,
            answers=answers,
        )
    except ActionEngineFault as ef:
        # Engine-level refusal (recursion limit etc.) — runner has
        # already rolled back and unwound the chain. Surface as a
        # clean ❌ so the user doesn't see the dispatcher's generic
        # `💥 Unexpected error` path.
        return await ctx.send(f"❌ action `{action.name}`: {ef.message}")
    finally:
        m._runtime_ctx = prev_ctx
        m._runtime_mgr = prev_mgr
    if not ok:
        # fail_info is (reason, message). The reason prefix is
        # included only when the GM supplied one (the single-arg
        # fail("msg") form leaves it empty).
        reason, message = fail_info
        tag = f"[{reason}] " if reason else ""
        return await ctx.send(
            f"❌ action `{action.name}`: {tag}{message}"
        )
    return await ctx.send(
        f"`{actor_id}` used action `{action.name}`."
    )


# ---- Automated Help command (shows available commands----------------------------------------------------------
@registry.command("help", access="all", usage="!help [command [sub]]", desc="Show command usage. Try `!help ent` or `!help ent move`.")
async def help_cmd(ctx: ReplyContext, args: List[str], mgr: MatchManager):
    title, body = registry.help_for(args)
    await ctx.send(f"**{title}**\n{body}")