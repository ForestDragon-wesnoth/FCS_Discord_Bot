"""
formula.py — Formula engine for the VTT.

================================================================================
OVERVIEW
================================================================================
Formulas are small, sandboxed snippets of Python-like code that can read and
write entity variables. They power:
  - $("...") substitution in command arguments (e.g. !ent hp hero "$(...)")
  - The !eval command, for ad-hoc inspection
  - The body of every passive — both entity-scoped and match-scoped global

Two evaluation modes are exposed:
  - eval_expression(src, ctx) -> value
      Parses src as a single expression. Used for $(...) substitution.
  - eval_program(src, ctx)    -> value | None
      Parses src as a sequence of statements. If the final statement is an
      expression, its value is returned; otherwise returns None. This is what
      passive formulas use.

Validation also has a public helper: validate_formula(src, mode="exec"), used
to catch malformed formulas at registration time rather than at fire time.

================================================================================
VARIABLE ACCESS
================================================================================
The only way to touch entity state is the magic `entity[X].path` form:

  entity[hero].hp                  reads `vars["hp"]` of entity with id "hero"
  entity[hero].inventory.sword     reads `vars["inventory"]["sword"]`
  entity[hero].hp = 30             writes 30 to `vars["hp"]`
  entity["rogue"].hp               same as entity[rogue].hp (string-literal id)

The X inside the brackets may be:
  - A bare identifier:      entity[hero]
  - A string literal:       entity["hero"]
  - One of two specials:
      entity[this]    or entity[current]   -> the entity whose turn it is now
      entity[self]                          -> the entity bound by the current
                                               frame (the passive's owner, or
                                               the target of a command). In an
                                               !eval call `self` is unbound.

Reading a path that does not exist raises FormulaError. There are no silent
defaults. If you need a default, write it explicitly: e.g.
  entity[self].hp = entity[self].hp + 5
will fail loudly if `hp` was never set — which is usually what you want.

Writing creates intermediate dicts as needed:
  entity[hero].inventory.bow.damage = 8
will produce `vars["inventory"] = {"bow": {"damage": 8}}` even if `inventory`
didn't previously exist.

================================================================================
OPERATORS AND BUILT-INS
================================================================================
Arithmetic:        +  -  *  /  //  %  **       (binary)
Unary:             +x   -x   not x
Comparison:        ==  !=  <  <=  >  >=
Boolean:           and  or  not
Ternary:           value_if_true if cond else value_if_false

Allowed functions: min, max, abs, round, int, float, str

Everything else is rejected: list comprehensions, lambdas, augmented
assignment (`+=`), subscripting (other than `entity[X]`), attribute access on
non-entity values, imports, function definitions, decorators, exception
handling, loops, with-blocks, etc.

================================================================================
CONDITIONAL FLOW
================================================================================
Two forms of conditional are supported.

(1) Ternary expressions — preferred when picking a single value:

  entity[self].hp = entity[self].hp + (5 if entity[self].resting == "yes" else 2)

(2) if / elif / else statements — preferred when whole assignments differ:

  if entity[self].resting == "yes":
      entity[self].stamina = (entity[self].stamina + 10) * 2
  elif entity[self].resting == "light":
      entity[self].stamina = entity[self].stamina + 6
  else:
      entity[self].stamina = entity[self].stamina + 2

Both forms compose freely: ternaries can appear inside if-bodies, conditions
can be compound boolean expressions, ifs can nest:

  if entity[self].hp <= 0 and entity[self].revives > 0:
      entity[self].hp = entity[self].max_hp // 2
      entity[self].revives = entity[self].revives - 1

`elif` is just Python's `elif` — chain as many as needed. `else` is optional.
An `if` without an `else` simply does nothing on the false branch.

There is NO match/case statement; use an elif chain. There are NO local
variables; if you need an intermediate value, recompute it or use a ternary
inside a single assignment.

================================================================================
TIPS AND COMMON PATTERNS
================================================================================
Clamp a value within bounds:
  entity[self].hp = min(max(entity[self].hp + 5, 0), entity[self].max_hp)

Conditional damage based on status:
  if entity[self].defending == "yes":
      entity[self].hp = entity[self].hp - max(entity[attacker].atk - 5, 0)
  else:
      entity[self].hp = entity[self].hp - entity[attacker].atk

Conditional self-heal in a passive (on_turn_start):
  if entity[self].hp < entity[self].max_hp:
      entity[self].hp = min(entity[self].hp + 3, entity[self].max_hp)

Multi-step assignment cascade:
  entity[self].turns_alive = entity[self].turns_alive + 1
  if entity[self].turns_alive >= 5:
      entity[self].state = "veteran"

Ternary inside a $() substitution token (note the outer quoting at the
command layer — required so the shell doesn't split on spaces):
  !ent hp hero "$(entity[self].max_hp if entity[self].blessed else entity[self].hp + 5)"

================================================================================
VAR HOOK CONTEXT
================================================================================
Formulas running inside a var-event hook (on_var_created, on_var_changed,
on_var_removed, on_var_written, or on_var_write_attempt) have access to
six extra identifiers, which are bound to None (or False, for was_clamped)
when the formula runs in any other context:

  changed_key      The dotted path of the var that fired this event, e.g.
                   "hp" or "inventory.sword.damage".
  old_value        Previous value at that path. None for on_var_created.
                   For on_var_removed on a subtree, this is the full removed
                   subtree as a dict, so a passive can inspect what was lost.
                   For on_var_write_attempt, this reflects the PRE-write
                   state (the attempt hasn't committed yet).
  new_value        New value at that path. None for on_var_removed. This is
                   the value actually stored — post-clamp if a clamp engaged.
                   For on_var_write_attempt, this is the proposed value the
                   write is ABOUT to store (the mutation hasn't happened
                   yet, but you can see what's coming).
  hook_name        One of "on_var_created", "on_var_changed",
                   "on_var_removed", or "on_var_write_attempt". Use this
                   inside an on_var_written catch-all to discriminate
                   between the sub-events. (It is never literally
                   "on_var_written" — that's a subscription name, not an
                   event kind. NOTE: on_var_written does NOT include
                   on_var_write_attempt events. Attempts are a separate
                   channel; subscribe to both if you want to audit all
                   write activity.)
  intended_value   The value the caller passed in BEFORE clamping. Equal to
                   new_value when no clamp engaged. For on_var_removed, this
                   is None (the caller intended absence). Use the difference
                   intended_value - new_value to compute overheal/overdamage
                   magnitudes.
  was_clamped      Boolean: True iff a clamp modified the value on this
                   event (intended_value != new_value). False when no clamp
                   engaged OR when the write used bypass_clamp at the
                   command layer (bypass is a deliberate override, not a
                   clamp engagement).

A note on on_var_write_attempt vs on_var_changed: the latter fires only
when the diff produces an event (i.e. the value actually changed). The
former fires for every write call. So a heal of 50 at full HP produces
NO on_var_changed event (since the clamped result equals the prior value,
diff is empty) — but it DOES produce an on_var_write_attempt event. Use
on_var_changed for "tell me when hp goes down/up"; use
on_var_write_attempt for "tell me about every heal/damage attempt
including ones that did nothing."

Typical patterns:

  Detect a particular item picked up (target="inventory" scope=children,
  on_var_created):
    if changed_key == "inventory.legendary_sword":
        entity[self].epic_quest_started = 1

  Track all HP losses (target="hp" scope=exact, on_var_changed):
    if new_value < old_value:
        entity[self].damage_taken_total = entity[self].damage_taken_total + (old_value - new_value)

  Track overheal magnitude (target="hp" scope=exact, on_var_changed):
    if was_clamped and intended_value > new_value:
        entity[self].overheal_total = entity[self].overheal_total + (intended_value - new_value)

  Track ALL wasted heal magnitude including heals at full HP (target="hp"
  scope=exact, on_var_write_attempt — fires regardless of whether the diff
  produces a change event):
    if intended_value > new_value:
        entity[self].wasted_heal = entity[self].wasted_heal + (intended_value - new_value)
    # Note: this also catches heals where intended<=old (no actual heal
    # attempted, just a write at-or-below current value). Filter further if
    # needed: `if intended_value > old_value and intended_value > new_value:`

  Generic write-logger (target="" scope=deep, on_var_written):
    entity[self].audit_log = entity[self].audit_log + "|" + hook_name + ":" + changed_key

Note: list literals aren't allowed in formulas, so accumulating multiple
values across passive firings is typically done via string concatenation
with a separator (see the audit_log example above).

================================================================================
SECURITY NOTES
================================================================================
Formulas run with __builtins__ disabled and a curated namespace. The AST is
walked and every node is checked against an explicit whitelist before any
code is compiled. Identifiers are restricted to the small set above plus
the internal __read/__write helpers injected by the transformer. There is
no path to import modules, open files, or escape the sandbox through normal
formula source. (Defensive note: a buggy formula that raises at runtime is
caught by the passive runner and reported as a "CRASHED" log line; sibling
passives still fire.)
"""
from __future__ import annotations
import ast
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import random

from logic import VTTError


class FormulaError(VTTError):
    """Raised when a formula cannot be parsed or evaluated."""
    pass


# --- whitelists --------------------------------------------------------------

# Random helpers, exposed to formulas under user-facing names. Wrapped
# so the FormulaError message is friendly when args are wrong — the bare
# random.randint / random.choice exceptions reference "non-integer stop"
# and "empty sequence" which read as Python internals rather than VTT
# user errors. Both consume the formula AST's allowed-Call path; no
# special-casing in the validator is needed beyond their inclusion in
# _ALLOWED_FUNCS below.

def _random_int(lo: Any, hi: Any) -> int:
    """random_int(lo, hi): inclusive on both ends, like a die roll.
    The match never has a seed so successive calls are independent;
    if a GM wants repeatable rolls they should write the choice out
    by hand instead."""
    if not isinstance(lo, int):
        raise FormulaError(
            f"random_int(lo, hi): lo must be int, got "
            f"{type(lo).__name__} ({lo!r})."
        )
    if not isinstance(hi, int):
        raise FormulaError(
            f"random_int(lo, hi): hi must be int, got "
            f"{type(hi).__name__} ({hi!r})."
        )
    if lo > hi:
        raise FormulaError(
            f"random_int(lo, hi): lo ({lo}) must be <= hi ({hi})."
        )
    return random.randint(lo, hi)


def _random_string(*choices: Any) -> str:
    """random_string("a", "b", ...): uniform pick from the arguments.
    Each argument must be a string — passing ints by accident would
    return a non-string and surprise downstream string comparisons,
    so we surface that as a FormulaError instead."""
    if not choices:
        raise FormulaError(
            "random_string(...): requires at least one argument."
        )
    for i, c in enumerate(choices):
        if not isinstance(c, str):
            raise FormulaError(
                f"random_string(...): all arguments must be strings; "
                f"argument {i} is {type(c).__name__} ({c!r})."
            )
    return random.choice(choices)


_ALLOWED_FUNCS: Dict[str, Any] = {
    "min": min, "max": max, "abs": abs, "round": round,
    "int": int, "float": float, "str": str,
    "random_int": _random_int,
    "random_string": _random_string,
}

# Match-bound function names. These functions are bound at namespace build
# time (FormulaEngine._namespace) because they need access to the match;
# they can't live in _ALLOWED_FUNCS like the pure helpers above. The
# validator accepts these names alongside _ALLOWED_FUNCS.
#
# The group_* functions touch Match.groups. Read-only:
#   group_has(name, eid)    -> bool   membership test
#   group_size(name)        -> int    member count (0 if group doesn't exist)
# Mutating:
#   group_add(name, eid)    -> bool   add; True iff newly added
#   group_remove(name, eid) -> bool   remove; True iff was a member
# Identity helpers (mirror the entity[self]/entity[this] shorthand for
# contexts where you need the id as a plain string to pass to another
# function, like group_has):
#   self_id()    -> str   the bound self id; raises if unbound
#   current_id() -> str   the current-turn entity id; raises if none
#
# Mutating group_add/group_remove still respect the recursion safeguards
# of any var hooks they might trigger downstream — actually they don't
# fire any var hooks (groups aren't in entity vars), so they're cheap.
# But a passive that mutates groups can still cascade indirectly via
# subsequent commands; that's the GM's problem to design around.
_MATCH_FUNC_NAMES: Tuple[str, ...] = (
    "group_has", "group_size", "group_add", "group_remove",
    "self_id", "current_id",
    # Tile data access. Tiles live on Match.tiles as a sparse dict of
    # (x, y) tuples to free-form data dicts. The validator restricts
    # attribute chains to entity[X].path, so tile data uses dotted-
    # STRING paths instead of dot-attribute syntax:
    #   tile_get(5, 5, "flame.burn_damage")   -> 5     or raises if absent
    #   tile_has(5, 5, "flame.burn_damage")   -> bool  (False on absent /
    #                                                  off-grid coords)
    # Reads only in this PR; mutation comes later with tile hooks.
    "tile_get", "tile_has",
)

_ALLOWED_NODES: Tuple[type, ...] = (
    ast.Module, ast.Expression,
    ast.Expr, ast.Assign,
    # Control flow:
    #   If         -> if / elif / else statements (elif is encoded as else: If)
    #   Pass       -> empty bodies, e.g. `if cond: pass`
    #   IfExp      -> ternary  (value_a if cond else value_b)
    ast.If, ast.Pass,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.IfExp, ast.Compare,
    ast.Call, ast.keyword,
    ast.Name, ast.Constant, ast.Load, ast.Store,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.And, ast.Or,
)


# --- evaluation context ------------------------------------------------------

# Identifier names that var hooks bind into the formula namespace. Stored as
# a module-level constant so both the validator (allowed-identifier check)
# and the namespace builder agree on the same list. Adding a new hook
# context binding means adding a name here AND making sure EvalCtx.extras
# (or its consumers) populates it.
HOOK_CONTEXT_NAMES: Tuple[str, ...] = (
    "changed_key",
    "old_value",
    "new_value",
    "hook_name",
    "intended_value",   # pre-clamp value the caller requested; same as
                        # new_value when no clamp engaged. Used for overheal
                        # tracking: intended_value - new_value gives the
                        # "lost" magnitude.
    "was_clamped",      # True iff new_value differs from intended_value
                        # because a clamp engaged. False if no clamp applied
                        # OR if the write used bypass_clamp at the command
                        # layer (bypassing was a deliberate choice, not a
                        # clamp engagement).
)


@dataclass
class EvalCtx:
    """Bindings exposed to a formula during evaluation.

    Always available:
      this    — entity whose turn it currently is (or None if unbound)
      target  — entity bound to `self` in this frame (or None if unbound)

    Var-hook-specific (populated only when firing on_var_*):
      extras  — dict of additional identifier bindings. Currently the var
                hooks populate {changed_key, old_value, new_value, hook_name}.
                Identifiers in HOOK_CONTEXT_NAMES that aren't in extras are
                bound to None in the namespace, so a formula using them in
                a non-var-hook context sees None (and probably errors at
                comparison time, which is a useful failure mode — better
                than a parse error that surprises the GM).
    """
    this: Optional[str] = None
    target: Optional[str] = None
    extras: Optional[Dict[str, Any]] = None

    def resolve_who(self, token: str) -> str:
        if token in ("this", "current"):
            if not self.this:
                raise FormulaError(
                    f"'{token}' is unbound (no current entity in turn order)."
                )
            return self.this
        if token == "self":
            if not self.target:
                raise FormulaError("'self' is unbound in this context.")
            return self.target
        return token  # literal entity id


# --- AST flattening / rewriting ---------------------------------------------

def _who_from_slice(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    raise FormulaError(
        'entity[...] index must be a bare identifier (entity[rogue]) '
        'or a string literal (entity["rogue"]).'
    )


def _flatten_entity_chain(node: ast.AST) -> Optional[Tuple[str, List[str]]]:
    """
    Recognize chains of the form entity[X].a.b.c... and return
    (who_token, ['a', 'b', 'c', ...]). Returns None if `node` is not such a chain.
    """
    parts: List[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if (isinstance(cur, ast.Subscript)
            and isinstance(cur.value, ast.Name)
            and cur.value.id == "entity"):
        slice_node = cur.slice
        if isinstance(slice_node, ast.Index):  # py<=3.8 wrapper, defensive
            slice_node = slice_node.value
        who = _who_from_slice(slice_node)
        parts.reverse()
        return who, parts
    return None


class _EntityAccessTransformer(ast.NodeTransformer):
    """Rewrites entity[X].path reads/writes into __read/__write helper calls."""

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        # Don't recurse: the whole chain is handled at once by _flatten.
        match = _flatten_entity_chain(node)
        if match is None:
            raise FormulaError("Attribute access is only allowed on entity[X].path.")
        who, parts = match
        if not parts:
            raise FormulaError("entity[X] must be followed by .path.")
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__read", ctx=ast.Load()),
                args=[ast.Constant(value=who),
                      ast.Constant(value=".".join(parts))],
                keywords=[],
            ),
            node,
        )

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        # Transform RHS (may itself contain entity[X].path reads).
        node.value = self.visit(node.value)
        if len(node.targets) != 1:
            raise FormulaError("Chained / tuple assignment is not supported.")
        match = _flatten_entity_chain(node.targets[0])
        if match is None:
            raise FormulaError("Assignment target must be entity[X].path.")
        who, parts = match
        if not parts:
            raise FormulaError("entity[X] must be followed by .path.")
        call = ast.Call(
            func=ast.Name(id="__write", ctx=ast.Load()),
            args=[ast.Constant(value=who),
                  ast.Constant(value=".".join(parts)),
                  node.value],
            keywords=[],
        )
        return ast.copy_location(ast.Expr(value=call), node)

    def visit_AugAssign(self, node: ast.AugAssign) -> ast.AST:
        raise FormulaError(
            "Augmented assignment (+= etc.) is not supported; "
            "use 'entity[X].var = entity[X].var + amount' instead."
        )

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:
        # Bare entity[X] without .attribute, or any other subscripting.
        if isinstance(node.value, ast.Name) and node.value.id == "entity":
            raise FormulaError("entity[X] must be followed by .path.")
        raise FormulaError("Subscripting is not allowed (only entity[X].path).")


def _validate_tree(tree: ast.AST) -> None:
    for n in ast.walk(tree):
        if not isinstance(n, _ALLOWED_NODES):
            raise FormulaError(f"Disallowed syntax: {type(n).__name__}")
        if isinstance(n, ast.Name):
            # Allowed Names: __read/__write (transformer-injected), the
            # built-in funcs (min, max, etc.), the hook context names
            # (changed_key, old_value, new_value, hook_name, ...) bound to
            # None outside var-hook contexts, and the match-bound
            # functions (group_has, group_size, group_add, group_remove,
            # self_id, current_id) bound at namespace-build time.
            if (n.id not in ("__read", "__write")
                    and n.id not in _ALLOWED_FUNCS
                    and n.id not in _MATCH_FUNC_NAMES
                    and n.id not in HOOK_CONTEXT_NAMES):
                raise FormulaError(f"Unknown identifier '{n.id}'.")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise FormulaError("Only direct function calls are allowed.")
            fname = n.func.id
            if (fname not in ("__read", "__write")
                    and fname not in _ALLOWED_FUNCS
                    and fname not in _MATCH_FUNC_NAMES):
                raise FormulaError(f"Function '{fname}' is not allowed.")


# --- variable-path helpers ---------------------------------------------------

def _get_path(d: Dict[str, Any], path: str) -> Any:
    keys = path.split(".")
    cur: Any = d
    for i, k in enumerate(keys):
        if not isinstance(cur, dict):
            raise FormulaError(
                f"Cannot read '{path}': '{'.'.join(keys[:i])}' is not a dict."
            )
        if k not in cur:
            raise FormulaError(f"Variable '{path}' is not defined.")
        cur = cur[k]
    return cur


def _set_path(d: Dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        node = cur.get(k)
        if not isinstance(node, dict):
            node = {}
            cur[k] = node
        cur = node
    cur[keys[-1]] = value


def validate_formula(src: str, *, mode: str = "exec") -> None:
    """Parse, transform, and validate a formula source string.

    Raises FormulaError on any syntactic or semantic problem. Useful for
    failing early when a passive is registered, rather than at hook-fire time.

    mode: 'exec' for full program bodies (assignments and expressions),
          'eval' for pure expressions.
    """
    FormulaEngine._prepare(src, mode)


# --- engine ------------------------------------------------------------------

class FormulaEngine:
    """Parses and evaluates formulas against a Match."""

    def __init__(self, match):
        self._match = match

    # ---- positional-var special-casing ------------------------------------
    # Names of the entity attributes that the formula engine treats as
    # first-class "vars" even though they live as dataclass fields on
    # Entity rather than inside entity.vars. Reads route to e.<name>;
    # writes are REJECTED with a FormulaError pointing the user at the
    # !ent tp command. The read side is "lazy intercept" — no vars
    # mirror, just a path-name special case in _read. The write side
    # was originally a per-axis tp() call, but that produces broken
    # results for any 2D move past another entity: writing x first
    # validates the (new_x, old_y) cell, which may be occupied even
    # when the actual intended destination (new_x, new_y) is free. To
    # avoid that edge case we punt on formula writes entirely until
    # the engine can validate full 2D destinations in one shot.
    # Read-only x/y also keeps the "rollback" question moot: there's
    # nothing to roll back if the write was never accepted.
    _POSITIONAL_PATHS = ("x", "y")

    def _read(self, who: str, path: str, ctx: EvalCtx) -> Any:
        eid = ctx.resolve_who(who)
        e = self._match.entities.get(eid)
        if e is None:
            raise FormulaError(f"Entity '{eid}' not found.")
        if path in self._POSITIONAL_PATHS:
            # Direct attribute read — x and y live on the dataclass,
            # not in vars, so the normal _get_path(e.vars, path) would
            # raise "Variable 'x' is not defined" for every entity.
            return getattr(e, path)
        return _get_path(e.vars, path)

    def _write(self, who: str, path: str, value: Any, ctx: EvalCtx) -> Any:
        """Route formula writes through Entity.write_var so var hooks fire.

        write_var returns log lines for any passives that matched the
        resulting events; we ignore them here because the formula engine
        has no place to surface them (the caller — _run_passive_safely or
        a command — doesn't have access). The events still fire and the
        passives still run; only the log lines are discarded. If a passive
        chain needs to surface logs, it should originate from a command
        path that captures them.

        Writes to "x" or "y" are rejected. See the comment on
        _POSITIONAL_PATHS — the per-axis tp() validation was unsound for
        any 2D move past another entity. Until the engine supports
        "validate destination then move" in a single check, the user
        should use the `!ent tp` command (or a movement-rule command)
        which already takes both coordinates at once.
        """
        eid = ctx.resolve_who(who)
        e = self._match.entities.get(eid)
        if e is None:
            raise FormulaError(f"Entity '{eid}' not found.")
        if path in self._POSITIONAL_PATHS:
            raise FormulaError(
                f"entity[{who}].{path} is read-only from formulas. "
                f"Use `!ent tp <id> <x> <y>` to move an entity — that "
                f"command validates the full destination at once, "
                f"avoiding the per-axis collision quirk that would "
                f"break legal 2D moves past another entity."
            )
        # write_var does the diff + event firing + mutation in one shot.
        # _ = e.write_var(path, value)  # log lines discarded
        e.write_var(path, value)
        return value

    def _namespace(self, ctx: EvalCtx) -> Dict[str, Any]:
        """Build the safe namespace for evaluating a formula.

        Includes:
          - __read/__write (transformer-injected helpers)
          - The allowed built-in functions (min, max, etc.)
          - Hook context names (changed_key, old_value, new_value, hook_name,
            intended_value, was_clamped) bound from ctx.extras. Defaults:
            was_clamped -> False (boolean-meaningful default), all others
            -> None. Unconditional binding keeps the validator simple — a
            formula referencing changed_key in a non-var-hook context just
            sees None (or False).
          - Match-bound functions (group_*, self_id, current_id). These
            close over self._match and the provided ctx so the formula
            doesn't need to thread either through explicitly.
        """
        extras = ctx.extras or {}
        ns: Dict[str, Any] = {
            "__read":  lambda who, path:        self._read(who, path, ctx),
            "__write": lambda who, path, value: self._write(who, path, value, ctx),
            **_ALLOWED_FUNCS,
        }
        # Per-name default: was_clamped is boolean-flavored (default False);
        # everything else defaults to None.
        _CONTEXT_DEFAULTS = {"was_clamped": False}
        for name in HOOK_CONTEXT_NAMES:
            ns[name] = extras.get(name, _CONTEXT_DEFAULTS.get(name))

        # Match-bound group functions. Each takes string args; entity-id
        # args go through ctx.resolve_who so the special tokens "self",
        # "this", and "current" work the same as inside entity[X].path
        # — meaning a formula can write group_add("swarm", "self") if
        # the bare-identifier shorthand self_id() feels too verbose.
        match = self._match
        def _eid(token: Any) -> str:
            if not isinstance(token, str):
                raise FormulaError(
                    f"Entity id argument must be a string, got {type(token).__name__}."
                )
            return ctx.resolve_who(token)
        def _group_has(name: Any, eid_token: Any) -> bool:
            if not isinstance(name, str):
                raise FormulaError("group_has(name, eid): name must be a string.")
            eid = _eid(eid_token)
            members = match.groups.get(name, [])
            return eid in members
        def _group_size(name: Any) -> int:
            if not isinstance(name, str):
                raise FormulaError("group_size(name): name must be a string.")
            return len(match.groups.get(name, []))
        def _group_add(name: Any, eid_token: Any) -> bool:
            if not isinstance(name, str):
                raise FormulaError("group_add(name, eid): name must be a string.")
            eid = _eid(eid_token)
            try:
                return match.add_to_group(name, eid)
            except VTTError as ex:
                raise FormulaError(str(ex))
        def _group_remove(name: Any, eid_token: Any) -> bool:
            if not isinstance(name, str):
                raise FormulaError("group_remove(name, eid): name must be a string.")
            eid = _eid(eid_token)
            # If the group doesn't exist, treat as a no-op (False) rather
            # than raising — symmetric with group_size returning 0 for an
            # absent group. Mutating a never-defined group from a passive
            # is most likely a typo; we surface that as a "did nothing"
            # rather than crashing the passive chain.
            if name not in match.groups:
                return False
            return match.remove_from_group(name, eid)
        def _self_id() -> str:
            if not ctx.target:
                raise FormulaError("self_id(): self is unbound in this context.")
            return ctx.target
        def _current_id() -> str:
            if not ctx.this:
                raise FormulaError("current_id(): no current entity (turn order empty).")
            return ctx.this

        ns["group_has"]    = _group_has
        ns["group_size"]   = _group_size
        ns["group_add"]    = _group_add
        ns["group_remove"] = _group_remove
        ns["self_id"]      = _self_id
        ns["current_id"]   = _current_id

        # Tile data accessors. tile_get raises FormulaError on missing
        # paths (matching the "Variable 'foo' is not defined" style of
        # entity-var reads); tile_has returns False instead of raising
        # so formulas can use it as a guard before tile_get. Both
        # accept integer coordinates and a dotted string path.
        def _tile_get(x: Any, y: Any, path: Any) -> Any:
            if not isinstance(x, int) or isinstance(x, bool):
                raise FormulaError(
                    f"tile_get(x, y, path): x must be int, got "
                    f"{type(x).__name__}."
                )
            if not isinstance(y, int) or isinstance(y, bool):
                raise FormulaError(
                    f"tile_get(x, y, path): y must be int, got "
                    f"{type(y).__name__}."
                )
            if not isinstance(path, str):
                raise FormulaError(
                    f"tile_get(x, y, path): path must be str, got "
                    f"{type(path).__name__}."
                )
            try:
                return match.tile_get_path(x, y, path)
            except VTTError as ex:
                # OutOfBounds / NotFound / empty-path errors from
                # tile_get_path are all VTTError subclasses; surface
                # them as FormulaError so the ❌ formula error path
                # picks them up consistently.
                raise FormulaError(str(ex))

        def _tile_has(x: Any, y: Any, path: Any) -> bool:
            if not isinstance(x, int) or isinstance(x, bool):
                raise FormulaError(
                    f"tile_has(x, y, path): x must be int, got "
                    f"{type(x).__name__}."
                )
            if not isinstance(y, int) or isinstance(y, bool):
                raise FormulaError(
                    f"tile_has(x, y, path): y must be int, got "
                    f"{type(y).__name__}."
                )
            if not isinstance(path, str):
                raise FormulaError(
                    f"tile_has(x, y, path): path must be str, got "
                    f"{type(path).__name__}."
                )
            return match.tile_has_path(x, y, path)

        ns["tile_get"] = _tile_get
        ns["tile_has"] = _tile_has

        return ns

    @staticmethod
    def _prepare(src: str, mode: str) -> ast.AST:
        try:
            tree = ast.parse(src, mode=mode)
        except SyntaxError as e:
            raise FormulaError(f"Syntax error: {e.msg}")
        tree = _EntityAccessTransformer().visit(tree)
        ast.fix_missing_locations(tree)
        _validate_tree(tree)
        return tree

    def eval_expression(self, src: str, ctx: EvalCtx) -> Any:
        tree = self._prepare(src, "eval")
        code = compile(tree, "<formula>", "eval")
        try:
            return eval(code, {"__builtins__": {}}, self._namespace(ctx))
        except FormulaError:
            raise
        except Exception as e:
            raise FormulaError(f"Runtime error: {e}")

    def eval_program(self, src: str, ctx: EvalCtx) -> Any:
        """Run statements; if the source ends with an expression, return its value."""
        try:
            full = ast.parse(src, mode="exec")
        except SyntaxError as e:
            raise FormulaError(f"Syntax error: {e.msg}")

        trailing_expr = None
        if full.body and isinstance(full.body[-1], ast.Expr):
            trailing_expr = full.body.pop().value

        full = _EntityAccessTransformer().visit(full)
        ast.fix_missing_locations(full)
        _validate_tree(full)

        ns = self._namespace(ctx)
        try:
            if full.body:
                exec(compile(full, "<formula>", "exec"), {"__builtins__": {}}, ns)
            if trailing_expr is None:
                return None
            expr_tree = ast.Expression(body=trailing_expr)
            expr_tree = _EntityAccessTransformer().visit(expr_tree)
            ast.fix_missing_locations(expr_tree)
            _validate_tree(expr_tree)
            return eval(compile(expr_tree, "<formula>", "eval"),
                        {"__builtins__": {}}, ns)
        except FormulaError:
            raise
        except Exception as e:
            raise FormulaError(f"Runtime error: {e}")


# --- arg-token resolution ($(...) substitution) -----------------------------

_QUOTED_RE = re.compile(r'^\s*\$\(\s*"(?P<body>.*)"\s*\)\s*$', flags=re.DOTALL)
_BARE_RE   = re.compile(r'^\s*\$\((?P<body>.*)\)\s*$',           flags=re.DOTALL)


def resolve_arg_token(token: str, match, self_id: Optional[str] = None) -> str:
    """
    If `token` is a $("...") or $(...) formula, evaluate it as an expression
    against `match` (with `this` = current-turn entity, `self` = self_id) and
    return the stringified result. Otherwise return `token` unchanged.
    """
    if not isinstance(token, str) or not token.startswith("$("):
        return token
    m = _QUOTED_RE.match(token) or _BARE_RE.match(token)
    if m is None:
        raise FormulaError(f"Malformed formula token: {token!r}")
    this_id = match.current_entity_id() if hasattr(match, "current_entity_id") else None
    ctx = EvalCtx(this=this_id, target=self_id)
    val = FormulaEngine(match).eval_expression(m.group("body"), ctx)
    return _stringify(val)


def _stringify(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if v is None:
        return ""
    return str(v)


# --- public validation helper -----------------------------------------------

def validate_program(src: str) -> None:
    """
    Parse and validate a formula in program mode (statements + optional
    trailing expression). Raises FormulaError on syntax errors or disallowed
    constructs. Used to eagerly catch broken passive formulas at add time.
    Does not need a match — purely a syntax check.
    """
    FormulaEngine._prepare(src, "exec")