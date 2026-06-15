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
  - A bare identifier:      entity[hero]    (the literal entity id "hero")
  - A string literal:       entity["hero"]
  - One of the specials:
      entity[this]    or entity[current]   -> the entity whose turn it is now
      entity[self]                          -> the entity bound by the current
                                               frame (the passive's owner, or
                                               the target of a command). In an
                                               !eval call `self` is unbound.
  - Any other expression (DYNAMIC):  evaluated at runtime, its value used
    as the entity id. Examples:
      entity[entity[self].target_id].hp    -> read self's target_id var,
                                              then that entity's hp
      entity[nearest_entity(self, "hostile")].hp
      entity[eid].hp    inside a function whose parameter is `eid`
                        -> uses the value passed for eid (a bare identifier
                           is dynamic ONLY when it's a function parameter;
                           otherwise it stays a literal id, so existing
                           entity[goblin] references are unaffected)
    A dynamic index that doesn't evaluate to a string raises FormulaError.

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

Allowed functions:
  Core:      min, max, abs, round, int, float, str, len
  Math:      sqrt, floor, ceil, pow, clamp(v, lo, hi), sign
  Random:    random_int, random_string, roll("2d6+3")
  Geometry:  distance, angle, direction_to
  Areas:     cells_in_burst, cells_in_line, cells_in_cone, cells_in_rect
             (return lists of (x,y) tuples; pair with len() OR iterate
             with a for-loop). clip_cells(cells) drops off-grid cells.
  Spatial:   entities_within(eid, n, mode, relation),
             nearest_entity(eid, relation, mode)   (scan alive entities
             relative to a reference; relation = ""/"hostile"/"ally"/
             "same_team"/"attackable"),
             entities_in_area(x, y, n, mode)   (the coord-rooted twin —
             scans alive entities around a POINT, not an entity),
             entities_in_cone / entities_in_rect / entities_in_line_ignorelos
             (entity-returning twins of the cells_in_* shapes),
             entities_on_los (line, sight-aware, between-only),
             first_opaque (first terrain blocker as an (x,y) coord)
  Match-wide queries (no reference entity; all loopable):
             all_entities(),
             entities_with_status(name),
             entities_with_var(path)
  Teams:     is_same_team, is_hostile, is_part_of_team, is_attackable
  Groups:    group_has, group_size, group_add, group_remove,
             group_members(name)   (insertion-order id list; loopable),
             entity_groups(eid)   (reverse index: groups containing eid)
  Statuses:  status_has, status_has_path, status_get, status_set,
             status_del, status_add, status_remove   (dict-of-dicts:
             status_get raises on missing, *_add / *_remove / *_del
             return bool),
             status_names(eid)   (loopable list of active status names)
  Vars:      var_keys(eid, path="")   (loopable list of keys at a vars
             path; "" = top-level var names),
             var_has(eid, "path"),
             var_get(eid, "path")      (runtime-path equivalents of
             var_set(eid, "path", v)    entity[X].path — same semantics,
             var_del(eid, "path")       but the path is computed at
                                        runtime so iteration over
                                        var_keys works)
  Identity:  self_id, current_id  (bare identifiers `self`, `this`,
             `current` also work — bind to ctx.target / ctx.this as
             string ids, or to None when unbound)
  Tiles:     tile_get, tile_has, tile_set, tile_del, tile_clear,
             tile_keys(x, y)   (top-level keys in the tile's data dict),
             fire_tile_hook(x, y, hook_name, eid=None)
                                (programmatically fire a tile hook;
                                returns log-line count — useful for
                                chained-effect tiles and testing)
  Movement:  move_entity(eid, x, y)   (formula-side `!ent tp` — same
                                bounds/occupancy validation; fires
                                tile on_enter/on_exit/on_stop and
                                on_entity_moved; returns the new
                                (x,y) tuple),
             move_step(eid, direction)   (one-cell directional step;
                                returns True if it moved, False if
                                blocked — honors allow_diagonal_movement)
  Rules:     rule_get(name)   (read a system rule's effective value;
                                None for unknown names)
  User-defined: any function defined via `!func def` on the match (see
                "USER-DEFINED FUNCTIONS" below)

For-loops (CONSTRAINED form):
  for eid in entities_within(self, 3, "square_radius_distance", "hostile"):
      entity[eid].hp = entity[eid].hp - 5
  for (cx, cy) in cells_in_burst(5, 5, 1):
      tile_set(cx, cy, "burned", 1)
The iterable MUST be a direct call to a loopable function (e.g.
entities_within, group_members, cells_in_burst/line/cone/rect,
entities_in_area/line/cone/rect, clip_cells, and the introspection
helpers — see _LOOPABLE_FUNCS). The target may be a single name
(entity id / scalar) or a tuple of names (for coord unpacking). Total iterations across all loops in
one evaluation are bounded by the formula_loop_limit rule
(default 10000). No `else:`, no break/continue. Loop variables are
in scope for the body (including as `entity[<var>]` indices).

Everything else is rejected: list comprehensions, lambdas, augmented
assignment (`+=`), subscripting (other than `entity[X]`), attribute access on
non-entity values, imports, function definitions, decorators, exception
handling, while-loops, with-blocks, etc.

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
USER-DEFINED FUNCTIONS
================================================================================
Reusable formula functions are defined per-match with `!func def` and then
callable by name from any formula on that match (passive bodies, $()
substitution, !eval, tile hooks, and other functions). They are the
"named macro / formula library" mechanism.

  !func def mitigated raw,armor "max(raw - armor, 0)"
  !func def regen_amount missing "ceil(missing / 4)"

A function has a name, an ordered parameter list, and a body. The body is
a full formula program: it may have statements (including entity[X] writes
that take effect as side effects) and an optional trailing expression whose
value becomes the return value. A body with no trailing expression returns
None.

Inside the body:
  - parameter names are bound to the call's argument values, as plain
    identifiers (e.g. `raw`, `armor` above)
  - entity[self] / entity[this] / entity[current] resolve against the
    CALLER's context — functions are inline macros running in the caller's
    frame, not isolated. So a function called from a passive sees that
    passive's `self`.
  - other functions (built-in or user-defined) are callable, enabling
    composition and recursion. Recursion is bounded by the
    formula_function_recursion_limit rule (default 64) — exceeding it
    raises a FormulaError rather than blowing the stack.

NOTE: the entity[X] accessor takes X as a literal token (self/this/current
or an entity id), NOT as a variable — you cannot pass an entity id through
a parameter and write entity[param]. Target a specific entity with the
special tokens or a hardcoded id.

Functions are stored on the match (serialized with save/load) and can be
seeded from a GameSystem's function library at match creation. Manage them
with: !func def, !func del, !func list, !func info.

Example — define damage math once, use it in many passives:

  !func def mitigated raw,armor "max(raw - armor, 0)"
  !passive add hero retaliate on_turn_start "entity[attacker].hp = entity[attacker].hp - mitigated(entity[self].thorns, entity[attacker].armor)"

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
OTHER EVENT-HOOK CONTEXTS
================================================================================
Beyond the var-event bindings, the engine fires three other event kinds
that pass extra identifiers via the same mechanism:

  on_entity_moved          fires after a tp / move_dirs / move_step /
                            move_entity. Bindings:
                              from_x, from_y   — old position
                              to_x, to_y       — new position
                              hook_name        — "on_entity_moved"

  on_status_added /        fires when a status appears / disappears /
  on_status_removed /      has data fields changed via set/del. Bindings:
  on_status_changed          status_name        — the affected status
                              old_value          — pre-change data dict
                                                   (None for added)
                              new_value          — post-change data dict
                                                   (None for removed)
                              hook_name          — the event kind

Tile time-hooks (on_round_start, on_round_end, on_turn_start, on_turn_end)
fire on each registered tile at the round/turn lifecycle moment.
Bindings: tile_x, tile_y (the firing tile's coords); hook_name. `self`
resolves to the currently-acting entity at the moment of fire (the
entity whose turn is current); if there isn't one, `self` is unbound and
a formula referencing it errors with the standard message.

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
import copy
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import random

from logic import VTTError, NotFound


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

# The random helpers take an `rng` (either the `random` module itself
# for unseeded global rolls, or a match-bound random.Random instance
# when the random_seed rule is set — see FormulaEngine._namespace).
# Both expose .randint / .choice so the same impl works for either.

def _random_int_impl(rng, lo: Any, hi: Any) -> int:
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
    return rng.randint(lo, hi)


def _random_string_impl(rng, choices) -> str:
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
    return rng.choice(choices)


def _random_int(lo: Any, hi: Any) -> int:
    """random_int(lo, hi): inclusive on both ends, like a die roll.

    By default rolls are independent (unseeded global RNG). If the match
    sets the `random_seed` rule, the engine shadows this with a
    match-bound seeded RNG so a session's rolls become reproducible —
    see the random_seed rule docs."""
    return _random_int_impl(random, lo, hi)


def _random_string(*choices: Any) -> str:
    """random_string("a", "b", ...): uniform pick from the arguments.
    Each argument must be a string — passing ints by accident would
    return a non-string and surprise downstream string comparisons,
    so we surface that as a FormulaError instead. Honors the
    random_seed rule the same way random_int does."""
    return _random_string_impl(random, choices)


# Caps on a single roll() so a typo can't hang the bot. A spell that
# rolls "1000d6" is already absurd; nobody legitimately rolls more.
_ROLL_MAX_DICE = 1000
_ROLL_MAX_SIDES = 1_000_000
_ROLL_TERM_RE = re.compile(r"^([+-]?)(\d*)d(\d+)$|^([+-]?)(\d+)$")


def _roll_impl(rng, spec: Any) -> int:
    """roll("2d6+3"): evaluate standard dice notation and return the
    integer total. Implementation shared by the seeded and unseeded
    bindings — the RNG is injected so the match's random_seed rule makes
    rolls reproducible the same way random_int does.

    Grammar: one or more terms joined by + / -, each term either a die
    group `NdM` (N optional, defaults to 1) or a flat integer modifier.
    Examples: "d20", "2d6+3", "1d8-1", "3d6+2d4+1". Whitespace and case
    are ignored. Each die contributes an independent randint(1, M)."""
    if not isinstance(spec, str):
        raise FormulaError(
            f"roll(spec): spec must be a dice string like '2d6+3', got "
            f"{type(spec).__name__} ({spec!r})."
        )
    s = spec.replace(" ", "").lower()
    if not s:
        raise FormulaError("roll(spec): empty dice expression.")
    # Split into signed terms while keeping each leading +/-; a bare
    # leading term has an implicit '+'.
    terms = re.findall(r"[+-]?[^+-]+", s)
    if not terms or "".join(terms) != s:
        raise FormulaError(f"roll(spec): malformed dice expression '{spec}'.")
    total = 0
    for term in terms:
        m = _ROLL_TERM_RE.match(term)
        if m is None:
            raise FormulaError(
                f"roll(spec): malformed term '{term}' in '{spec}' — "
                f"expected NdM or a flat integer."
            )
        if m.group(3) is not None:
            # Die group: sign, optional count, sides.
            sgn = -1 if m.group(1) == "-" else 1
            count = int(m.group(2)) if m.group(2) else 1
            sides = int(m.group(3))
            if count < 1 or count > _ROLL_MAX_DICE:
                raise FormulaError(
                    f"roll(spec): die count {count} out of range "
                    f"(1..{_ROLL_MAX_DICE}) in '{spec}'."
                )
            if sides < 1 or sides > _ROLL_MAX_SIDES:
                raise FormulaError(
                    f"roll(spec): die sides {sides} out of range "
                    f"(1..{_ROLL_MAX_SIDES}) in '{spec}'."
                )
            for _ in range(count):
                total += sgn * rng.randint(1, sides)
        else:
            # Flat integer modifier.
            sgn = -1 if m.group(4) == "-" else 1
            total += sgn * int(m.group(5))
    return total


def _roll(spec: Any) -> int:
    """roll("2d6+3"): roll standard dice notation, returning the integer
    total. By default rolls are independent (unseeded global RNG); when
    the match sets the random_seed rule the engine shadows this with a
    match-bound seeded RNG so a session's rolls become reproducible —
    see the random_seed rule docs (same mechanism as random_int)."""
    return _roll_impl(random, spec)


# Distance modes accepted by the distance() formula function. The full
# names match how the user thinks about them; the short aliases save
# typing in deeply-nested formulas. All four entries below map to the
# same three behaviors. Adding a mode means appending here AND extending
# the if-chain inside _distance.
_DISTANCE_MODES: Tuple[str, ...] = (
    "square_radius_distance",  # Chebyshev: max(|dx|, |dy|)
    "manhattan_distance",      # taxicab:   |dx| + |dy|
    "euclidean_distance",      # pythag:    sqrt(dx^2 + dy^2), float
    # short aliases
    "square_radius", "manhattan", "euclidean",
)


def _distance(x1: Any, y1: Any, x2: Any, y2: Any,
              mode: Any = "square_radius_distance") -> Any:
    """distance(x1, y1, x2, y2, mode="square_radius_distance"): how far
    apart two map cells are under the chosen distance metric.

    Modes:
      square_radius_distance  Chebyshev / "king's move" distance —
                              max(|dx|, |dy|). Diagonal counts as one
                              step, matching how !ent move treats
                              diagonal directions when they're enabled.
                              Returns int. (Default — most common in
                              tile-based RPGs for spell range etc.)
      manhattan_distance      Taxicab — |dx| + |dy|. Diagonals cost 2
                              steps. Use this for systems that forbid
                              diagonal movement entirely. Returns int.
      euclidean_distance      Straight-line — sqrt(dx^2 + dy^2),
                              assuming each cell is 1x1. Returns float.
      Short aliases: "square_radius", "manhattan", "euclidean".

    Entity collisions are NOT considered — this is geometric distance
    between two points, not pathfinding cost. A wall, another entity,
    or off-map blockers between (x1,y1) and (x2,y2) don't change the
    result. For "shortest traversable path" you'd want a separate
    pathfind function (not in this engine yet).

    Coordinates may be ints or floats; the four args must be numeric.
    For entity-to-entity distance just pass entity[A].x, entity[A].y,
    entity[B].x, entity[B].y.
    """
    def _num(v, name):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FormulaError(
                f"distance(...): {name} must be a number, got "
                f"{type(v).__name__}."
            )
        return v
    x1 = _num(x1, "x1"); y1 = _num(y1, "y1")
    x2 = _num(x2, "x2"); y2 = _num(y2, "y2")
    if not isinstance(mode, str):
        raise FormulaError(
            f"distance(..., mode): mode must be a string, got "
            f"{type(mode).__name__}."
        )
    if mode not in _DISTANCE_MODES:
        allowed = ", ".join(sorted(set(_DISTANCE_MODES)))
        raise FormulaError(
            f"distance(...): unknown mode '{mode}'. Allowed: {allowed}."
        )
    dx = abs(x2 - x1)
    dy = abs(y2 - y1)
    if mode in ("square_radius_distance", "square_radius"):
        return int(max(dx, dy))
    if mode in ("manhattan_distance", "manhattan"):
        return int(dx + dy)
    # euclidean — returns float by design
    return math.sqrt(dx * dx + dy * dy)


# Angle reference axes. Each entry is (dx, dy) — the unit vector
# pointing along that reference. We use these to rotate the raw angle
# so that whichever axis the caller picked reads as 0°. The pairs match
# DIRECTION_VECTORS in logic.py (up = -y because screen coords).
_ANGLE_REFERENCES: Dict[str, Tuple[int, int]] = {
    "up":    ( 0, -1),
    "right": ( 1,  0),
    "down":  ( 0,  1),
    "left":  (-1,  0),
}


def _angle(x1: Any, y1: Any, x2: Any, y2: Any,
           reference: Any = "up", direction: Any = "cw",
           signed: Any = False, as_int: Any = True) -> Any:
    """angle(x1, y1, x2, y2, reference="up", direction="cw",
       signed=False, as_int=True): bearing FROM (x1,y1) TO (x2,y2).

    Default convention is compass bearing: 0° points "up" (the same
    direction !ent move treats as up, i.e. y decreasing), and angles
    grow clockwise. So:
      (0,0) -> (0,-1)  is 0°    (target directly up)
      (0,0) -> (1, 0)  is 90°   (target directly right)
      (0,0) -> (0, 1)  is 180°  (target directly down)
      (0,0) -> (-1,0)  is 270°  (target directly left)
      (0,0) -> (1,-1)  is 45°   (target up-right diagonal)

    Knobs:
      reference   "up" (default) | "right" | "down" | "left". Rotates
                  the zero point. reference="right" with direction="ccw"
                  gives the standard math convention (0° = +x, CCW
                  positive).
      direction   "cw" (default — compass) | "ccw" (math).
      signed      False (default — range [0, 360)) | True (range
                  [-180, 180], with positive in the chosen direction).
      as_int      True (default — rounded to nearest degree, returns
                  int) | False (returns float with arbitrary precision).

    Same-point case: angle(x, y, x, y) is undefined geometrically
    (no displacement). Returns 0 (treating "no direction" as "no
    rotation from the reference") rather than raising — formulas
    often compute angles before knowing whether the target is
    distinct, and forcing every caller to guard would be tedious.

    Entity collisions are ignored — this is the angle between two
    points, not the angle of an actual line of sight.
    """
    def _num(v, name):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FormulaError(
                f"angle(...): {name} must be a number, got "
                f"{type(v).__name__}."
            )
        return v
    x1 = _num(x1, "x1"); y1 = _num(y1, "y1")
    x2 = _num(x2, "x2"); y2 = _num(y2, "y2")
    if not isinstance(reference, str) or reference not in _ANGLE_REFERENCES:
        allowed = ", ".join(sorted(_ANGLE_REFERENCES.keys()))
        raise FormulaError(
            f"angle(..., reference): must be one of {allowed}, got "
            f"{reference!r}."
        )
    if direction not in ("cw", "ccw"):
        raise FormulaError(
            f"angle(..., direction): must be 'cw' or 'ccw', got "
            f"{direction!r}."
        )
    if not isinstance(signed, bool):
        raise FormulaError(
            f"angle(..., signed): must be bool, got {type(signed).__name__}."
        )
    if not isinstance(as_int, bool):
        raise FormulaError(
            f"angle(..., as_int): must be bool, got {type(as_int).__name__}."
        )
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return 0 if as_int else 0.0
    # Compute the angle FROM "up" (y=-1) measured CLOCKWISE. Using
    # math.atan2(dx, -dy) gives exactly that: atan2's first arg is
    # the "y component" of the rotation it computes — by feeding dx
    # there and -dy as the "x component", we rotate the standard
    # math angle into compass orientation in one call.
    raw = math.degrees(math.atan2(dx, -dy))  # range (-180, 180], 0=up, CW
    # Rotate by the chosen reference (offset from "up" measured CW).
    ref_offset_cw = {
        "up": 0.0, "right": 90.0, "down": 180.0, "left": 270.0,
    }[reference]
    a = raw - ref_offset_cw
    if direction == "ccw":
        a = -a
    if signed:
        # Wrap into (-180, 180] — directly "behind" (the reference axis
        # reversed) returns 180, NOT -180, since 180 is the cleaner
        # representation and matches what GMs would write.
        a = a % 360.0
        if a > 180.0:
            a -= 360.0
    else:
        a = a % 360.0
    if as_int:
        # Round to nearest integer then re-wrap: round(359.6) = 360,
        # which should be 0 (unsigned) or -180→180 normalized (signed).
        a = int(round(a))
        if not signed:
            a = a % 360
        elif a > 180:
            a -= 360
        elif a <= -180:
            a += 360
    return a


def _direction_to(x1: Any, y1: Any, x2: Any, y2: Any,
                  allow_diagonals: Any = True) -> str:
    """direction_to(x1, y1, x2, y2, allow_diagonals=True): name of the
    direction FROM (x1,y1) TO (x2,y2), snapped to the engine's cardinal
    (or cardinal+diagonal) direction set.

    With allow_diagonals=True (the default), returns one of:
      "up", "up_right", "right", "down_right",
      "down", "down_left", "left", "up_left"
    Each cell adjacent to (x1,y1) maps to one of these eight names, and
    farther cells snap to the closest of the eight by sign of dx/dy.

    With allow_diagonals=False, returns one of "up", "down", "left",
    "right". Pure diagonals are "clumsily rounded" to the dominant
    axis; perfect 45° ties (|dx| == |dy|) snap to the VERTICAL axis,
    matching the per-step facing rule for cardinal-only systems
    (see move_dirs in logic.py).

    Same-point case (x1,y1) == (x2,y2) returns "" (empty string).
    There's no displacement and thus no direction; callers wanting a
    sentinel can compare `== ""` rather than guarding with distance().

    Coords may be ints or floats; the result is always one of the
    direction strings above (or "") regardless of input precision.
    Entity collisions are ignored, like distance() and angle() —
    this is pure geometry.
    """
    def _num(v, name):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise FormulaError(
                f"direction_to(...): {name} must be a number, got "
                f"{type(v).__name__}."
            )
        return v
    x1 = _num(x1, "x1"); y1 = _num(y1, "y1")
    x2 = _num(x2, "x2"); y2 = _num(y2, "y2")
    if not isinstance(allow_diagonals, bool):
        raise FormulaError(
            f"direction_to(..., allow_diagonals): must be bool, got "
            f"{type(allow_diagonals).__name__}."
        )
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return ""
    # Helper for the sign (vector component) of a number — we don't use
    # math.copysign because that returns +/- 1.0 even for input 0 and
    # we want true 0 for "no displacement on this axis".
    def _sign(v):
        if v > 0: return 1
        if v < 0: return -1
        return 0
    sx, sy = _sign(dx), _sign(dy)
    if allow_diagonals:
        # 8-way snap: each component independently contributes its sign.
        names = {
            ( 0, -1): "up",
            ( 1, -1): "up_right",
            ( 1,  0): "right",
            ( 1,  1): "down_right",
            ( 0,  1): "down",
            (-1,  1): "down_left",
            (-1,  0): "left",
            (-1, -1): "up_left",
        }
        return names[(sx, sy)]
    # 4-way snap: pick the dominant axis. On exact ties prefer
    # vertical (the "ties-prefer-vertical" convention used by
    # cardinal-only diagonal facing in move_dirs).
    if abs(dx) > abs(dy):
        return "right" if sx > 0 else "left"
    # abs(dy) >= abs(dx) — vertical wins ties
    return "down" if sy > 0 else "up"


# --- math helpers ------------------------------------------------------------
# Pure numeric functions exposed to formulas. min/max/abs/round/int/float
# already live in _ALLOWED_FUNCS; these fill the common gaps (roots,
# rounding direction, clamping, sign). Each validates its arguments and
# raises FormulaError on a type mismatch rather than letting a bare
# Python TypeError bubble up as a generic "Runtime error".

def _num_arg(v: Any, fname: str, argname: str) -> Any:
    """Shared numeric-arg guard. bool is rejected even though it's an
    int subclass — passing True/False to a math function is almost
    always a mistake, and silently treating True as 1 hides it."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise FormulaError(
            f"{fname}(...): {argname} must be a number, got "
            f"{type(v).__name__}."
        )
    return v


def _sqrt(v: Any) -> float:
    _num_arg(v, "sqrt", "value")
    if v < 0:
        raise FormulaError(f"sqrt(value): value must be >= 0, got {v}.")
    return math.sqrt(v)


def _floor(v: Any) -> int:
    _num_arg(v, "floor", "value")
    return math.floor(v)


def _ceil(v: Any) -> int:
    _num_arg(v, "ceil", "value")
    return math.ceil(v)


def _pow(base: Any, exp: Any) -> Any:
    _num_arg(base, "pow", "base")
    _num_arg(exp, "pow", "exp")
    # math via Python's ** so int**int stays int where possible (matches
    # the existing ** operator's behavior); guard the 0**negative and
    # negative-base-fractional-exp cases that raise/return complex.
    try:
        result = base ** exp
    except ZeroDivisionError:
        raise FormulaError("pow(base, exp): 0 raised to a negative power.")
    if isinstance(result, complex):
        raise FormulaError(
            "pow(base, exp): result is complex (negative base with a "
            "fractional exponent)."
        )
    return result


def _clamp(v: Any, lo: Any, hi: Any) -> Any:
    """clamp(value, lo, hi): value pinned into [lo, hi]. Returns lo if
    value < lo, hi if value > hi, else value unchanged. Requires
    lo <= hi — an inverted range is almost certainly a bug, so we
    raise rather than silently swap."""
    _num_arg(v, "clamp", "value")
    _num_arg(lo, "clamp", "lo")
    _num_arg(hi, "clamp", "hi")
    if lo > hi:
        raise FormulaError(
            f"clamp(value, lo, hi): lo ({lo}) must be <= hi ({hi})."
        )
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _sign(v: Any) -> int:
    """sign(value): -1 if negative, 0 if zero, 1 if positive. Returns an
    int regardless of input type so it composes cleanly with int math."""
    _num_arg(v, "sign", "value")
    if v > 0:
        return 1
    if v < 0:
        return -1
    return 0


def _len(v: Any) -> int:
    """len(value): number of items in a list (or characters in a string).

    Primarily the companion to the list-returning geometry functions —
    `len(entities_within(self, 3, "square_radius_distance", "hostile"))`
    counts nearby enemies for a condition like "if 3+ foes adjacent".
    Until formula for-loops land, this is the main way to consume the
    list/coordinate-list return values. Raises FormulaError on a value
    that has no length (e.g. a number)."""
    if isinstance(v, (list, tuple, str, dict)):
        return len(v)
    raise FormulaError(
        f"len(value): value has no length (got {type(v).__name__}); "
        f"len works on lists, strings, and the coordinate lists "
        f"returned by cells_in_* / entities_within."
    )


# --- area / shape helpers ----------------------------------------------------
# Pure geometric functions that return LISTS of (x, y) coordinate tuples
# for an area shape. They take no match and do no grid clipping — a burst
# at the map edge will include off-grid coordinates, which simply match
# no tile/entity downstream. Combine with entities_within (to find who's
# in the shape) or, once formula for-loops exist, iterate the cells
# directly. Coordinates are 1-indexed to match the rest of the engine,
# but nothing here enforces the grid bounds.

def _coord_int(v: Any, fname: str, argname: str) -> int:
    """Coerce a coordinate arg to int, rejecting non-numerics. Floats are
    floored (a fractional coordinate snaps to its containing cell)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise FormulaError(
            f"{fname}(...): {argname} must be a number, got "
            f"{type(v).__name__}."
        )
    return int(math.floor(v))


def _cells_in_burst(x: Any, y: Any, r: Any,
                    mode: Any = "square_radius_distance") -> list:
    """cells_in_burst(x, y, r, mode="square_radius_distance"): every cell
    within distance r of (x, y) under the given distance metric, INCLUDING
    the center. Shape depends on mode: square_radius -> filled square,
    manhattan -> diamond, euclidean -> disc. Returns a list of (cx, cy)
    tuples sorted in (x, y) order."""
    cx0 = _coord_int(x, "cells_in_burst", "x")
    cy0 = _coord_int(y, "cells_in_burst", "y")
    if isinstance(r, bool) or not isinstance(r, (int, float)):
        raise FormulaError(
            f"cells_in_burst(...): r must be a number, got "
            f"{type(r).__name__}."
        )
    if r < 0:
        raise FormulaError(f"cells_in_burst(...): r must be >= 0, got {r}.")
    ri = int(math.floor(r))
    out = []
    for cx in range(cx0 - ri, cx0 + ri + 1):
        for cy in range(cy0 - ri, cy0 + ri + 1):
            # Reuse _distance for the metric so burst shape exactly
            # matches what distance()/entities_within consider "within r".
            if _distance(cx0, cy0, cx, cy, mode) <= r:
                out.append((cx, cy))
    return out


def _cells_in_line(x1: Any, y1: Any, x2: Any, y2: Any) -> list:
    """cells_in_line(x1, y1, x2, y2): the cells on the straight line from
    (x1,y1) to (x2,y2) inclusive, via Bresenham's algorithm. Returns a
    list of (x, y) tuples ordered from start to end. Useful as the basis
    of a line-of-sight or beam-attack check."""
    ax = _coord_int(x1, "cells_in_line", "x1")
    ay = _coord_int(y1, "cells_in_line", "y1")
    bx = _coord_int(x2, "cells_in_line", "x2")
    by = _coord_int(y2, "cells_in_line", "y2")
    dx = abs(bx - ax)
    dy = abs(by - ay)
    sx = 1 if ax < bx else -1
    sy = 1 if ay < by else -1
    err = dx - dy
    out = []
    cx, cy = ax, ay
    while True:
        out.append((cx, cy))
        if cx == bx and cy == by:
            break
        e2 = 2 * err
        if e2 > -dy:
            err -= dy
            cx += sx
        if e2 < dx:
            err += dx
            cy += sy
    return out


# Compass angle (0=up, clockwise) of each named direction, for cone
# orientation. Matches angle()/direction_to conventions.
_DIRECTION_ANGLES: Dict[str, float] = {
    "up": 0.0, "up_right": 45.0, "right": 90.0, "down_right": 135.0,
    "down": 180.0, "down_left": 225.0, "left": 270.0, "up_left": 315.0,
}


def _cells_in_cone(x: Any, y: Any, direction: Any, length: Any,
                   half_angle: Any = 45) -> list:
    """cells_in_cone(x, y, direction, length, half_angle=45): the cells
    inside a cone emanating from (x, y).

    direction   a named direction ("up", "up_right", ...) OR a compass
                angle in degrees (0=up, clockwise) giving the cone's
                centerline.
    length      the cone's reach in cells (euclidean). The origin (x,y)
                itself is NOT included.
    half_angle  half the cone's angular width in degrees (default 45, so
                a 90°-wide cone). A cell is in the cone if its angular
                offset from the centerline is <= half_angle.

    Returns a list of (cx, cy) tuples sorted in (x, y) order. Reuses
    angle()/distance() internally so the cone respects the same compass
    conventions as the rest of the engine."""
    cx0 = _coord_int(x, "cells_in_cone", "x")
    cy0 = _coord_int(y, "cells_in_cone", "y")
    if isinstance(direction, str):
        if direction not in _DIRECTION_ANGLES:
            allowed = ", ".join(sorted(_DIRECTION_ANGLES))
            raise FormulaError(
                f"cells_in_cone(...): direction '{direction}' must be a "
                f"named direction ({allowed}) or a number."
            )
        center = _DIRECTION_ANGLES[direction]
    elif isinstance(direction, (int, float)) and not isinstance(direction, bool):
        center = float(direction) % 360.0
    else:
        raise FormulaError(
            f"cells_in_cone(...): direction must be a named direction or "
            f"a number, got {type(direction).__name__}."
        )
    if isinstance(length, bool) or not isinstance(length, (int, float)):
        raise FormulaError(
            f"cells_in_cone(...): length must be a number, got "
            f"{type(length).__name__}."
        )
    if isinstance(half_angle, bool) or not isinstance(half_angle, (int, float)):
        raise FormulaError(
            f"cells_in_cone(...): half_angle must be a number, got "
            f"{type(half_angle).__name__}."
        )
    li = int(math.floor(length))
    out = []
    for cx in range(cx0 - li, cx0 + li + 1):
        for cy in range(cy0 - li, cy0 + li + 1):
            if cx == cx0 and cy == cy0:
                continue  # exclude the origin
            if _distance(cx0, cy0, cx, cy, "euclidean_distance") > length:
                continue
            a = _angle(cx0, cy0, cx, cy)  # compass degrees, 0=up cw
            diff = abs(a - center) % 360.0
            if diff > 180.0:
                diff = 360.0 - diff
            if diff <= half_angle:
                out.append((cx, cy))
    return out


def _cells_in_rect(x1: Any, y1: Any, x2: Any, y2: Any) -> list:
    """cells_in_rect(x1, y1, x2, y2): every cell in the axis-aligned
    rectangle whose opposite corners are (x1,y1) and (x2,y2), inclusive.
    Corner order doesn't matter (the bounds are normalized), so
    cells_in_rect(4,4,2,2) == cells_in_rect(2,2,4,4). Returns a list of
    (cx, cy) tuples sorted in (x, y) order — the rectangular twin of
    cells_in_burst's filled-area shape."""
    ax = _coord_int(x1, "cells_in_rect", "x1")
    ay = _coord_int(y1, "cells_in_rect", "y1")
    bx = _coord_int(x2, "cells_in_rect", "x2")
    by = _coord_int(y2, "cells_in_rect", "y2")
    lo_x, hi_x = (ax, bx) if ax <= bx else (bx, ax)
    lo_y, hi_y = (ay, by) if ay <= by else (by, ay)
    out = []
    for cx in range(lo_x, hi_x + 1):
        for cy in range(lo_y, hi_y + 1):
            out.append((cx, cy))
    return out


# ---- directional / facing-relative geometry -------------------------------
# Sides are named in the ENTITY's own frame (0deg = the way it faces =
# front; 90 = its right; 180 = back; 270 = its left). NOTE the lateral
# names are left_side / right_side, NOT left / right — bare left/right mean
# absolute map directions everywhere else in the engine, and these are
# facing-RELATIVE, so the distinct names avoid that collision.
_SIDE_CARDINALS: Tuple[Tuple[float, str], ...] = (
    (0.0, "front"), (90.0, "right_side"), (180.0, "back"), (270.0, "left_side"),
)
_SIDE_CORNERS: Tuple[Tuple[float, str], ...] = (
    (45.0, "front_right_side"), (135.0, "back_right_side"),
    (225.0, "back_left_side"), (315.0, "front_left_side"),
)


def _facing_degrees(facing: Any, fname: str) -> float:
    """Resolve a facing argument — a named direction ('up'/'down'/... or a
    diagonal) or a compass-degree number — to degrees (0=up, clockwise)."""
    if isinstance(facing, str):
        key = facing.strip().lower()
        if key not in _DIRECTION_ANGLES:
            allowed = ", ".join(sorted(_DIRECTION_ANGLES))
            raise FormulaError(
                f"{fname}(...): facing '{facing}' must be a named direction "
                f"({allowed}) or a number.")
        return _DIRECTION_ANGLES[key]
    if isinstance(facing, (int, float)) and not isinstance(facing, bool):
        return float(facing) % 360.0
    raise FormulaError(
        f"{fname}(...): facing must be a named direction or a number, got "
        f"{type(facing).__name__}.")


def _relative_angle(facing: Any, abs_angle: Any, signed: Any = False) -> float:
    """relative_angle(facing, abs_angle, signed=False): the absolute compass
    bearing abs_angle re-expressed in the frame of an entity facing
    `facing` — i.e. (abs_angle - facing) normalized. 0 = straight ahead
    (front), 90 = the entity's right, 180 = behind, 270 = its left.
    signed=False -> [0,360); True -> (-180,180]. `facing` is a named
    direction or compass degrees; `abs_angle` is compass degrees (get one
    from angle(...))."""
    fdeg = _facing_degrees(facing, "relative_angle")
    if isinstance(abs_angle, bool) or not isinstance(abs_angle, (int, float)):
        raise FormulaError(
            f"relative_angle(...): abs_angle must be a number, got "
            f"{type(abs_angle).__name__}.")
    if not isinstance(signed, bool):
        raise FormulaError(
            f"relative_angle(..., signed): must be bool, got "
            f"{type(signed).__name__}.")
    rel = (float(abs_angle) - fdeg) % 360.0
    if signed and rel > 180.0:
        rel -= 360.0
    return rel


def _ang_diff(a: float, b: float) -> float:
    """Smallest absolute angular gap between two compass bearings (deg)."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _coerce_corner_arc(arc: Any) -> float:
    """Validate + clamp a corner_arc (degrees) to [0, 90]. Clamps rather
    than raising on out-of-range so a stray rule value can't crash a
    combat formula; a non-number is still a hard error."""
    if isinstance(arc, bool) or not isinstance(arc, (int, float)):
        raise FormulaError(
            f"corner_arc must be a number, got {type(arc).__name__}.")
    return max(0.0, min(float(arc), 90.0))


def _relative_side_name(rel: float, sides: int, corner_arc: float) -> str:
    """Bucket a facing-relative bearing `rel` (deg, 0=front) into a side
    name. sides=4: four 90deg faces. sides=8: cardinal faces span
    (90 - corner_arc) and each diagonal corner spans corner_arc, centered
    on the 45deg lines — so corner_arc=0 collapses to 4-way and
    corner_arc=45 gives equal octants."""
    rel %= 360.0
    if sides == 4:
        # Each cardinal face owns a full 90deg quadrant; ties at the 45deg
        # boundary fall to the cardinal checked first (front, then cw).
        for c, name in _SIDE_CARDINALS:
            if _ang_diff(rel, c) <= 45.0 + 1e-9:
                return name
        return "front"  # unreachable; quadrants tile the circle
    face_half = (90.0 - corner_arc) / 2.0
    for c, name in _SIDE_CARDINALS:
        if _ang_diff(rel, c) <= face_half + 1e-9:
            return name
    # Not within any cardinal face -> it's a corner; pick the nearest.
    return min(_SIDE_CORNERS, key=lambda kc: _ang_diff(rel, kc[0]))[1]


# ---- coordinate extractors ------------------------------------------------
# A coordinate is the 2-element (x, y) pair that cells_in_* / first_opaque /
# etc. produce (or an action-mode Coord with .x/.y). The sandbox bans
# subscript and attribute-on-call-result, so these are the read convention
# for a returned coord: coord_x(first_opaque(...)), coord_y(c).
def _coord_x(c: Any) -> Any:
    """coord_x(c): the x component of a coordinate (an (x, y) pair or a
    Coord). Raises on None / a non-coordinate (check `c == None` first when
    a function may return 'no coordinate')."""
    if isinstance(c, (list, tuple)):
        if len(c) == 2:
            return c[0]
    elif hasattr(c, "x"):
        return c.x
    raise FormulaError(
        f"coord_x(...): expected an (x, y) coordinate, got "
        f"{'None' if c is None else type(c).__name__}.")


def _coord_y(c: Any) -> Any:
    """coord_y(c): the y component of a coordinate (an (x, y) pair or a
    Coord). Raises on None / a non-coordinate."""
    if isinstance(c, (list, tuple)):
        if len(c) == 2:
            return c[1]
    elif hasattr(c, "y"):
        return c.y
    raise FormulaError(
        f"coord_y(...): expected an (x, y) coordinate, got "
        f"{'None' if c is None else type(c).__name__}.")


_ALLOWED_FUNCS: Dict[str, Any] = {
    "min": min, "max": max, "abs": abs, "round": round,
    "int": int, "float": float, "str": str,
    "len": _len,
    "random_int": _random_int,
    "random_string": _random_string,
    "roll": _roll,
    "distance": _distance,
    "angle": _angle,
    "direction_to": _direction_to,
    "sqrt": _sqrt,
    "floor": _floor,
    "ceil": _ceil,
    "pow": _pow,
    "clamp": _clamp,
    "sign": _sign,
    "cells_in_burst": _cells_in_burst,
    "cells_in_line": _cells_in_line,
    "cells_in_cone": _cells_in_cone,
    "cells_in_rect": _cells_in_rect,
    "relative_angle": _relative_angle,
    "coord_x": _coord_x,
    "coord_y": _coord_y,
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
    #   tile_set(5, 5, "flame.burn_damage", v) -> v   write at dotted path
    #   tile_del(5, 5, "flame.burn_damage")   -> bool (True iff present)
    #   tile_clear(5, 5)                      -> bool (True iff the tile
    #                                                  had any data to drop)
    # The mutating trio (set/del/clear) is meant for use INSIDE tile
    # hooks — a landmine's on_enter can deal damage and call
    # tile_clear() to self-destruct in one formula.
    "tile_get", "tile_has", "tile_set", "tile_del", "tile_clear",
    # Move an entity from a formula. The geometry checks (bounds,
    # occupancy) and side-effect firing (tile on_exit/on_enter/on_stop,
    # on_entity_moved) are exactly the !ent tp command's, so
    # move_entity and !ent tp are semantically interchangeable.
    # Returns the new (x, y) as a tuple. Raises FormulaError on any
    # validation failure (off-grid, occupied) so a formula can guard
    # with try-equivalent patterns (a status, a flag var, etc.).
    "move_entity",
    # Single-step move in a named direction; returns True if the step
    # succeeded (within bounds, destination free), False if blocked.
    # Honors allow_diagonal_movement just like !ent move.
    "move_step",
    # Forced movement: push an entity up to n cells in a direction,
    # stopping at the first blocked cell. Returns the number of cells
    # actually moved (0..n). Honors allow_diagonal_movement.
    "push_entity",
    # Forced movement TOWARD a point: drag an entity up to n cells toward
    # (x, y), stopping at the first blocked cell or on reaching the
    # target. The point-directed twin of push_entity; returns cells moved.
    "pull_entity",
    # Atomically exchange two entities' positions. Bypasses the usual
    # occupancy check (the two cells are occupied — by each other).
    # Fires on_exit/on_enter/on_stop/on_entity_moved for both. Returns
    # True on a real swap, False on the same-entity no-op.
    "swap_entities",
    # Programmatically fire a tile hook (for chained effects, tests,
    # or "trigger this trap from a script" patterns). The bound entity
    # for `self` defaults to ctx.target (whoever is the caller's
    # self); pass an explicit eid string to bind a different entity.
    "fire_tile_hook",
    # Team relationship predicates. Each takes entity ids (strings) and
    # consults the team_var field on each entity's vars. Teamless
    # entities (var absent / empty string) are treated as singletons:
    # a teamless entity shares a team with NOBODY, so is_same_team
    # against a teamless entity is always False and is_hostile is
    # always True. The four predicates split deliberately:
    #   is_same_team(a, b)        — both have the same non-empty team
    #   is_hostile(a, b)          — NOT same team (the "strict" check;
    #                               unaffected by the friendlyfire rule)
    #   is_part_of_team(a, team)  — entity a's team equals `team` literal
    #   is_attackable(a, b)       — the "can A attack B" check. Equals
    #                               is_hostile when friendlyfire is off
    #                               (the default); always True for
    #                               distinct pairs when friendlyfire
    #                               is on. is_attackable(a, a) is always
    #                               False — you can't target yourself.
    # The split matters because changing the friendlyfire rule must
    # NOT silently flip is_hostile-keyed formulas (which often gate
    # heal/buff decisions on team identity, not target legality).
    "is_same_team", "is_hostile", "is_part_of_team", "is_attackable",
    # Spatial-query helpers. Both scan match.entities relative to a
    # reference entity, exclude the reference itself, consider only
    # ALIVE entities (matching occupancy/turn-order conventions), and
    # accept an optional `relation` filter ("hostile" / "ally" /
    # "same_team" / "attackable" / "" for no filter):
    #   entities_within(eid, n, mode, relation)  -> list of ids in range,
    #                                               sorted by (distance, id)
    #   nearest_entity(eid, relation, mode)      -> single closest id, or
    #                                               "" if none match
    "entities_within", "nearest_entity",
    # Iterable companion to group_has / group_size: the list of member
    # ids for a named group, in insertion order. Returns [] when the
    # group doesn't exist (consistent with group_size returning 0). The
    # main consumer is the new for-loop:
    #     for m in group_members("party"):
    #         entity[m].hp = entity[m].hp + 5
    "group_members",
    # Status-data accessors. Statuses are entity-owned dicts:
    # entity.status[name] = {field: value, ...}. The status_* trio
    # mirrors tile_* — same dotted-path semantics, same raise-on-
    # missing for status_get, same bool return for status_has /
    # status_remove.
    #   status_has(eid, name)               -> bool (status present)
    #   status_has_path(eid, name, "path")  -> bool (status present AND
    #                                          path resolves; False on
    #                                          either layer missing)
    #   status_get(eid, name, "path")       -> value (raises on missing)
    #   status_set(eid, name, "path", v)    -> v   (creates intermediates)
    #   status_del(eid, name, "path")       -> bool (True iff existed)
    #   status_add(eid, name)               -> bool (True iff newly added)
    #   status_remove(eid, name)            -> bool (True iff removed)
    "status_has", "status_has_path", "status_get",
    "status_set", "status_del", "status_add", "status_remove",
    # status_apply(eid, name[, level, duration]) -> apply a status via its
    # definition + stack mode (the host-friendly "inflict burn" call).
    "status_apply",
    # Status-name introspection: status_names(eid) -> list of the active
    # status names on an entity (insertion order). The companion that
    # lets a formula iterate WHATEVER statuses exist instead of checking
    # a hardcoded list (e.g. "purge every debuff", "tick all durations").
    "status_names",
    # Var-path introspection + runtime-path accessors. entity[X].path
    # needs a STATIC path; these take a runtime string path, mirroring
    # tile_*/status_* exactly, so a formula can read/write a var whose
    # name it computed (the thing that makes var_keys iteration useful):
    #   var_keys(eid, path="")        -> list of keys at that vars path
    #                                    ("" = top-level var names)
    #   var_has(eid, "path")          -> bool
    #   var_get(eid, "path")          -> value (raises on missing)
    #   var_set(eid, "path", value)   -> value (routes through write_var
    #                                    so var hooks fire, same as
    #                                    entity[eid].path = value)
    #   var_del(eid, "path")          -> bool (True iff existed; routes
    #                                    through remove_var)
    "var_keys", "var_has", "var_get", "var_set", "var_del",
    # Match-level var accessors: runtime-path twins of the reserved
    # `match.<path>` formula root (which itself mirrors entity[X].path).
    # All read/write the single match-wide vars dict — global GM state
    # like alarm_level / objective_progress / weather — with no entity id
    # and no var hooks. match_var_get raises on a missing path; the
    # others mirror their var_* counterparts.
    #   match_var_keys(path="")     -> list of keys at that match-vars path
    #   match_var_has(path)         -> bool
    #   match_var_get(path)         -> value (raises on missing)
    #   match_var_set(path, value)  -> value
    #   match_var_del(path)         -> bool (True iff existed)
    "match_var_keys", "match_var_has", "match_var_get",
    "match_var_set", "match_var_del",
    # Container-shape introspection / convenience over the SAME var
    # path machinery. None of these encode an "inventory" concept —
    # they're generic operations on dicts living under entity.vars.
    #   var_has_key(eid, path, key)  -> bool (path is a dict that has
    #                                   `key`); tolerant of shape
    #   var_count(eid, path="")      -> int (immediate-child count)
    #   var_sum(eid, path="")        -> number (sum of numeric children;
    #                                   non-numeric skipped silently)
    #   var_max_key(eid, path="")    -> str|None (key of largest child;
    #                                   ties: insertion order)
    #   var_min_key(eid, path="")    -> str|None
    #   var_pick_random(eid, path="") -> str|None (honors random_seed)
    #   var_clear(eid, path="")      -> int (children removed; fires
    #                                   on_var_removed per child)
    "var_has_key", "var_count", "var_sum",
    "var_max_key", "var_min_key", "var_pick_random",
    "var_clear",
    # Tile-key introspection: tile_keys(x, y) -> top-level keys in the
    # tile's data dict (symmetric companion to tile_get/tile_has).
    "tile_keys",
    # Reverse group index: entity_groups(eid) -> names of every group
    # containing the entity (insertion order over Match.groups).
    "entity_groups",
    # Action introspection / invocation primitives. Discovery walks the
    # entity's vars tree (subject to action_container_* rules); the
    # results power both these primitives and the !action command.
    #   has_action(eid, name)         -> bool
    #   entity_actions(eid)           -> list of action names (loopable)
    #   use_action(eid, name, target=None, args=None)
    #                                 -> True on success, False on a
    #                                    clean fail(). Counts toward
    #                                    action_recursion_limit.
    "has_action", "entity_actions", "use_action",
    # Summon / despawn family. A template is an entity-shaped dict in
    # ordinary storage (vars / tile data); these turn one into a live
    # entity and back. All route through Match.summon_entity (shared
    # summon_event_limit guard + turn-order/on_entity_spawned wiring).
    #   entity_snapshot(eid)                  -> template dict (id/pos stripped)
    #   summon(template, x, y, id_prefix=None) -> new id (strict placement)
    #   summon_near(template, x, y, radius, id_prefix=None) -> new id (ring search)
    #   summon_from(path, x, y, id_prefix=None) -> new id (template from self's vars)
    #   remove_entity(eid)                    -> True (despawn)
    "entity_snapshot", "summon", "summon_near", "summon_from",
    "remove_entity",
    # Death / corpse family. kill / revive route through the same
    # death pipeline as the natural-death chokepoint; the introspection
    # trio (has_corpse, corpse_at, all_corpses) reads tile-data
    # corpse entries with the tile location as the authoritative
    # position (never the embedded snapshot's x/y).
    #   kill(eid)           -> bool (death triggered)
    #   revive(eid)         -> str (new entity id; raises if no corpse)
    #   has_corpse(eid)     -> bool
    #   corpse_at(eid)      -> (x, y); raises if no such corpse
    #   all_corpses()       -> list of corpse ids (loopable)
    #   corpse_has(eid, path)        -> bool (dotted vars path resolves
    #                                   on the dead entity's snapshot)
    #   corpse_var(eid, path[, def]) -> the dead entity's stored var at a
    #                                   dotted path (raises if missing
    #                                   unless a default is supplied)
    "kill", "revive", "has_corpse", "corpse_at", "all_corpses",
    "corpse_has", "corpse_var",
    # Scheduled / delayed effects. schedule() queues a MATCH-level body
    # to run at on_round_start `delay` rounds out (no self bound);
    # schedule_on() queues an ENTITY-attached body to run at that
    # entity's on_turn_start after `delay` of its turns (self=eid,
    # dropped if it dies first); both return a name. cancel_schedule()
    # removes pending schedules by name.
    #   schedule(delay, body, name=None)            -> name (rounds)
    #   schedule_on(eid, delay, body, name=None)    -> name (turns)
    #   cancel_schedule(name)                       -> count removed
    "schedule", "schedule_on", "cancel_schedule",
    # Append a custom entry to the match's structured event log (the
    # combat log). log(message) stringifies its arg; recorded when
    # event_log_enabled (the `custom` type bypasses the event_log_events
    # filter). Returns True if recorded. View with !log.
    "log",
    # Zone primitives. A zone is a named region (a set of cells) with
    # free-form data and hooks; these mirror the tile_* accessors plus
    # membership queries and footprint mutation.
    #   Queries: zone_exists(name), zones_at(x,y), cell_in_zone(x,y,name),
    #     in_zone(eid,name), entity_zones(eid), entities_in_zone(name),
    #     zone_cells(name), zone_names(), zone_size(name)
    #   Data:    zone_get/has/keys/set/del (dotted paths under the zone's
    #            data dict — same semantics as tile_*)
    #   Shape:   create_zone(name), delete_zone(name),
    #            zone_add_cell(name,x,y), zone_remove_cell(name,x,y),
    #            zone_fill(name,x1,y1,x2,y2), zone_shift(name,dx,dy)
    "zone_exists", "zones_at", "cell_in_zone", "in_zone", "entity_zones",
    "entities_in_zone", "zone_cells", "zone_names", "zone_size",
    "zone_get", "zone_has", "zone_keys", "zone_set", "zone_del",
    "create_zone", "delete_zone", "zone_add_cell", "zone_remove_cell",
    "zone_fill", "zone_shift",
    # Entity-anchored auras: bind a zone to an entity so its cells track
    # the entity's footprint (re-stamped on move). radius 0 = the
    # footprint itself.
    #   zone_anchor(name, eid, radius=0, metric="square_radius") -> cells
    #   zone_unanchor(name)   -> bool (was it anchored)
    #   zone_anchor_of(name)  -> the anchor eid, or '' if not an aura
    "zone_anchor", "zone_unanchor", "zone_anchor_of",
    # Read a game-system rule value from inside a formula. rule_get(name)
    # -> the rule's effective value, or None if unknown.
    "rule_get",
    # Match-clock primitives (no args). round_number() -> current round
    # (1-based); turn_index() -> 0-based position of the acting entity
    # in the round's turn order. For cadence checks ('every N rounds',
    # 'first turn of the round') and round-measured cooldowns.
    "round_number", "turn_index",
    # Match-wide entity queries (no reference entity; all loopable). Each
    # returns a list of ALIVE entity ids:
    #   all_entities()                -> every alive entity, insertion order
    #   entities_with_status(name)    -> those carrying status `name`
    #   entities_with_var("path")     -> those for which var_has is True
    #   entities_in_area(x, y, n, mode) -> those within distance n of the
    #                                    POINT (x, y) — coord-rooted twin
    #                                    of entities_within
    "all_entities", "entities_with_status", "entities_with_var",
    "entities_in_area",
    # Body parts (locational damage)
    "parts", "part", "has_part", "part_of", "damage_part", "damage_spread",
    # Stat modifiers (derived / effective stats)
    "apply_mods", "list_mods",
    # Shape-rooted entity queries — the entity-returning twins of the
    # cells_in_* area helpers (entities_in_area already covers burst/
    # radius). Each returns ALIVE entity ids standing on the shape's cells:
    #   entities_in_cone(x,y,dir,len[,half_angle]) -> within a cone
    #   entities_in_rect(x1,y1,x2,y2)  -> within an axis-aligned rectangle
    # All loopable. Filter by team/relation inside the loop.
    "entities_in_cone", "entities_in_rect",
    # Line / beam entity queries (the LOS pair). Both ordered near->far:
    #   entities_in_line_ignorelos(x1,y1,x2,y2) -> every entity on the
    #       geometric line INCLUDING the endpoints, walls ignored.
    #   entities_on_los(x1,y1,x2,y2,viewer=None) -> entities STRICTLY
    #       BETWEEN the points that the viewer has line of sight to (cut at
    #       the first opaque cell; shooter + target excluded) — the bodies a
    #       projectile would meet. Compose the block rule in the loop.
    #   first_opaque(x1,y1,x2,y2,viewer=None) -> the first opaque cell
    #       strictly between as an (x,y) pair (read with coord_x/coord_y), or
    #       None if clear. NOT loopable (single coord).
    "entities_in_line_ignorelos", "entities_on_los",
    "entities_in_line_until", "first_opaque",
    # clip_cells(cells) -> the subset of an (x,y) cell list that is on the
    # grid. The cells_in_* helpers can return off-grid cells (they're pure
    # geometry); wrap one to keep only valid cells, e.g.
    # `clip_cells(cells_in_burst(x, y, 3))`. Loopable (yields (x,y)).
    "clip_cells",
    # Vision primitives. All return bool and ignore the fog_enabled /
    # fog_los toggles — raw "is it in sight?" queries a GM composes with.
    # The bare names mean range AND line-of-sight; _rangeonly / _losonly
    # isolate each concept. A falsy/empty team is omniscient (sees all).
    #   team_sees_cell(team, x, y)        -> any alive member in range AND
    #                                        with a clear line to (x, y)
    #   team_sees_cell_rangeonly(...)     -> range only (any member in radius)
    #   team_sees_cell_losonly(...)       -> clear line only (ignores range)
    #   team_sees_entity(team, eid)[ _rangeonly | _losonly ] -> same of eid's cell
    #   can_see(eid, x, y)[ _rangeonly | _losonly ] -> single unit eid
    #   has_los(x1, y1, x2, y2, viewer=None) -> clear line between two cells
    #       (pass a viewer for viewer-conditional opacity; without one such
    #       conditions read transparent). LOS is blocked by OPAQUE cells
    #       (tile_opaque_condition / zone_opaque_condition + per-cell
    #       `opaque` overrides), judged with the los_corner_mode rule.
    "team_sees_cell", "team_sees_cell_rangeonly", "team_sees_cell_losonly",
    "team_sees_entity", "team_sees_entity_rangeonly", "team_sees_entity_losonly",
    "can_see", "can_see_rangeonly", "can_see_losonly", "has_los",
    # Directional / facing-relative primitives. facing_of bridges the
    # entity facing attribute (otherwise unreadable from formulas) into a
    # formula value; relative_side buckets an absolute bearing into a side
    # of an entity facing some direction; side_hit is the headline combat
    # primitive (which side of a target a hit from a point lands on);
    # directional_get reads a per-side value table in one call. The PURE
    # relative_angle (no match needed) lives in _ALLOWED_FUNCS.
    #   facing_of(eid)                                   -> direction name
    #   relative_side(facing, abs_angle, sides=4, corner_arc=None) -> side
    #   side_hit(target, from_x, from_y, sides=4, corner_arc=None) -> side
    #   directional_get(eid, base_path, from_x, from_y,
    #                   default=None, sides=4, corner_arc=None)    -> value
    # Sides: front / back / left_side / right_side (+ the four *_side
    # corners when sides=8). corner_arc defaults to the
    # directional_corner_arc rule.
    "facing_of", "relative_side", "side_hit", "directional_get",
    "hit_location",
    # Footprint / large-entity primitives. A large entity occupies a W×H
    # rectangle anchored at its top-left cell (entity[X].x / .y); these
    # expose that footprint to formulas.
    #   footprint_width(eid) / footprint_height(eid) -> the W / H (>=1)
    #   footprint_cells(eid)        -> loopable list of (x,y) cells it
    #                                  covers (read each via coord_x/coord_y)
    #   occupies(eid, x, y)         -> bool: does eid's footprint cover (x,y)
    #   cell_entity(x, y)           -> id of the blocking entity covering
    #                                  (x,y), or "" if none
    #   entity_center(eid)          -> the footprint's center cell (x,y),
    #                                  rounding down for even sizes
    #   aoe_origin(eid)             -> the cell an area effect cast BY eid
    #                                  originates from: its center or anchor
    #                                  per the aoe_origin_mode rule
    "footprint_width", "footprint_height", "footprint_cells",
    "occupies", "cell_entity", "entity_center", "aoe_origin",
)

_ALLOWED_NODES: Tuple[type, ...] = (
    ast.Module, ast.Expression,
    ast.Expr, ast.Assign,
    # Control flow:
    #   If         -> if / elif / else statements (elif is encoded as else: If)
    #   Pass       -> empty bodies, e.g. `if cond: pass`
    #   IfExp      -> ternary  (value_a if cond else value_b)
    #   For        -> constrained for-loop (see _LOOPABLE_FUNCS and
    #                  _validate_for). The loop target may be a single
    #                  Name (entity id / scalar) or a Tuple of Names
    #                  (coord unpacking, e.g. `for (cx, cy) in
    #                  cells_in_burst(...)`).
    ast.If, ast.Pass,
    ast.For, ast.Tuple,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.IfExp, ast.Compare,
    ast.Call, ast.keyword,
    # Attribute: outside action_mode the transformer rewrites every
    # entity[X].path Attribute into a __read/__write Call, so no
    # Attribute node survives to be validated. In action_mode the
    # transformer deliberately LEAVES source.<path>/args.<key>/
    # target.<x> Attribute chains in place for the SourceProxy/
    # ArgsProxy/raw-value runtime to handle, so the node type must
    # be in the allowed list for the validator's _ALLOWED_NODES
    # check to pass. Misuse outside action_mode is still rejected
    # at the transformer layer with a specific error.
    ast.Attribute,
    # Container literals — Dict and List. Needed for action bodies
    # that call use_action(..., args={'power': 3}) and for any
    # formula that wants to build small constant data on the fly.
    # The _validate_for loop iterable check still restricts loop
    # iterators to _LOOPABLE_FUNCS calls (or `target` in action
    # mode), so allowing List literals here doesn't let a GM
    # iterate `[1, 2, 3]` as a sneak path around the loop limit.
    ast.Dict, ast.List,
    ast.Name, ast.Constant, ast.Load, ast.Store,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    # Membership tests. Useful as guards: `if "crit" in args:` for
    # action-arg presence, `if status_name in entity_status_names:`
    # in passives, etc. Read-only (no Python sandbox escape).
    ast.In, ast.NotIn,
    ast.And, ast.Or,
)


# Functions whose call result is a known list/tuple suitable for a
# formula for-loop. Keeping this explicit (rather than allowing any
# call) is a sandbox safety measure: a user-defined function could
# silently return a non-iterable and a `for` over it would crash at
# runtime; gating to known list-returning builtins keeps the validation
# pinned to "this loop can be iterated cheaply".
#
# All entries here MUST be functions defined elsewhere in this module
# (either in _ALLOWED_FUNCS or _MATCH_FUNC_NAMES). They return either
# string ids (entities_within / group_members) or coordinate tuples
# (cells_in_*). The for-loop target shape must match: a single Name for
# the id case, a 2-tuple of Names for the coord case.
_LOOPABLE_FUNCS: "frozenset[str]" = frozenset({
    "entities_within",
    "group_members",
    "cells_in_burst",
    "cells_in_line",
    "cells_in_cone",
    "cells_in_rect",
    "entities_in_line_ignorelos",
    "entities_on_los",
    "entities_in_line_until",
    "entities_in_cone",
    "entities_in_rect",
    "clip_cells",
    # Introspection / query helpers (PR: introspection primitives). All
    # return lists, so all are loopable.
    "status_names",
    "var_keys",
    "tile_keys",
    "entity_groups",
    "all_entities",
    "parts",
    "entities_with_status",
    "entities_with_var",
    "entities_in_area",
    # Action introspection — returns a list of action names, loopable.
    "entity_actions",
    # Corpse introspection — returns a list of corpse ids, loopable.
    "all_corpses",
    # Zone queries — all return lists. zones_at / entity_zones /
    # zone_names yield zone-name strings; entities_in_zone yields entity
    # ids; zone_cells yields [x,y] pairs (loop with `for (cx,cy) in ...`).
    "zones_at",
    "entity_zones",
    "zone_names",
    "entities_in_zone",
    "zone_cells",
    "zone_keys",
    # Footprint cells — a list of (x,y) pairs (loop with `for (cx,cy) in
    # footprint_cells(eid)`).
    "footprint_cells",
})


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
    "tile_x",           # The firing tile's x coordinate. Bound during
                        # tile-hook evaluation (on_enter / on_exit /
                        # on_stop); None elsewhere. Redundant with
                        # entity[self].x in on_enter / on_stop (entity
                        # is AT the tile) but distinct in on_exit
                        # (entity is still at the OLD coords during
                        # the hook — tile_x and entity[self].x happen
                        # to agree there too with the current "fire
                        # before move" timing, but the distinction
                        # matters if the hook moves the entity
                        # mid-fire via a future entity-tp formula).
    "tile_y",           # See tile_x.
    "status_name",      # The status being ticked. Bound only during
                        # status_tick_formula evaluation (see
                        # status_tick_when / status_tick_formula rules
                        # and Match.fire_status_tick); None elsewhere.
    # Movement-event bindings. Bound only during on_entity_moved
    # firing (see Match.fire_entity_moved); None elsewhere. from_*
    # are the position the entity moved FROM, to_* are where it
    # ended up. For a stepwise move from (3,3) to (7,5), the hook
    # fires ONCE with from=(3,3), to=(7,5) — per-step observation is
    # via tile on_enter/on_exit.
    "from_x", "from_y", "to_x", "to_y",
    # Action-event bindings. Bound only during on_action_used firing
    # (see Match.fire_action_used); None elsewhere. action_name is
    # the bare action name; action_path is the full vars location
    # the action was loaded from.
    "action_name", "action_path",
    # `target` and `args` overlap with the action-mode body bindings,
    # but inside a passive (where action_mode is NOT enabled) they're
    # populated from EvalCtx.extras for the on_action_used firing —
    # so a passive can read `target` and `args` without enabling the
    # action-body sandbox. For passives observing any other hook
    # they default to None / empty dict.
    "target", "args",
    # The entity that USED the action. Bound during on_action_used
    # (where it equals `self`), on_action_used_on_target (where
    # `self` is the defender and `actor` is the user), and
    # on_action_failed (same as on_action_used). None elsewhere.
    # Lets target-side reactive passives reference the attacker
    # naturally: `entity[actor].team`, `is_hostile(actor, self)`,
    # `cmd("ent hp " + actor + " -3")` for retaliation.
    "actor",
    # Failure event bindings. Bound only during on_action_failed
    # firing. fail_reason is the GM-supplied tag from
    # fail(reason, msg) — "" for the single-arg fail(msg) form.
    # fail_message is the human-readable explanation. Together they
    # let reactive logic discriminate: "if fail_reason == 'cost'
    # and entity[self].mana < 5: status_add(self, 'exhausted')".
    "fail_reason", "fail_message",
    # Zone-hook binding. Bound during zone-hook evaluation (boundary
    # on_enter/on_exit/on_stop, per-cell on_cell_enter/on_cell_exit/
    # on_cell_stop, and the time hooks) to the firing zone's name; None
    # elsewhere. tile_x/tile_y carry the crossed/stepped cell for the
    # movement hooks (the entity's own .x/.y may differ for on_exit /
    # on_cell_exit, which fire pre-move). Lets a single shared hook
    # formula reference its zone generically: `zone_get(zone_name,
    # "gas.dmg")`, `zone_shift(zone_name, 1, 0)`.
    "zone_name",
    # Visibility binding. Bound during entity_visibility_condition
    # evaluation (Match.entity_visible_to) to the requesting channel's
    # POV team — the string team name a player channel renders from.
    # None when the view is omniscient (a host channel), though the
    # omniscient case short-circuits before the formula runs, so a
    # visibility formula in practice always sees a concrete team string.
    # `self` is the entity whose visibility is being tested. Lets a GM
    # write "hidden unless an ally sees it": `not status_has(self,
    # "invisible") or is_part_of_team(self, pov_team)`.
    "pov_team",
    # Bound during corpse_visibility_condition evaluation to the dead
    # entity's team value at death (the team_var on its snapshot; "" if
    # none). A corpse is a stored snapshot, not a live entity, so it has
    # no `self`/entity[X] — corpse_team + tile_x/tile_y are how a
    # visibility formula keys on it. None outside corpse-visibility.
    "corpse_team",
    # Stat-modifier context. Bound when apply_mods / list_mods evaluate a
    # modifier's condition / value / tag formulas, to the related entities
    # the caller passed (each optional — unbound if not supplied). `self`
    # is always the modifier's owner. Lets a modifier read the other party:
    # "+25 vs undead" -> condition `entity[target].undead`.
    "attacker", "defender", "other",
)

# Entity-id sentinel identifiers. Inside `entity[X]` these get
# special-cased by the transformer; as bare identifiers they bind to
# the actual entity id string at namespace build time (or to None when
# unbound). Letting them appear bare lets a formula write
# `status_set(self, status_name, "remaining", 0)` instead of the more
# verbose `status_set(self_id(), ...)`.
_ENTITY_TOKEN_NAMES: Tuple[str, ...] = ("self", "this", "current", "parent")


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
    # Back-reference to the Match, set by the engine at the top of each
    # eval so resolve_who can dereference the `parent` token (which needs
    # to read the contextual entity's `part_of`). None for callers that
    # never use `parent`.
    match: Optional[Any] = None

    def resolve_who(self, token: Any) -> str:
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
        if token == "parent":
            # The parent of the contextual entity (self, falling back to
            # the current-turn entity). Used by a body part's passives /
            # actions to reach the whole creature: entity[parent].hp.
            base = self.target or self.this
            if not base:
                raise FormulaError("'parent' is unbound in this context.")
            if self.match is None:
                raise FormulaError("'parent' cannot be resolved here.")
            e = self.match.entities.get(base)
            if e is None or not e.part_of:
                raise FormulaError(
                    f"'{base}' has no parent (it is not a body part)."
                )
            return e.part_of
        # Anything else is treated as a literal entity id. For the dynamic
        # entity[<expr>] form the expression is evaluated at runtime and
        # its (already-computed) value lands here — guard against a
        # non-string so entity[5] / entity[some_dict] fail clearly rather
        # than as a downstream "entity not found".
        if not isinstance(token, str):
            raise FormulaError(
                f"entity[...] index must be an entity id string, got "
                f"{type(token).__name__}."
            )
        return token


# --- AST flattening / rewriting ---------------------------------------------

# Identifier names the action body language reserves on top of plain
# formula. The validator and transformer treat these specially when
# action_mode is True; the runtime binds them in the eval namespace.
#   source / args / target  — bindings (proxies for source/args,
#                              raw value for target)
#   cmd / fail               — callables (added to _ACTION_BUILTINS
#                              and allowed as Call targets)
_ACTION_BINDING_NAMES = frozenset({"source", "args", "target"})
# cmd/fail plus the mid-body choice prompts. choose(prompt, list) returns
# the chosen element; choose_number(prompt, lo, hi) returns the chosen int.
# Both are supplied as action_bindings by the runner (action-mode only).
_ACTION_BUILTINS = frozenset({"cmd", "fail", "choose", "choose_number"})
# Names that are loopable in action mode WITHOUT being a Call. Lets
# `for x in target:` work for *_list target types. The runtime will
# error cleanly if the loop runs against a non-iterable (e.g. for a
# target=entity action where the GM iterates `target`).
_ACTION_LOOPABLE_NAMES = frozenset({"target"})


def _collect_action_locals(tree: ast.AST) -> "frozenset[str]":
    """Pre-pass over an action body AST. Collects every bare Name
    that appears on the LHS of an Assign — those become the action's
    locals (Python `exec` binds them in the eval namespace; the
    validator needs to allow RHS references too). For-loop target
    names are handled separately by _validate_for.

    NOT a strict use-before-assign checker; treats every assigned
    name as in-scope throughout the program. Matches Python's "name
    resolution is function-wide" semantic, which is the natural
    expectation for a multi-statement body."""
    locals_set: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    locals_set.add(tgt.id)
    return frozenset(locals_set)


class _EntityAccessTransformer(ast.NodeTransformer):
    """Rewrites entity[X].path reads/writes into __read/__write helper calls.

    The index X in entity[X] may be:
      - a special token (self / this / current)   -> resolved at runtime
      - a bare identifier that is a known function parameter -> evaluated
        at runtime (the parameter's value is the entity id)
      - any other bare identifier                 -> a LITERAL entity id
        (backward compatible: entity[rogue] means the entity with id
        "rogue")
      - a string literal                          -> a literal entity id
      - any other expression (a call, an entity read, arithmetic on
        strings, ...)                             -> evaluated at runtime

    `known_params` is the set of identifiers that should be treated as
    runtime variables rather than literal ids. It is non-empty only when
    transforming a user-defined function body (the function's params);
    for top-level formulas it is empty, so bare identifiers stay literal
    and existing formulas are unaffected.
    """

    def __init__(self, known_params: "frozenset[str]" = frozenset(),
                 action_mode: bool = False):
        self.known_params = known_params
        # When True, three extra patterns are allowed and left
        # un-rewritten (the runtime namespace handles them):
        #   - bare Name = expr   (Python binds in the eval ns; the
        #     pre-pass added all such names to known_params for the
        #     validator)
        #   - source.<path>      (SourceProxy.__getattr__ at runtime)
        #   - source.<path> = v  (SourceProxy.__setattr__ at runtime)
        #   - args.<key>         (ArgsProxy.__getattr__ at runtime)
        # All four are rejected outside action_mode for safety.
        self.action_mode = action_mode

    def _who_arg(self, slice_node: ast.AST) -> ast.AST:
        """Return the AST expression to use as the `who` argument of
        __read / __write for an entity[...] index."""
        if isinstance(slice_node, ast.Index):  # py<=3.8 wrapper, defensive
            slice_node = slice_node.value
        if isinstance(slice_node, ast.Name):
            if slice_node.id in _ENTITY_TOKEN_NAMES:
                # Special token (self/this/current/parent) — pass the token
                # string; resolve_who maps it to a concrete entity id.
                return ast.Constant(value=slice_node.id)
            if slice_node.id in self.known_params:
                # Dynamic: the parameter's bound value is the entity id.
                return ast.Name(id=slice_node.id, ctx=ast.Load())
            if self.action_mode and slice_node.id in _ACTION_BINDING_NAMES:
                # In action mode, `entity[target]` resolves at runtime
                # against whatever the runner bound `target` to (the
                # eid for target=entity actions; a list for entity_list;
                # etc.). Treat it like a known_param — emit the bare
                # Name so Python's namespace lookup runs.
                return ast.Name(id=slice_node.id, ctx=ast.Load())
            if slice_node.id in HOOK_CONTEXT_NAMES:
                # Hook context bindings (actor / target / etc.) are
                # populated from EvalCtx.extras into the eval namespace.
                # Inside `entity[X]` they MUST be evaluated dynamically,
                # not frozen to the literal token name — otherwise
                # `entity[actor].hp` reads the (nonexistent) entity
                # whose id is the string "actor" instead of dereferencing
                # the binding. The validator already accepts these
                # names; this just makes the subscript path agree.
                return ast.Name(id=slice_node.id, ctx=ast.Load())
            # Bare identifier -> literal entity id (backward compatible).
            return ast.Constant(value=slice_node.id)
        if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
            return ast.Constant(value=slice_node.value)
        # Any other expression -> dynamic. Recursively transform it so it
        # may itself contain entity[...] reads / function calls, then use
        # its runtime value as the entity id.
        return self.visit(slice_node)

    def _flatten(self, node: ast.AST) -> Optional[Tuple[ast.AST, List[str]]]:
        """Recognize entity[X].a.b.c and return (who_arg_ast, ['a','b','c'])
        or None if `node` isn't such a chain."""
        parts: List[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if (isinstance(cur, ast.Subscript)
                and isinstance(cur.value, ast.Name)
                and cur.value.id == "entity"):
            who_arg = self._who_arg(cur.slice)
            parts.reverse()
            return who_arg, parts
        return None

    def _match_root_path(self, node: ast.AST) -> Optional[str]:
        """If `node` is an Attribute chain rooted at the reserved Name
        `match` (the match-level var root), return the dotted path below
        it (e.g. `match.weather.kind` -> "weather.kind"). Returns None
        for a bare `match` Name or any chain not rooted at `match`.
        Works in every formula mode — match vars are global state, not an
        action-only binding — so this is checked before the entity[...]
        and action-passthrough branches."""
        parts: List[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id == "match" and parts:
            parts.reverse()
            return ".".join(parts)
        return None

    def _action_binding_root(self, node: ast.AST) -> Optional[str]:
        """If `node` is an Attribute chain whose root is a Name in
        _ACTION_BINDING_NAMES (source/args/target), return that root
        name. Else None. Used to recognize source.<path> patterns we
        leave un-rewritten in action_mode."""
        cur = node
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        if isinstance(cur, ast.Name) and cur.id in _ACTION_BINDING_NAMES:
            return cur.id
        return None

    def _is_action_attr_passthrough(self, node: ast.AST) -> bool:
        """In action mode, ANY Attribute chain whose root is a bare
        Name is left un-rewritten so Python's runtime attribute
        resolution handles it. This covers:
          - the engine-bound proxies (source/args/target)
          - for-loop loop variables that bind to Coord proxies
            (target=location_list) or any user object exposing
            __getattr__
          - locals the GM assigned earlier in the body
        The validator's identifier check still runs on the root Name,
        so `nonexistent.x` still errors as 'Unknown identifier' at
        validation time. Runtime errors (missing attribute on a
        bound value) surface cleanly through the runner's exception
        handler."""
        if not self.action_mode:
            return False
        cur = node
        while isinstance(cur, ast.Attribute):
            cur = cur.value
        return isinstance(cur, ast.Name)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        # match.<path> read — rewrite to __read_match("path"). Checked
        # before entity[...] and the action-mode attribute passthrough so
        # the reserved `match` root resolves uniformly in every mode.
        mpath = self._match_root_path(node)
        if mpath is not None:
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="__read_match", ctx=ast.Load()),
                    args=[ast.Constant(value=mpath)],
                    keywords=[],
                ),
                node,
            )
        flat = self._flatten(node)
        if flat is None:
            # In action mode, any Attribute chain rooted at a bare
            # Name is delegated to Python's runtime attribute resolu-
            # tion: the engine bindings (source/args/target), the
            # for-loop Coord proxies, and any local the GM bound
            # earlier all surface through this same path.
            if self._is_action_attr_passthrough(node):
                return node
            raise FormulaError("Attribute access is only allowed on entity[X].path.")
        who_arg, parts = flat
        if not parts:
            raise FormulaError("entity[X] must be followed by .path.")
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="__read", ctx=ast.Load()),
                args=[who_arg, ast.Constant(value=".".join(parts))],
                keywords=[],
            ),
            node,
        )

    def visit_Assign(self, node: ast.Assign) -> ast.AST:
        # Transform RHS (may itself contain entity[X].path reads).
        node.value = self.visit(node.value)
        if len(node.targets) != 1:
            raise FormulaError("Chained / tuple assignment is not supported.")
        target = node.targets[0]
        # match.<path> = value — rewrite to __write_match("path", value).
        # Checked first so the reserved root works in every mode and isn't
        # captured by the action-mode bare-Name local rule below.
        mpath = self._match_root_path(target)
        if mpath is not None:
            call = ast.Call(
                func=ast.Name(id="__write_match", ctx=ast.Load()),
                args=[ast.Constant(value=mpath), node.value],
                keywords=[],
            )
            return ast.copy_location(ast.Expr(value=call), node)
        if isinstance(target, ast.Name) and target.id == "match":
            raise FormulaError(
                "'match' is a reserved formula root; assign to "
                "match.<path> (e.g. match.alarm = 3), not bare 'match'."
            )
        # In action_mode three extra Assign target shapes are allowed:
        #   1. bare Name = expr        -> local binding (Python's eval
        #      namespace handles it; pre-pass added the name to
        #      known_params so RHS reads validate too)
        #   2. <any-bare-name>.attr = expr -> Python setattr on the
        #      bound value. Covers source/args/target/coord loop vars
        #      uniformly.
        if self.action_mode:
            if isinstance(target, ast.Name):
                return node
            if isinstance(target, ast.Attribute) and self._is_action_attr_passthrough(target):
                return node
        flat = self._flatten(target)
        if flat is None:
            raise FormulaError("Assignment target must be entity[X].path.")
        who_arg, parts = flat
        if not parts:
            raise FormulaError("entity[X] must be followed by .path.")
        call = ast.Call(
            func=ast.Name(id="__write", ctx=ast.Load()),
            args=[who_arg,
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

    def visit_For(self, node: ast.For) -> ast.AST:
        # Treat for-loop target variables as dynamic identifiers inside
        # the body, so `entity[<loop_var>]` resolves at runtime to the
        # loop iteration's value (an entity id) instead of being
        # frozen to the literal token name. The validator separately
        # checks the iterable's shape — here we only need to extend
        # self.known_params for the body traversal and restore it after.
        # We also inject a `__loop_tick()` call as the FIRST statement
        # in the body so the runtime can enforce the formula_loop_limit
        # rule — the engine raises FormulaError once total iterations
        # across all loops in this evaluation exceed the limit.
        node.iter = self.visit(node.iter)
        loop_vars = set()
        if isinstance(node.target, ast.Name):
            loop_vars.add(node.target.id)
        elif isinstance(node.target, ast.Tuple):
            for elt in node.target.elts:
                if isinstance(elt, ast.Name):
                    loop_vars.add(elt.id)
        saved = self.known_params
        self.known_params = saved | frozenset(loop_vars)
        try:
            new_body = [self.visit(stmt) for stmt in node.body]
        finally:
            self.known_params = saved
        tick_call = ast.Expr(value=ast.Call(
            func=ast.Name(id="__loop_tick", ctx=ast.Load()),
            args=[], keywords=[],
        ))
        ast.copy_location(tick_call, node)
        ast.copy_location(tick_call.value, node)
        node.body = [tick_call] + new_body
        return node


def _for_target_names(target: ast.AST) -> List[str]:
    """Extract the loop-variable names from a `for` target. Allowed
    shapes: a single Name (`for eid in ...`), or a 2-Tuple of Names
    (`for (cx, cy) in cells_in_burst(...)`). Anything else (nested
    tuples, starred targets, attribute targets) is rejected — the
    sandbox keeps loop targets to plain bindings."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Tuple):
        names = []
        for elt in target.elts:
            if not isinstance(elt, ast.Name):
                raise FormulaError(
                    "for-loop target tuple must contain only plain names "
                    "(e.g. `for (cx, cy) in cells_in_burst(...)`)."
                )
            names.append(elt.id)
        if not names:
            raise FormulaError(
                "for-loop target tuple cannot be empty."
            )
        return names
    raise FormulaError(
        "for-loop target must be a name or a tuple of names "
        "(e.g. `for eid in entities_within(...)` or "
        "`for (cx, cy) in cells_in_burst(...)`)."
    )


def _validate_for(
    node: ast.For, *, action_mode: bool = False,
) -> Tuple[List[str], "frozenset[str]"]:
    """Verify a For node's iterable is a Call to a _LOOPABLE_FUNCS name,
    and return the loop variable names. The caller augments
    known_params with these names before recursing into the body.

    When action_mode is True, the iterable may also be a bare Name in
    _ACTION_LOOPABLE_NAMES (currently just `target`) — that lets
    `for eid in target:` work cleanly when the action's target_type
    is entity_list / location_list. Iterating a non-list target at
    runtime is a clean Python TypeError that surfaces as a
    FormulaError."""
    if node.orelse:
        # `for ... else:` is supported by Python but its semantics
        # ("else runs unless break") are surprising in a sandboxed
        # context where there's no break. Reject up front.
        raise FormulaError(
            "for-loop `else:` clause is not supported."
        )
    it = node.iter
    is_loopable_call = (
        isinstance(it, ast.Call)
        and isinstance(it.func, ast.Name)
        and it.func.id in _LOOPABLE_FUNCS
    )
    is_action_loopable_name = (
        action_mode
        and isinstance(it, ast.Name)
        and it.id in _ACTION_LOOPABLE_NAMES
    )
    if not (is_loopable_call or is_action_loopable_name):
        allowed = ", ".join(sorted(_LOOPABLE_FUNCS))
        if action_mode:
            extras = ", ".join(sorted(_ACTION_LOOPABLE_NAMES))
            extra_msg = f" (or, in action mode, one of {{{extras}}})"
        else:
            extra_msg = ""
        raise FormulaError(
            f"for-loop iterable must be a direct call to one of "
            f"{{{allowed}}}{extra_msg} — got "
            f"`{ast.unparse(it) if hasattr(ast, 'unparse') else type(it).__name__}`."
        )
    names = _for_target_names(node.target)
    return names, frozenset(names)


def _validate_tree(
    tree: ast.AST,
    known_funcs: "frozenset[str]" = frozenset(),
    known_params: "frozenset[str]" = frozenset(),
    action_mode: bool = False,
) -> None:
    """Walk the (already-transformed) AST and reject anything outside the
    sandbox whitelist.

    known_funcs   names callable as user-defined formula functions. The
                  engine passes the current match's function registry so
                  a formula calling a defined function validates; an
                  undefined name still errors.
    known_params  identifier names that are legal as plain values in this
                  scope. Used for function-body parameters AND for-loop
                  target variables; both extend the in-scope identifier
                  set the same way.
    action_mode   when True, three extra identifier sets are allowed:
                  _ACTION_BINDING_NAMES (source/args/target) as plain
                  Names, and _ACTION_BUILTINS (cmd/fail) as Call
                  targets. Also relaxes _validate_for to accept
                  `for x in target:` for *_list target types.
    """
    # Action-mode allowances expressed as extra membership tests in
    # the identifier and call whitelists. Centralized here so each
    # check site stays readable.
    extra_names = _ACTION_BINDING_NAMES if action_mode else frozenset()
    extra_calls = _ACTION_BUILTINS if action_mode else frozenset()

    def _check_node(n: ast.AST, scope_params: "frozenset[str]") -> None:
        if not isinstance(n, _ALLOWED_NODES):
            raise FormulaError(f"Disallowed syntax: {type(n).__name__}")
        if isinstance(n, ast.Name):
            if (n.id not in ("__read", "__write", "__read_match",
                             "__write_match", "__loop_tick")
                    and n.id not in _ALLOWED_FUNCS
                    and n.id not in _MATCH_FUNC_NAMES
                    and n.id not in HOOK_CONTEXT_NAMES
                    and n.id not in _ENTITY_TOKEN_NAMES
                    and n.id not in known_funcs
                    and n.id not in scope_params
                    and n.id not in extra_names
                    and n.id not in extra_calls):
                # Did-you-mean: the identifier surface is large (hook
                # context names, entity tokens, allowed funcs, match
                # funcs, scope params, user-defined functions) so a
                # typo-recovery hint is most of what tells the GM
                # whether they misspelled a builtin or a local param.
                import difflib as _difflib
                pool = (set(_ALLOWED_FUNCS) | set(_MATCH_FUNC_NAMES)
                        | set(HOOK_CONTEXT_NAMES) | set(_ENTITY_TOKEN_NAMES)
                        | set(known_funcs) | set(scope_params)
                        | set(extra_names) | set(extra_calls))
                hits = _difflib.get_close_matches(n.id, list(pool), n=3, cutoff=0.6)
                if hits:
                    suggestions = ", ".join(f"'{h}'" for h in hits)
                    raise FormulaError(
                        f"Unknown identifier '{n.id}'. Did you mean: {suggestions}?"
                    )
                raise FormulaError(f"Unknown identifier '{n.id}'.")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise FormulaError("Only direct function calls are allowed.")
            fname = n.func.id
            if (fname not in ("__read", "__write", "__read_match",
                              "__write_match", "__loop_tick")
                    and fname not in _ALLOWED_FUNCS
                    and fname not in _MATCH_FUNC_NAMES
                    and fname not in known_funcs
                    and fname not in extra_calls):
                import difflib as _difflib
                pool = (set(_ALLOWED_FUNCS) | set(_MATCH_FUNC_NAMES)
                        | set(known_funcs) | set(extra_calls))
                hits = _difflib.get_close_matches(fname, list(pool), n=3, cutoff=0.6)
                if hits:
                    suggestions = ", ".join(f"'{h}'" for h in hits)
                    raise FormulaError(
                        f"Function '{fname}' is not allowed. "
                        f"Did you mean: {suggestions}?"
                    )
                raise FormulaError(f"Function '{fname}' is not allowed.")

    def _walk(node: ast.AST, scope_params: "frozenset[str]") -> None:
        _check_node(node, scope_params)
        if isinstance(node, ast.For):
            # Validate the iterable + target shape; recurse into the
            # body with the loop variables added to scope. The target
            # node itself does NOT need to be _check_node'd — its
            # Tuple/Name children are loop-binding shapes, not Loads.
            _check_node(node.iter, scope_params)
            for child in ast.iter_child_nodes(node.iter):
                _walk(child, scope_params)
            _, loop_vars = _validate_for(node, action_mode=action_mode)
            body_scope = scope_params | loop_vars
            for stmt in node.body:
                _walk(stmt, body_scope)
            return
        for child in ast.iter_child_nodes(node):
            _walk(child, scope_params)

    _walk(tree, known_params)


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


def validate_formula(
    src: str, *, mode: str = "exec",
    known_funcs: "frozenset[str]" = frozenset(),
    known_params: "frozenset[str]" = frozenset(),
) -> None:
    """Parse, transform, and validate a formula source string.

    Raises FormulaError on any syntactic or semantic problem. Useful for
    failing early when a passive (or formula function) is registered,
    rather than at fire/call time.

    mode: 'exec' for full program bodies (assignments and expressions),
          'eval' for pure expressions.
    known_funcs / known_params: forwarded to _validate_tree. Pass the
          match's defined-function names so a formula referencing a
          user function validates; pass a function's parameter names
          when validating that function's body.
    """
    FormulaEngine._prepare(src, mode, known_funcs=known_funcs,
                           known_params=known_params)


# --- engine ------------------------------------------------------------------

class FormulaEngine:
    """Parses and evaluates formulas against a Match."""

    def __init__(self, match):
        self._match = match
        # Current nesting depth of user-defined formula-function calls.
        # Bumped on entry to each call, restored on exit. Guards against
        # unbounded recursion blowing Python's stack — see the
        # formula_function_recursion_limit rule. A fresh engine is
        # created per hook fire / command, so this naturally starts at 0
        # for each top-level evaluation.
        self._fn_depth = 0
        # Entity ids whose vars or status were mutated during the current
        # top-level evaluation, in first-touch order. Reset at the start
        # of every eval_program / eval_expression. Surfaced via
        # `affected_entities` so the !eval command can report
        # "Affected: a, b, c" for a side-effecting formula that returns
        # no value (instead of a confusing "= None").
        self._affected_order: List[str] = []
        self._affected_set: set = set()

    def _note_affected(self, eid: str) -> None:
        if eid not in self._affected_set:
            self._affected_set.add(eid)
            self._affected_order.append(eid)

    @property
    def affected_entities(self) -> List[str]:
        return list(self._affected_order)

    def _reset_affected(self) -> None:
        self._affected_order = []
        self._affected_set = set()

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
        e.write_var(path, value)
        # Track for the "Affected: a, b, c" command-layer summary.
        self._note_affected(eid)
        return value

    def _read_match(self, path: str) -> Any:
        """Read match-level var at dotted `path` (the `match.<path>`
        formula root). Raises FormulaError on a missing path — same
        contract as entity var reads, so `match.x = match.x + 1` requires
        x to already exist (initialize it first via `!match var set` or
        check with match_var_has)."""
        if self._match is None:
            raise FormulaError("match vars are unavailable in this context.")
        return _get_path(self._match.vars, path)

    def _write_match(self, path: str, value: Any) -> Any:
        """Write match-level var at dotted `path`. Plain storage — match
        vars don't fire var hooks (no per-entity owner). Returns the
        written value so it composes inside larger expressions."""
        if self._match is None:
            raise FormulaError("match vars are unavailable in this context.")
        _set_path(self._match.vars, path, value)
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
        # Bounded for-loop counter. The transformer injects a
        # __loop_tick() call as the first statement in every loop body,
        # so this runs once per iteration across all loops in the
        # evaluation. Reset to 0 at the start of every top-level
        # eval_program / eval_expression call.
        loop_limit = int(self._match.rules.get("formula_loop_limit", 10000)) \
            if self._match else 10000
        self._loop_iters = 0
        def _loop_tick():
            self._loop_iters += 1
            if self._loop_iters > loop_limit:
                raise FormulaError(
                    f"formula for-loop iteration limit ({loop_limit}) "
                    f"exceeded — likely an unbounded loop. Tune via the "
                    f"formula_loop_limit rule if a higher cap is "
                    f"intentionally needed."
                )
        ns: Dict[str, Any] = {
            "__read":  lambda who, path:        self._read(who, path, ctx),
            "__write": lambda who, path, value: self._write(who, path, value, ctx),
            "__read_match":  lambda path:        self._read_match(path),
            "__write_match": lambda path, value: self._write_match(path, value),
            "__loop_tick": _loop_tick,
            **_ALLOWED_FUNCS,
        }
        # Per-name default: was_clamped is boolean-flavored (default False);
        # args defaults to an empty dict (so attribute access via
        # ArgsProxy never NPEs); everything else defaults to None.
        _CONTEXT_DEFAULTS = {"was_clamped": False, "args": {}}
        for name in HOOK_CONTEXT_NAMES:
            val = extras.get(name, _CONTEXT_DEFAULTS.get(name))
            # `args` binding is wrapped in an ArgsProxy so passives can
            # use the same `args.<key>` attribute-access pattern that
            # action bodies use. Action mode overrides this binding
            # later via eval_program's action_bindings dict, so the
            # action body sees its own (mutable) proxy.
            if name == "args" and isinstance(val, dict):
                from action import ArgsProxy  # local import to avoid cycle
                val = ArgsProxy(val)
            ns[name] = val
        # Bare entity-id tokens. Bound to the actual id string when
        # available, None otherwise. Calls that consume them (status_*,
        # group_*, etc.) detect None via _eid and surface a clear
        # "X is unbound" FormulaError.
        ns["self"]    = ctx.target
        ns["this"]    = ctx.this
        ns["current"] = ctx.this

        # Seeded RNG: when the random_seed rule is non-empty, shadow the
        # global-RNG random_int/random_string with versions bound to a
        # match-local random.Random so rolls are reproducible. The RNG
        # lives on the match and advances across calls (so a SEQUENCE of
        # rolls is deterministic, not every roll identical). It's rebuilt
        # when the seed changes or the match reloads (runtime-only state).
        seed = self._match.rules.get("random_seed", "") if self._match else ""
        if seed:
            rng = getattr(self._match, "_rng", None)
            if rng is None or getattr(self._match, "_rng_seed", None) != seed:
                rng = random.Random(seed)
                self._match._rng = rng
                self._match._rng_seed = seed
            ns["random_int"] = (
                lambda lo, hi, _r=rng: _random_int_impl(_r, lo, hi)
            )
            ns["random_string"] = (
                lambda *choices, _r=rng: _random_string_impl(_r, choices)
            )
            ns["roll"] = (
                lambda spec, _r=rng: _roll_impl(_r, spec)
            )

        # Match-bound group functions. Each takes string args; entity-id
        # args go through ctx.resolve_who so the special tokens "self",
        # "this", and "current" work the same as inside entity[X].path
        # — meaning a formula can write group_add("swarm", "self") if
        # the bare-identifier shorthand self_id() feels too verbose.
        match = self._match
        def _active_rng():
            """The RNG hit_location (and any other replay-safe roll) should
            use: the match-seeded random.Random when random_seed is set,
            else the global `random` module. Both honor getstate/setstate,
            so the action choice-replay snapshot covers either."""
            return getattr(match, "_rng", None) or random
        def _eid(token: Any) -> str:
            if token is None:
                # A bare `self` / `this` / `current` token was passed,
                # but the calling context didn't bind that side
                # (e.g. `self_id()` outside a passive). Distinguish from
                # an arbitrary None so the message is actionable.
                raise FormulaError(
                    "entity id is None — 'self', 'this', or 'current' "
                    "was used but is unbound in this context."
                )
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

        # Mutating tile functions. tile_set / tile_del / tile_clear
        # forward to Match.tile_set_path / tile_del_path / direct dict
        # mutation respectively. These are meant for use INSIDE tile
        # hooks (e.g. a landmine's on_enter clearing itself after
        # dealing damage), but they're also callable from any other
        # formula context. No recursion guard is needed in this PR
        # because tile data writes don't trigger var events, and the
        # formula engine can't move entities (writes to entity[X].x
        # are blocked); a hook can't reentrantly fire more tile hooks
        # from within itself.
        def _tile_set(x: Any, y: Any, path: Any, value: Any) -> Any:
            if not isinstance(x, int) or isinstance(x, bool):
                raise FormulaError(
                    f"tile_set(x, y, path, value): x must be int, got "
                    f"{type(x).__name__}."
                )
            if not isinstance(y, int) or isinstance(y, bool):
                raise FormulaError(
                    f"tile_set(x, y, path, value): y must be int, got "
                    f"{type(y).__name__}."
                )
            if not isinstance(path, str):
                raise FormulaError(
                    f"tile_set(x, y, path, value): path must be str, got "
                    f"{type(path).__name__}."
                )
            try:
                match.tile_set_path(x, y, path, value)
            except VTTError as ex:
                raise FormulaError(str(ex))
            return value

        def _tile_del(x: Any, y: Any, path: Any) -> bool:
            if not isinstance(x, int) or isinstance(x, bool):
                raise FormulaError(
                    f"tile_del(x, y, path): x must be int, got "
                    f"{type(x).__name__}."
                )
            if not isinstance(y, int) or isinstance(y, bool):
                raise FormulaError(
                    f"tile_del(x, y, path): y must be int, got "
                    f"{type(y).__name__}."
                )
            if not isinstance(path, str):
                raise FormulaError(
                    f"tile_del(x, y, path): path must be str, got "
                    f"{type(path).__name__}."
                )
            # Pre-check existence so we can return True/False instead
            # of raising on a missing path — formulas chaining
            # tile_del calls would otherwise need a tile_has guard
            # for each one. Match.tile_del_path raises NotFound on
            # absent paths; we swallow that and return False.
            if not match.tile_has_path(x, y, path):
                return False
            try:
                match.tile_del_path(x, y, path)
            except VTTError as ex:
                raise FormulaError(str(ex))
            return True

        def _tile_clear(x: Any, y: Any) -> bool:
            if not isinstance(x, int) or isinstance(x, bool):
                raise FormulaError(
                    f"tile_clear(x, y): x must be int, got "
                    f"{type(x).__name__}."
                )
            if not isinstance(y, int) or isinstance(y, bool):
                raise FormulaError(
                    f"tile_clear(x, y): y must be int, got "
                    f"{type(y).__name__}."
                )
            if (x, y) not in match.tiles:
                return False
            del match.tiles[(x, y)]
            return True

        ns["tile_set"] = _tile_set
        ns["tile_del"] = _tile_del
        ns["tile_clear"] = _tile_clear

        # ---- movement writes & tile-hook firing ----
        # move_entity / move_step let a formula change an entity's
        # position. The geometry / occupancy validation and the tile
        # hook firing pipeline are exactly what Entity.tp /
        # Entity.move_dirs do — so formula-driven movement is
        # observationally identical to a command-driven move (on_enter,
        # on_exit, on_stop, on_entity_moved all fire). Both return a
        # value rather than mutating in place from the caller's POV.
        from logic import (
            normalize_direction as _normalize_direction,
            DIRECTION_VECTORS as _DIRECTION_VECTORS,
            DIAGONAL_DIRECTIONS as _DIAGONAL_DIRECTIONS,
            OutOfBounds as _OutOfBounds,
            Occupied as _Occupied,
        )

        def _move_entity(eid_t: Any, x: Any, y: Any) -> tuple:
            """move_entity(eid, x, y): teleport the entity to (x, y) with
            full tp() semantics (bounds + occupancy validated, tile
            hooks + on_entity_moved fire). Returns (x, y) on success;
            raises FormulaError on validation failure so a formula can
            safeguard with prior reads (in_bounds / is_occupied are not
            yet exposed; check via tile_keys or entity coordinate
            equality)."""
            eid = _eid(eid_t)
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"move_entity: unknown entity id '{eid}'.")
            if not isinstance(x, int) or isinstance(x, bool):
                raise FormulaError(
                    f"move_entity(eid, x, y): x must be int, got "
                    f"{type(x).__name__}."
                )
            if not isinstance(y, int) or isinstance(y, bool):
                raise FormulaError(
                    f"move_entity(eid, x, y): y must be int, got "
                    f"{type(y).__name__}."
                )
            try:
                e.tp(x, y)
            except (_OutOfBounds, _Occupied) as ex:
                raise FormulaError(str(ex))
            self._note_affected(eid)
            return (x, y)

        def _move_step(eid_t: Any, direction: Any) -> bool:
            """move_step(eid, direction): one-cell move toward a named
            direction. Returns True if the step happened, False if it
            was blocked (off-grid or occupied) so a formula can branch
            without try-equivalent patterns. Honors
            allow_diagonal_movement the same way !ent move does."""
            eid = _eid(eid_t)
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"move_step: unknown entity id '{eid}'.")
            if not isinstance(direction, str):
                raise FormulaError(
                    f"move_step(eid, direction): direction must be a "
                    f"string, got {type(direction).__name__}."
                )
            canon = _normalize_direction(direction)
            if canon is None:
                raise FormulaError(
                    f"move_step(eid, direction): unknown direction "
                    f"'{direction}'."
                )
            allow_diag = bool(match.rules.get("allow_diagonal_movement", False))
            if canon in _DIAGONAL_DIRECTIONS and not allow_diag:
                raise FormulaError(
                    f"move_step: diagonal direction '{direction}' "
                    f"requires allow_diagonal_movement=True."
                )
            dx, dy = _DIRECTION_VECTORS[canon]
            nx, ny = e.x + dx, e.y + dy
            if not match.in_bounds(nx, ny):
                return False
            # Stackable movers bypass the precheck — Entity.tp itself
            # would also bypass, but we precheck here to return False
            # (the formula's documented "blocked" signal) rather than
            # raising. For stackable, "blocked" never applies.
            if not e.is_cell_stackable and match.is_occupied(nx, ny, ignore_entity_id=e.id):
                return False
            try:
                e.tp(nx, ny)
            except (_OutOfBounds, _Occupied):
                return False
            self._note_affected(eid)
            return True

        def _fire_tile_hook(x: Any, y: Any, hook_name: Any,
                            eid_t: Any = None) -> int:
            """fire_tile_hook(x, y, hook_name, eid=None): fire the
            named hook at (x, y) immediately, binding `self` to eid
            if given (otherwise to the caller's ctx.target).

            Returns the number of log lines emitted (the engine's
            convention for "did anything fire?": >0 means a hook ran,
            == 0 means no hook of that name was registered on the
            tile). Useful for chained effects ('this tile detonates
            and ignites its neighbors') and for tests
            (`!eval --as ... fire_tile_hook(3, 3, 'on_enter')`).
            """
            if not isinstance(x, int) or isinstance(x, bool):
                raise FormulaError(
                    f"fire_tile_hook(x, y, ...): x must be int, got "
                    f"{type(x).__name__}."
                )
            if not isinstance(y, int) or isinstance(y, bool):
                raise FormulaError(
                    f"fire_tile_hook(x, y, ...): y must be int, got "
                    f"{type(y).__name__}."
                )
            if not isinstance(hook_name, str):
                raise FormulaError(
                    f"fire_tile_hook(x, y, hook_name): hook_name must "
                    f"be a string."
                )
            from logic import TILE_HOOK_NAMES as _THN
            if hook_name not in _THN:
                allowed = ", ".join(sorted(_THN))
                raise FormulaError(
                    f"fire_tile_hook: unknown hook_name '{hook_name}'. "
                    f"Allowed: {allowed}."
                )
            target_eid = ctx.target
            if eid_t is not None:
                target_eid = _eid(eid_t)
            log = match.fire_tile_hook(hook_name, target_eid, x, y)
            return len(log)

        def _push_entity(eid_t: Any, direction: Any, n: Any = 1) -> int:
            """push_entity(eid, direction, n=1): step `eid` up to `n`
            cells in `direction`, stopping at the first blocked cell
            (off-grid or occupied). Returns the NUMBER of cells actually
            moved (0..n). Honors allow_diagonal_movement the same way
            move_step does; raises FormulaError on bad direction or
            unknown entity. Per-step tile hooks (on_exit/on_enter) and
            the final on_stop / on_entity_moved fire the same way they
            would for a stepwise `!ent move`, so a pushed entity walks
            through fire tiles cell by cell."""
            eid = _eid(eid_t)
            # Geometry, hook firing and validation live in
            # Match.push_entity (shared with the !ent push command);
            # this primitive just resolves the id, surfaces any engine
            # error as a FormulaError, and records the affected entity.
            try:
                steps, _log = match.push_entity(eid, direction, n)
            except VTTError as ex:
                raise FormulaError(str(ex))
            if steps:
                self._note_affected(eid)
            return steps

        def _pull_entity(eid_t: Any, x: Any, y: Any, n: Any = 1) -> int:
            """pull_entity(eid, x, y, n=1): drag `eid` up to `n` cells
            TOWARD the point (x, y), stopping at the first blocked cell
            (off-grid or occupied) or once it reaches the target cell.
            Returns the NUMBER of cells actually moved (0..n). The point-
            directed twin of push_entity — the heading recomputes each
            step so the path curves toward (x, y); honors
            allow_diagonal_movement the same way. Per-step tile hooks and
            the final on_stop / on_entity_moved fire as for a stepwise
            move. Raises FormulaError on a non-int coord/n or unknown
            entity."""
            eid = _eid(eid_t)
            # Geometry, hook firing and validation live in
            # Match.pull_entity (shared with the !ent pull command).
            try:
                steps, _log = match.pull_entity(eid, x, y, n)
            except VTTError as ex:
                raise FormulaError(str(ex))
            if steps:
                self._note_affected(eid)
            return steps

        def _swap_entities(a_t: Any, b_t: Any) -> bool:
            """swap_entities(a, b): atomically exchange the positions of
            two entities. Both must exist; swapping an entity with
            itself is a no-op that returns False. Bypasses the usual
            occupancy check (the two cells ARE occupied — by each
            other — but the swap is atomic). Fires on_exit on both old
            cells, on_enter + on_stop on both new cells, and
            on_entity_moved for both entities. Returns True on a real
            swap, False on the same-entity no-op."""
            aid = _eid(a_t)
            bid = _eid(b_t)
            # The atomic swap, occupancy bypass and hook firing live in
            # Match.swap_entities (shared with the !ent swap command).
            try:
                swapped, _log = match.swap_entities(aid, bid)
            except VTTError as ex:
                raise FormulaError(str(ex))
            if swapped:
                self._note_affected(aid)
                self._note_affected(bid)
            return swapped

        ns["move_entity"] = _move_entity
        ns["move_step"]   = _move_step
        ns["push_entity"] = _push_entity
        ns["pull_entity"] = _pull_entity
        ns["swap_entities"] = _swap_entities
        ns["fire_tile_hook"] = _fire_tile_hook

        # Team-relationship predicates. All four resolve entity ids
        # through _eid (so they accept "self" / "this" / "current"
        # sentinels) and then read the team via Entity.team, which
        # honors the team_var rule rename. A "no team" entity has
        # team == None (or empty string by convention); team
        # comparisons treat that as a singleton-bucket — see the
        # docstrings above in _MATCH_FUNC_NAMES for the truth table.
        def _team_of(eid: str) -> Optional[str]:
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(
                    f"unknown entity id '{eid}'."
                )
            t = e.team
            if t is None or t == "":
                return None
            return t

        def _is_same_team(a_token: Any, b_token: Any) -> bool:
            a = _eid(a_token); b = _eid(b_token)
            ta, tb = _team_of(a), _team_of(b)
            if ta is None or tb is None:
                return False
            return ta == tb

        def _is_hostile(a_token: Any, b_token: Any) -> bool:
            # Inverse of is_same_team. Teamless entities are "hostile to
            # everyone" — they have no allies to spare from a friendly-
            # fire-off attack.
            return not _is_same_team(a_token, b_token)

        def _is_part_of_team(a_token: Any, team: Any) -> bool:
            if not isinstance(team, str):
                raise FormulaError(
                    f"is_part_of_team(a, team): team must be a string, "
                    f"got {type(team).__name__}."
                )
            a = _eid(a_token)
            ta = _team_of(a)
            if ta is None:
                # Teamless: only matches the empty-string sentinel,
                # which we already convert to None — so is_part_of_team
                # against "" or absent never matches. Callers wanting
                # to test "is teamless" should compare entity[X].team
                # directly.
                return False
            return ta == team

        def _is_attackable(a_token: Any, b_token: Any) -> bool:
            a = _eid(a_token); b = _eid(b_token)
            # An entity is never attackable by itself, regardless of
            # the friendlyfire rule. Self-targeting belongs in
            # separate "self-buff" / "self-damage" formula paths.
            if a == b:
                return False
            if bool(match.rules.get("friendlyfire", False)):
                # Friendly fire on: same-team and different-team alike
                # are valid attack targets. Only self is excluded.
                return True
            # Friendly fire off (default): same-team is protected;
            # otherwise the targeting rule matches is_hostile.
            return _is_hostile(a_token, b_token)

        ns["is_same_team"]   = _is_same_team
        ns["is_hostile"]     = _is_hostile
        ns["is_part_of_team"] = _is_part_of_team
        ns["is_attackable"]  = _is_attackable

        # ---- spatial-query helpers ----
        # Shared relation filter for entities_within / nearest_entity.
        # `relation` selects which other entities count, relative to the
        # reference entity `ref`. "" / "any" = no filter. The named
        # relations reuse the team-predicate closures above so they honor
        # team_var renames and the friendlyfire rule (for "attackable").
        def _relation_ok(relation: str, ref: str, other: str) -> bool:
            if relation in ("", "any"):
                return True
            if relation == "hostile":
                return _is_hostile(ref, other)
            if relation in ("ally", "same_team"):
                return _is_same_team(ref, other)
            if relation == "attackable":
                return _is_attackable(ref, other)
            raise FormulaError(
                f"unknown relation '{relation}'. Allowed: any, hostile, "
                f"ally, same_team, attackable."
            )

        def _candidates(ref_eid: str, relation: str):
            """Yield (other_eid, entity) for every ALIVE entity other than
            ref_eid that passes the relation filter."""
            ref_e = match.entities.get(ref_eid)
            if ref_e is None:
                raise FormulaError(f"unknown entity id '{ref_eid}'.")
            for oid, oe in match.entities.items():
                if oid == ref_eid:
                    continue
                if not oe.is_alive:
                    continue
                # Attached body parts aren't independent map targets — a
                # distance/AoE query resolves to the parent, not its parts.
                if oe.is_glued_part:
                    continue
                if not _relation_ok(relation, ref_eid, oid):
                    continue
                yield oid, oe, ref_e

        def _ent_dist(ref_e, oe, mode):
            # Footprint-aware entity-to-entity distance = the gap between
            # the two axis-aligned footprint rectangles (nearest-cell
            # distance), combined per `mode`. Computed in closed form: the
            # per-axis gap is 0 when the rectangles overlap on that axis,
            # else the separation; distance(0,0,gx,gy,mode) then applies
            # the same metric as point distance. For two 1×1 entities this
            # reduces to the old anchor-to-anchor distance.
            rw, rh = match.entity_footprint(ref_e)
            ow, oh = match.entity_footprint(oe)
            gx = max(0, ref_e.x - (oe.x + ow - 1), oe.x - (ref_e.x + rw - 1))
            gy = max(0, ref_e.y - (oe.y + oh - 1), oe.y - (ref_e.y + rh - 1))
            return _distance(0, 0, gx, gy, mode)

        def _entities_within(eid_token: Any, n: Any,
                             mode: Any = "square_radius_distance",
                             relation: Any = "") -> list:
            eid = _eid(eid_token)
            if isinstance(n, bool) or not isinstance(n, (int, float)):
                raise FormulaError(
                    f"entities_within(eid, n, ...): n must be a number, "
                    f"got {type(n).__name__}."
                )
            if not isinstance(relation, str):
                raise FormulaError(
                    "entities_within(...): relation must be a string."
                )
            scored = []
            for oid, oe, ref_e in _candidates(eid, relation):
                d = _ent_dist(ref_e, oe, mode)
                if d <= n:
                    scored.append((d, oid))
            scored.sort(key=lambda t: (t[0], t[1]))
            return [oid for _, oid in scored]

        def _nearest_entity(eid_token: Any, relation: Any = "",
                            mode: Any = "square_radius_distance") -> str:
            eid = _eid(eid_token)
            if not isinstance(relation, str):
                raise FormulaError(
                    "nearest_entity(...): relation must be a string."
                )
            best_id = ""
            best_d = None
            for oid, oe, ref_e in _candidates(eid, relation):
                d = _ent_dist(ref_e, oe, mode)
                # Tie-break by id so the result is deterministic.
                if best_d is None or d < best_d or (d == best_d and oid < best_id):
                    best_d = d
                    best_id = oid
            return best_id

        ns["entities_within"] = _entities_within
        ns["nearest_entity"]  = _nearest_entity

        # ---- footprint / large-entity accessors ----
        def _require_e(eid_t: Any, fn: str):
            eid = _eid(eid_t)
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"{fn}(...): unknown entity id '{eid}'.")
            return e

        def _footprint_width(eid_t: Any) -> int:
            return match.entity_footprint(_require_e(eid_t, "footprint_width"))[0]

        def _footprint_height(eid_t: Any) -> int:
            return match.entity_footprint(_require_e(eid_t, "footprint_height"))[1]

        def _footprint_cells(eid_t: Any) -> list:
            # List of (x,y) the entity covers; loopable + coord-readable.
            return [(cx, cy) for cx, cy in
                    match.entity_cells(_require_e(eid_t, "footprint_cells"))]

        def _occupies(eid_t: Any, x: Any, y: Any) -> bool:
            e = _require_e(eid_t, "occupies")
            return match.entity_occupies(
                e, _coord_int(x, "occupies", "x"), _coord_int(y, "occupies", "y"))

        def _cell_entity(x: Any, y: Any) -> str:
            # The blocking entity covering (x,y), or "" if the cell is free.
            occ = match.cell_occupant(
                _coord_int(x, "cell_entity", "x"),
                _coord_int(y, "cell_entity", "y"))
            return occ or ""

        def _entity_center(eid_t: Any) -> tuple:
            # The footprint's center CELL (rounding down for even sizes so
            # the result is always a covered integer cell).
            e = _require_e(eid_t, "entity_center")
            w, h = match.entity_footprint(e)
            return (e.x + (w - 1) // 2, e.y + (h - 1) // 2)

        def _aoe_origin(eid_t: Any) -> tuple:
            # Where an area effect cast BY this entity originates: its
            # center cell or its anchor, per the aoe_origin_mode rule.
            e = _require_e(eid_t, "aoe_origin")
            if str(match.rules.get("aoe_origin_mode", "center")) == "anchor":
                return (e.x, e.y)
            w, h = match.entity_footprint(e)
            return (e.x + (w - 1) // 2, e.y + (h - 1) // 2)

        ns["footprint_width"] = _footprint_width
        ns["footprint_height"] = _footprint_height
        ns["footprint_cells"] = _footprint_cells
        ns["occupies"] = _occupies
        ns["cell_entity"] = _cell_entity
        ns["entity_center"] = _entity_center
        ns["aoe_origin"] = _aoe_origin

        # ---- group iteration ----
        def _group_members(name: Any) -> list:
            if not isinstance(name, str):
                raise FormulaError(
                    "group_members(name): name must be a string."
                )
            # Returns the live list copy (insertion order). The for-loop
            # iterates a snapshot, so concurrent group_add / group_remove
            # during iteration is safe even though they mutate this list.
            return list(match.groups.get(name, []))
        ns["group_members"] = _group_members

        # ---- status data accessors ----
        # Mirror the tile_* shape: dotted-string paths into the per-status
        # data dict, raise on missing path for get, bool returns for
        # has / has_path / del / add / remove.

        def _status_data(eid: str, name: str, must_exist: bool):
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"unknown entity id '{eid}'.")
            data = e.status.get(name)
            if data is None:
                if must_exist:
                    raise FormulaError(
                        f"entity '{eid}' has no status '{name}'."
                    )
                return None, e
            return data, e

        def _status_has(eid_t: Any, name: Any) -> bool:
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_has(eid, name): name must be a string.")
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"unknown entity id '{eid}'.")
            return name in e.status

        def _status_has_path(eid_t: Any, name: Any, path: Any) -> bool:
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_has_path(eid, name, path): name must be a string.")
            if not isinstance(path, str) or not path:
                raise FormulaError("status_has_path(eid, name, path): path must be a non-empty string.")
            data, _ = _status_data(eid, name, must_exist=False)
            if data is None:
                return False
            cur = data
            for k in path.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return False
                cur = cur[k]
            return True

        def _status_get(eid_t: Any, name: Any, path: Any) -> Any:
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_get(eid, name, path): name must be a string.")
            if not isinstance(path, str) or not path:
                raise FormulaError("status_get(eid, name, path): path must be a non-empty string.")
            data, _ = _status_data(eid, name, must_exist=True)
            cur = data
            keys = path.split(".")
            for i, k in enumerate(keys):
                if not isinstance(cur, dict) or k not in cur:
                    where = ".".join(keys[:i + 1])
                    raise FormulaError(
                        f"status '{eid}.{name}' has no value at '{where}'."
                    )
                cur = cur[k]
            return cur

        def _status_set(eid_t: Any, name: Any, path: Any, value: Any) -> Any:
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_set(eid, name, path, value): name must be a string.")
            if not isinstance(path, str) or not path:
                raise FormulaError("status_set(eid, name, path, value): path must be a non-empty string.")
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"unknown entity id '{eid}'.")
            before = copy.deepcopy(e.status[name]) if name in e.status else None
            data = e.status.setdefault(name, {})
            keys = path.split(".")
            cur = data
            for i, k in enumerate(keys[:-1]):
                existing = cur.get(k)
                if existing is not None and not isinstance(existing, dict):
                    where = ".".join(keys[:i + 1])
                    raise FormulaError(
                        f"status '{eid}.{name}' value at '{where}' is "
                        f"{type(existing).__name__}, not a dict."
                    )
                if k not in cur:
                    cur[k] = {}
                cur = cur[k]
            cur[keys[-1]] = value
            after = copy.deepcopy(e.status[name])
            match._emit_status_diff(eid, name, before, after)
            self._note_affected(eid)
            return value

        def _status_del(eid_t: Any, name: Any, path: Any) -> bool:
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_del(eid, name, path): name must be a string.")
            if not isinstance(path, str) or not path:
                raise FormulaError("status_del(eid, name, path): path must be a non-empty string.")
            data, _ = _status_data(eid, name, must_exist=False)
            if data is None:
                return False
            before = copy.deepcopy(data)
            keys = path.split(".")
            chain = [data]
            for i, k in enumerate(keys[:-1]):
                cur = chain[-1]
                if not isinstance(cur, dict) or k not in cur:
                    return False
                chain.append(cur[k])
            leaf = chain[-1]
            if not isinstance(leaf, dict) or keys[-1] not in leaf:
                return False
            del leaf[keys[-1]]
            for i in range(len(chain) - 1, 0, -1):
                if chain[i]:
                    break
                del chain[i - 1][keys[i - 1]]
            after = copy.deepcopy(match.entities[eid].status.get(name))
            match._emit_status_diff(eid, name, before, after)
            self._note_affected(eid)
            return True

        def _status_add(eid_t: Any, name: Any) -> bool:
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_add(eid, name): name must be a string.")
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"unknown entity id '{eid}'.")
            if name in e.status:
                return False
            e.status[name] = {}
            match._emit_status_diff(eid, name, None, {})
            self._note_affected(eid)
            return True

        def _status_remove(eid_t: Any, name: Any) -> bool:
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_remove(eid, name): name must be a string.")
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"unknown entity id '{eid}'.")
            if name not in e.status:
                return False
            before = copy.deepcopy(e.status[name])
            del e.status[name]
            match._emit_status_diff(eid, name, before, None)
            self._note_affected(eid)
            return True

        def _status_apply(eid_t: Any, name: Any,
                          level: Any = None, duration: Any = None) -> bool:
            """status_apply(eid, name, level=None, duration=None): apply a
            status, honoring its definition's stack mode (else the
            status_default_stack rule) when already present. Seeds the
            definition's default data on a first application. Returns True
            (always applied/updated; a 'none' stack mode on a present
            status is a silent no-op)."""
            eid = _eid(eid_t)
            if not isinstance(name, str):
                raise FormulaError("status_apply(eid, name, ...): name must be a string.")
            for label, v in (("level", level), ("duration", duration)):
                if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))):
                    raise FormulaError(f"status_apply(...): {label} must be a number.")
            lv = None if level is None else int(level)
            du = None if duration is None else int(duration)
            try:
                match.apply_status(eid, name, lv, du)
            except (VTTError, NotFound) as ex:
                raise FormulaError(str(ex))
            self._note_affected(eid)
            return True

        ns["status_has"]      = _status_has
        ns["status_has_path"] = _status_has_path
        ns["status_get"]      = _status_get
        ns["status_set"]      = _status_set
        ns["status_del"]      = _status_del
        ns["status_add"]      = _status_add
        ns["status_remove"]   = _status_remove
        ns["status_apply"]    = _status_apply

        # ---- introspection / runtime-path / query primitives ----
        # The pattern: entity[X].path needs a STATIC path at AST time.
        # The runtime-path accessors var_get/var_has/var_set/var_del
        # parallel status_*/tile_*, taking a string path computed at
        # runtime — which is what makes `for k in var_keys(self, ""):`
        # / `var_set(self, k, ...)` actually useful (you can iterate
        # whatever vars exist and touch them without knowing names).
        # All ALSO route writes through write_var so var hooks fire,
        # matching entity[X].path = value semantics exactly.

        engine = self  # capture for _note_affected calls below

        def _resolve_entity(token: Any, fname: str):
            """Resolve a token to (eid, Entity). Shared validator."""
            eid = _eid(token)
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"{fname}: unknown entity id '{eid}'.")
            return eid, e

        def _walk_vars(e, path: str, *, must_exist: bool):
            """Walk a dotted path into e.vars. Returns the value at the
            leaf when must_exist is True (raises on missing), or
            (parent_dict, leaf_key) when must_exist is False — useful
            for the set/del paths that need the parent reference."""
            if not isinstance(path, str):
                raise FormulaError("var path must be a string.")
            keys = path.split(".") if path else []
            cur: Any = e.vars
            for i, k in enumerate(keys):
                if not isinstance(cur, dict):
                    where = ".".join(keys[:i])
                    raise FormulaError(
                        f"`{e.id}`.{where} is "
                        f"{type(cur).__name__}, not a dict."
                    )
                if k not in cur:
                    if must_exist:
                        where = ".".join(keys[:i + 1])
                        raise FormulaError(
                            f"`{e.id}` has no var at '{where}'."
                        )
                    return None  # caller handles "doesn't exist"
                cur = cur[k]
            return cur

        def _var_keys(eid_t: Any, path: Any = "") -> list:
            """var_keys(eid, path=""): keys at a dotted vars path.
            Empty path = top-level var names. Non-dict at the path
            errors (you can't list keys of a scalar). Returns insertion
            order — formulas iterating this get a stable order."""
            _, e = _resolve_entity(eid_t, "var_keys")
            if not isinstance(path, str):
                raise FormulaError("var_keys(eid, path): path must be a string.")
            if not path:
                return list(e.vars.keys())
            v = _walk_vars(e, path, must_exist=True)
            if not isinstance(v, dict):
                raise FormulaError(
                    f"var_keys(`{e.id}`, '{path}'): not a dict "
                    f"({type(v).__name__})."
                )
            return list(v.keys())

        def _var_has(eid_t: Any, path: Any) -> bool:
            """var_has(eid, path): True iff the dotted vars path resolves
            on this entity. Off-grid / nested-into-scalar / missing all
            return False (no raise)."""
            if not isinstance(path, str) or not path:
                raise FormulaError("var_has(eid, path): path must be a non-empty string.")
            _, e = _resolve_entity(eid_t, "var_has")
            cur: Any = e.vars
            for k in path.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return False
                cur = cur[k]
            return True

        def _var_get(eid_t: Any, path: Any) -> Any:
            """var_get(eid, path): runtime-path equivalent of
            entity[eid].path. Raises on missing path (same semantics as
            entity[X].path reads)."""
            if not isinstance(path, str) or not path:
                raise FormulaError("var_get(eid, path): path must be a non-empty string.")
            _, e = _resolve_entity(eid_t, "var_get")
            return _walk_vars(e, path, must_exist=True)

        def _var_set(eid_t: Any, path: Any, value: Any) -> Any:
            """var_set(eid, path, value): runtime-path equivalent of
            entity[eid].path = value. Routes through Entity.write_var so
            var hooks fire — semantically identical to the static-path
            form, just with a computed path. Returns the written value."""
            if not isinstance(path, str) or not path:
                raise FormulaError("var_set(eid, path, value): path must be a non-empty string.")
            eid, e = _resolve_entity(eid_t, "var_set")
            # Mirror the positional-axis safety check in _write: x/y
            # are read-only here too. Reusing the same set keeps the
            # static and dynamic paths consistent.
            if path in self._POSITIONAL_PATHS:
                raise FormulaError(
                    f"var_set({eid!r}, '{path}', ...): `{path}` is "
                    f"read-only from formulas. Use `!ent tp` (or, once "
                    f"available, move_entity from formulas)."
                )
            e.write_var(path, value)
            engine._note_affected(eid)
            return value

        def _var_del(eid_t: Any, path: Any) -> bool:
            """var_del(eid, path): runtime-path equivalent of removing
            a var. Routes through Entity.remove_var so on_var_removed
            fires. Returns True iff the path existed (and was removed);
            False if absent (no error, no hook). Vital vars are
            engine-protected at the remove_var layer."""
            if not isinstance(path, str) or not path:
                raise FormulaError("var_del(eid, path): path must be a non-empty string.")
            eid, e = _resolve_entity(eid_t, "var_del")
            if not _var_has(eid_t, path):
                return False
            try:
                e.remove_var(path)
            except VTTError as ex:
                raise FormulaError(str(ex))
            engine._note_affected(eid)
            return True

        ns["var_keys"] = _var_keys
        ns["var_has"]  = _var_has
        ns["var_get"]  = _var_get
        ns["var_set"]  = _var_set
        ns["var_del"]  = _var_del

        # Match-level var accessors: the runtime-path twins of the
        # `match.<path>` static root (mirroring how var_* twins the
        # entity[X].path root). All operate on the single match-wide
        # vars dict; no entity id, no var hooks.
        def _match_var_keys(path: Any = "") -> list:
            """match_var_keys(path=""): keys at a dotted match-vars path.
            Empty path = top-level names. Non-dict at the path errors."""
            if not isinstance(path, str):
                raise FormulaError("match_var_keys(path): path must be a string.")
            if match is None:
                raise FormulaError("match vars are unavailable in this context.")
            if not path:
                return list(match.vars.keys())
            v = _get_path(match.vars, path)
            if not isinstance(v, dict):
                raise FormulaError(
                    f"match_var_keys('{path}'): not a dict ({type(v).__name__})."
                )
            return list(v.keys())

        def _match_var_has(path: Any) -> bool:
            """match_var_has(path): True iff the dotted match-vars path
            resolves. Missing / nested-into-scalar return False (no raise)."""
            if not isinstance(path, str) or not path:
                raise FormulaError("match_var_has(path): path must be a non-empty string.")
            if match is None:
                return False
            cur: Any = match.vars
            for k in path.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return False
                cur = cur[k]
            return True

        def _match_var_get(path: Any) -> Any:
            """match_var_get(path): runtime-path equivalent of match.path.
            Raises on a missing path (same as the static root)."""
            if not isinstance(path, str) or not path:
                raise FormulaError("match_var_get(path): path must be a non-empty string.")
            return self._read_match(path)

        def _match_var_set(path: Any, value: Any) -> Any:
            """match_var_set(path, value): runtime-path equivalent of
            match.path = value. Returns the written value."""
            if not isinstance(path, str) or not path:
                raise FormulaError("match_var_set(path, value): path must be a non-empty string.")
            return self._write_match(path, value)

        def _match_var_del(path: Any) -> bool:
            """match_var_del(path): remove a match var at a dotted path.
            Returns True iff it existed (and was removed); False if absent."""
            if not isinstance(path, str) or not path:
                raise FormulaError("match_var_del(path): path must be a non-empty string.")
            if match is None or not _match_var_has(path):
                return False
            keys = path.split(".")
            cur: Any = match.vars
            for k in keys[:-1]:
                cur = cur[k]
            del cur[keys[-1]]
            return True

        ns["match_var_keys"] = _match_var_keys
        ns["match_var_has"]  = _match_var_has
        ns["match_var_get"]  = _match_var_get
        ns["match_var_set"]  = _match_var_set
        ns["match_var_del"]  = _match_var_del

        def _var_has_key(eid_t: Any, path: Any, key: Any) -> bool:
            """var_has_key(eid, path, key): True iff the value at `path`
            is a dict that contains `key`. Tolerant — returns False if
            `path` is missing or resolves to a non-dict, never raises
            on shape (so a passive can guard `if var_has_key(self,
            'inventory', 'sword'): ...` without first checking that
            inventory exists). The companion to `var_has(eid, path)`:
            that one asks "does this path resolve at all", this one
            asks "is this path a dict with this specific child key"."""
            if not isinstance(path, str):
                raise FormulaError("var_has_key(eid, path, key): path must be a string.")
            if not isinstance(key, str) or not key:
                raise FormulaError(
                    "var_has_key(eid, path, key): key must be a "
                    "non-empty string."
                )
            _, e = _resolve_entity(eid_t, "var_has_key")
            cur: Any = e.vars
            if path:
                for seg in path.split("."):
                    if not isinstance(cur, dict) or seg not in cur:
                        return False
                    cur = cur[seg]
            return isinstance(cur, dict) and key in cur

        def _var_count(eid_t: Any, path: Any = "") -> int:
            """var_count(eid, path=""): number of immediate children at
            the dotted vars path. Empty path = number of top-level vars.
            Missing path or non-dict at path returns 0 (the read-side
            functions are tolerant — they answer "how many" not "is the
            shape valid")."""
            if not isinstance(path, str):
                raise FormulaError("var_count(eid, path): path must be a string.")
            _, e = _resolve_entity(eid_t, "var_count")
            cur: Any = e.vars
            if path:
                for seg in path.split("."):
                    if not isinstance(cur, dict) or seg not in cur:
                        return 0
                    cur = cur[seg]
            if not isinstance(cur, dict):
                return 0
            return len(cur)

        def _var_sum(eid_t: Any, path: Any = "") -> Any:
            """var_sum(eid, path=""): sum of the immediate-child numeric
            values at the dotted vars path. Non-numeric children (dicts,
            strings, bools, None) are skipped silently — the GM hasn't
            committed to all children being numbers, and skipping is the
            useful behavior for mixed-shape containers. Missing path or
            non-dict at path returns 0. Result is int if every contri-
            buting value was int, otherwise float (Python's natural
            sum() promotion)."""
            if not isinstance(path, str):
                raise FormulaError("var_sum(eid, path): path must be a string.")
            _, e = _resolve_entity(eid_t, "var_sum")
            cur: Any = e.vars
            if path:
                for seg in path.split("."):
                    if not isinstance(cur, dict) or seg not in cur:
                        return 0
                    cur = cur[seg]
            if not isinstance(cur, dict):
                return 0
            total: Any = 0
            for v in cur.values():
                # Bool is a subclass of int in Python; exclude it
                # explicitly so a True flag doesn't contribute 1 to an
                # inventory total. Strings and dicts are non-numeric.
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    total = total + v
            return total

        def _var_extremum_key(eid_t: Any, path: str, *,
                              fname: str, want_max: bool) -> Any:
            """Shared core for var_max_key / var_min_key. Returns the
            child key with the highest (or lowest) numeric value. Ties
            break to insertion order (first-seen wins) so successive
            calls are deterministic for a fixed dict. None for
            missing path / non-dict / no numeric children."""
            if not isinstance(path, str):
                raise FormulaError(f"{fname}(eid, path): path must be a string.")
            _, e = _resolve_entity(eid_t, fname)
            cur: Any = e.vars
            if path:
                for seg in path.split("."):
                    if not isinstance(cur, dict) or seg not in cur:
                        return None
                    cur = cur[seg]
            if not isinstance(cur, dict):
                return None
            best_key: Any = None
            best_val: Any = None
            for k, v in cur.items():
                if isinstance(v, bool):
                    continue
                if not isinstance(v, (int, float)):
                    continue
                if best_val is None or (
                    v > best_val if want_max else v < best_val
                ):
                    best_key = k
                    best_val = v
            return best_key

        def _var_max_key(eid_t: Any, path: Any = "") -> Any:
            """var_max_key(eid, path=""): the immediate-child key whose
            numeric value is largest. None if path missing, non-dict,
            or no numeric children. Ties break to insertion order."""
            return _var_extremum_key(eid_t, path, fname="var_max_key", want_max=True)

        def _var_min_key(eid_t: Any, path: Any = "") -> Any:
            """var_min_key(eid, path=""): the immediate-child key whose
            numeric value is smallest. None if path missing, non-dict,
            or no numeric children. Ties break to insertion order."""
            return _var_extremum_key(eid_t, path, fname="var_min_key", want_max=False)

        def _var_pick_random(eid_t: Any, path: Any = "") -> Any:
            """var_pick_random(eid, path=""): a randomly-chosen immediate-
            child key. None if path missing / non-dict / empty. Honors
            the random_seed rule the same way random_int does — when a
            seed is set, picks come from the match-bound seeded RNG so
            a session is reproducible."""
            if not isinstance(path, str):
                raise FormulaError("var_pick_random(eid, path): path must be a string.")
            _, e = _resolve_entity(eid_t, "var_pick_random")
            cur: Any = e.vars
            if path:
                for seg in path.split("."):
                    if not isinstance(cur, dict) or seg not in cur:
                        return None
                    cur = cur[seg]
            if not isinstance(cur, dict) or not cur:
                return None
            keys = list(cur.keys())
            rng = getattr(self._match, "_rng", None) if self._match else None
            if rng is not None:
                return rng.choice(keys)
            return random.choice(keys)

        def _var_clear(eid_t: Any, path: Any = "") -> int:
            """var_clear(eid, path=""): drop the contents at `path`.
            If `path` resolves to a dict, removes each immediate child
            (subtree removal — grandchildren fire on_var_removed too,
            same as remove_var elsewhere). The container itself is
            left in place as an empty dict, so `var_has(eid, path)`
            still returns True afterwards — the inventory still
            exists, it's just empty. If `path` is a leaf scalar, the
            var itself is removed (return 1). Missing path is a
            no-op (return 0). Empty path = clear ALL top-level vars,
            which the engine refuses on vital paths (initiative /
            team_var / hp_var) at the remove_var layer; those will
            simply be skipped and not counted as removed."""
            if not isinstance(path, str):
                raise FormulaError("var_clear(eid, path): path must be a string.")
            eid, e = _resolve_entity(eid_t, "var_clear")
            cur: Any = e.vars
            if path:
                for seg in path.split("."):
                    if not isinstance(cur, dict) or seg not in cur:
                        return 0
                    cur = cur[seg]
            if not isinstance(cur, dict):
                # Leaf scalar at `path` — drop the var itself.
                # Path is non-empty here (root is always a dict).
                try:
                    e.remove_var(path)
                except VTTError as ex:
                    raise FormulaError(str(ex))
                engine._note_affected(eid)
                return 1
            # Dict at `path`. Remove each immediate child individually
            # so on_var_removed fires per child (matching the natural
            # "you lost item X" event pattern). Iterate a snapshot of
            # keys so a passive that mutates vars during fire doesn't
            # invalidate the iterator.
            children = list(cur.keys())
            removed = 0
            for k in children:
                child_path = f"{path}.{k}" if path else k
                try:
                    e.remove_var(child_path)
                    removed += 1
                except (VTTError, NotFound):
                    # Vital-var protection (initiative / team / hp) or
                    # races with concurrent passive mutation: skip and
                    # keep going. The count reflects what we ACTUALLY
                    # removed, not what we tried to remove.
                    continue
            if removed:
                engine._note_affected(eid)
            return removed

        ns["var_has_key"]    = _var_has_key
        ns["var_count"]      = _var_count
        ns["var_sum"]        = _var_sum
        ns["var_max_key"]    = _var_max_key
        ns["var_min_key"]    = _var_min_key
        ns["var_pick_random"] = _var_pick_random
        ns["var_clear"]      = _var_clear

        def _status_names(eid_t: Any) -> list:
            """status_names(eid): names of active statuses, in insertion
            order. Companion to status_has; lets a formula iterate
            WHATEVER statuses exist (e.g. 'purge every debuff'),
            instead of checking hardcoded names."""
            _, e = _resolve_entity(eid_t, "status_names")
            return list(e.status.keys())
        ns["status_names"] = _status_names

        def _tile_keys(x: Any, y: Any) -> list:
            """tile_keys(x, y): top-level keys in the tile's data dict.
            Empty list when the tile has no data or is off-grid (no
            raise — symmetric with tile_has)."""
            if isinstance(x, bool) or not isinstance(x, int):
                raise FormulaError(f"tile_keys(x, y): x must be int, got {type(x).__name__}.")
            if isinstance(y, bool) or not isinstance(y, int):
                raise FormulaError(f"tile_keys(x, y): y must be int, got {type(y).__name__}.")
            data = match.tiles.get((x, y))
            if not isinstance(data, dict):
                return []
            return list(data.keys())
        ns["tile_keys"] = _tile_keys

        def _entity_groups(eid_t: Any) -> list:
            """entity_groups(eid): names of groups containing this
            entity, in Match.groups insertion order. The reverse-index
            companion to group_members."""
            eid, e = _resolve_entity(eid_t, "entity_groups")
            return [gname for gname, members in match.groups.items()
                    if eid in members]
        ns["entity_groups"] = _entity_groups

        def _has_action(eid_t: Any, name: Any) -> bool:
            """has_action(eid, name): True iff `eid` has at least one
            discoverable action with the given name (anywhere in its
            vars tree, subject to the action_container_* rules). The
            action-mode body language and passives can both use this
            to gate behaviour ("only fire if I can slice")."""
            if not isinstance(name, str) or not name:
                raise FormulaError("has_action(eid, name): name must be a non-empty string.")
            _, e = _resolve_entity(eid_t, "has_action")
            from action import discover_actions, lookup_action
            actions = discover_actions(e, match.rules)
            return len(lookup_action(actions, name, match.rules)) > 0

        def _entity_actions(eid_t: Any) -> list:
            """entity_actions(eid): the names of every discoverable
            action on the entity (insertion order over the vars walk;
            duplicate names appear once). The for-loop companion to
            has_action — iterate every action the entity could use."""
            _, e = _resolve_entity(eid_t, "entity_actions")
            from action import discover_actions
            actions = discover_actions(e, match.rules)
            return list(actions.keys())

        def _use_action(eid_t: Any, name: Any,
                        target_arg: Any = None,
                        args_arg: Any = None) -> bool:
            """use_action(eid, name, target=None, args=None): invoke an
            action on `eid` from inside another formula or action body.
            Returns True on a clean successful run, False if the action
            cleanly fail()ed. Unexpected errors propagate as
            FormulaError. Counts toward the action_recursion_limit."""
            if not isinstance(name, str) or not name:
                raise FormulaError("use_action(eid, name): name must be a non-empty string.")
            eid, e = _resolve_entity(eid_t, "use_action")
            from action import discover_actions, lookup_action, ActionFail
            actions = discover_actions(e, match.rules)
            matches = lookup_action(actions, name, match.rules)
            if not matches:
                raise FormulaError(
                    f"use_action: entity `{eid}` has no action `{name}`."
                )
            if len(matches) > 1:
                paths = ", ".join(f"`{a.full_path}`" for a in matches)
                raise FormulaError(
                    f"use_action: entity `{eid}` has multiple `{name}` "
                    f"actions ({paths}); use_action() doesn't disambiguate. "
                    f"Use the !action command for the menu, or make the "
                    f"action names unique."
                )
            action = matches[0]
            args_dict = {}
            if isinstance(args_arg, dict):
                args_dict = dict(args_arg)
            elif args_arg is not None:
                raise FormulaError(
                    f"use_action(args=...): args must be a dict, got "
                    f"{type(args_arg).__name__}."
                )
            # Resolve a synchronous-friendly path through the runner.
            # Because the formula engine is sync but run_action is
            # async, we drive the coroutine with the same trampoline
            # the action runner uses for cmd().
            from action import run_action, _sync_dispatch_returning
            # We need ctx and mgr; both are wired into FormulaEngine
            # only loosely. Best path: stash them on the match when
            # the outer action runner starts (already happens via the
            # ctx/mgr closure variables in cmd()) — for use_action
            # called from a passive or a top-level !eval there's no
            # outer runner, so we look up via the match's manager
            # attribute set up below.
            mgr_local = getattr(match, "_runtime_mgr", None)
            ctx_local = getattr(match, "_runtime_ctx", None)
            if mgr_local is None or ctx_local is None:
                raise FormulaError(
                    "use_action: no command context available — "
                    "this formula was evaluated outside an active "
                    "command dispatch (e.g. an internal !eval). "
                    "Call use_action from inside !action / !ent / a "
                    "passive that fires during command processing."
                )
            coro = run_action(
                action, actor_id=eid, target=target_arg,
                args=args_dict, match=match, mgr=mgr_local,
                ctx=ctx_local,
            )
            ok, _msg = _sync_dispatch_returning(coro)
            return ok

        ns["has_action"]     = _has_action
        ns["entity_actions"] = _entity_actions
        ns["use_action"]     = _use_action

        # ---- summon / despawn family --------------------------------
        # A template is an entity-shaped dict living in ordinary
        # storage (vars / tile data). These primitives turn one into a
        # live entity (or snapshot a live entity back into a template).
        # All route through Match.summon_entity, so the
        # summon_event_limit guard and turn-order/on_entity_spawned
        # wiring are shared.
        def _entity_snapshot(eid_t: Any) -> dict:
            """entity_snapshot(eid): capture an entity into a summon-
            ready template dict (id and position stripped). Pair with
            var_set / tile_set to store it, then summon / summon_from
            to instantiate copies."""
            _, e = _resolve_entity(eid_t, "entity_snapshot")
            return match.entity_template_dict(e)

        def _summon(template: Any, x: Any, y: Any, id_prefix: Any = None) -> str:
            """summon(template, x, y, id_prefix=None): instantiate a
            live entity from a template dict at exactly (x, y). Raises
            (off-grid / occupied) if the cell isn't free — use
            summon_near to ring-search. Returns the new entity id.
            id_prefix overrides the auto-derived id base (template
            name, else 'summon'); the engine appends a counter to keep
            ids unique."""
            prefix = None if id_prefix is None else str(id_prefix)
            try:
                new_id, _log = match.summon_entity(
                    template, x, y, id_prefix=prefix,
                )
            except (VTTError, NotFound) as ex:
                raise FormulaError(str(ex))
            engine._note_affected(new_id)
            return new_id

        def _summon_near(template: Any, x: Any, y: Any, radius: Any,
                         id_prefix: Any = None) -> str:
            """summon_near(template, x, y, radius, id_prefix=None): like
            summon, but searches outward (Chebyshev rings, nearest
            first) up to `radius` for the first free in-bounds cell.
            Raises only if no free cell is found in range. Ideal for
            'spawn a minion adjacent to me' (radius 1)."""
            if not isinstance(radius, int) or isinstance(radius, bool):
                raise FormulaError("summon_near: radius must be an int.")
            prefix = None if id_prefix is None else str(id_prefix)
            try:
                new_id, _log = match.summon_entity(
                    template, x, y, id_prefix=prefix, near_radius=radius,
                )
            except (VTTError, NotFound) as ex:
                raise FormulaError(str(ex))
            engine._note_affected(new_id)
            return new_id

        def _summon_from(path: Any, x: Any, y: Any, id_prefix: Any = None) -> str:
            """summon_from(path, x, y, id_prefix=None): read a template
            from `self`'s vars at the dotted `path`, then summon it
            (strict placement). Convenience for the common case where
            the summoner stores its blueprint in its own vars — e.g. a
            boss with `minion_template` in vars calls
            `summon_from('minion_template', x, y)`. For templates in a
            tile or another entity, fetch with tile_get / var_get and
            use summon(...) directly."""
            if not isinstance(path, str) or not path:
                raise FormulaError("summon_from: path must be a non-empty string.")
            self_id = ctx.target
            if self_id is None:
                raise FormulaError(
                    "summon_from: no `self` entity bound — call it from a "
                    "passive / action / `!eval --as <eid>` so `self` "
                    "resolves the vars owner."
                )
            e = match.entities.get(self_id)
            if e is None:
                raise FormulaError(f"summon_from: entity '{self_id}' not found.")
            cur: Any = e.vars
            for seg in path.split("."):
                if not isinstance(cur, dict) or seg not in cur:
                    raise FormulaError(
                        f"summon_from: `{self_id}` has no template at "
                        f"vars '{path}'."
                    )
                cur = cur[seg]
            return _summon(cur, x, y, id_prefix)

        def _remove_entity(eid_t: Any) -> bool:
            """remove_entity(eid): despawn an entity — the complement of
            summon. Removes it from the match, turn order, and any
            groups. Returns True. Use for 'minion expires after N
            turns', 'banish', cleanup of summoned waves. Raises if the
            id isn't a live entity."""
            eid, e = _resolve_entity(eid_t, "remove_entity")
            e.remove()
            return True

        ns["entity_snapshot"] = _entity_snapshot
        ns["summon"]          = _summon
        ns["summon_near"]     = _summon_near
        ns["summon_from"]     = _summon_from
        ns["remove_entity"]   = _remove_entity

        # ---- death / corpse family ---------------------------------
        # All death/corpse mutations route through Match.check_death /
        # Match.revive_corpse so on_death / on_revive fire consistently
        # and corpse tile-data stays the authoritative position store.
        def _kill(eid_t: Any) -> bool:
            """kill(eid): trigger death on `eid` unconditionally. Used
            for instant-kill effects, abilities that bypass hp, and
            'cleanup' patterns.

            Pipeline:
              1. Run `default_kill_function_effects` (default
                 `entity[self].hp = 0`) on the target — so the corpse
                 reads naturally (no 99/99hp corpses) and any GM-
                 configured pre-death effects fire. Skipped if the
                 rule is empty.
              2. Call Match._process_death unconditionally — fires
                 on_death, stores corpse (or deletes per
                 death_result), removes from turn order. Bypasses the
                 death CONDITION so kill works regardless of what the
                 GM configured.

            Returns True if the entity was present and got killed,
            False if there was no such entity to begin with."""
            # Effects-formula run + unconditional death pipeline live in
            # Match.kill_entity (shared with the !ent kill command).
            killed, _log = match.kill_entity(_eid(eid_t))
            return killed

        def _revive(eid_t: Any) -> str:
            """revive(eid): bring back the corpse with id `eid`. Spawns
            a fresh entity from the stored snapshot at the corpse's
            tile coords, removes the corpse, and fires on_revive on
            the restored entity. Hp is restored to max_hp; per-revive
            customization belongs in an on_revive passive. Returns
            the revived entity id. Raises if no such corpse exists."""
            eid = str(_eid(eid_t))
            try:
                new_id, _log = match.revive_corpse(eid)
            except (VTTError, NotFound, OutOfBounds, Occupied) as ex:
                raise FormulaError(str(ex))
            engine._note_affected(new_id)
            return new_id

        def _has_corpse(eid_t: Any) -> bool:
            """has_corpse(eid): True iff a corpse with id `eid` exists
            in this match. The corpse-side companion to has_action /
            has_status — useful for conditional revives, "necromancer
            harvests bones" patterns, etc."""
            return match.find_corpse(str(_eid(eid_t))) is not None

        def _corpse_at(eid_t: Any) -> tuple:
            """corpse_at(eid): the (x, y) tile coordinates of the
            corpse with id `eid`. Authoritative source (tile location,
            never the embedded snapshot). Raises if no such corpse."""
            eid = str(_eid(eid_t))
            loc = match.find_corpse(eid)
            if loc is None:
                raise FormulaError(f"corpse_at: no corpse with id '{eid}'.")
            x, y, _ = loc
            return (x, y)

        def _all_corpses() -> list:
            """all_corpses(): list of corpse ids in the match (insertion
            order by tile, then by id). Loopable — `for cid in
            all_corpses(): if condition: revive(cid)`."""
            return [eid for (_x, _y, eid, _c) in match.all_corpses()]

        # ---- corpse var introspection ----
        # A corpse is a stored snapshot (corpse["entity"] = Entity.to_dict
        # at death). These read the DEAD entity's frozen vars by dotted
        # path — the loot / "raise with the same statline" / "was it
        # carrying the key?" patterns. Read-only: a corpse's snapshot is
        # immutable until revive. Mirror var_get / var_has semantics
        # (raise on missing for the getter unless a default is supplied;
        # bool for the has-check). Status is intentionally NOT exposed yet.
        _corpse_no_default = object()

        def _corpse_vars(eid: str):
            """The dead entity's stored vars dict, or None if no such
            corpse (an empty dict if the corpse has no vars)."""
            loc = match.find_corpse(eid)
            if loc is None:
                return None
            _x, _y, corpse = loc
            ent = corpse.get("entity") if isinstance(corpse, dict) else None
            cv = ent.get("vars") if isinstance(ent, dict) else None
            return cv if isinstance(cv, dict) else {}

        def _corpse_has(eid_t: Any, path: Any) -> bool:
            """corpse_has(eid, path): True iff the dotted vars path
            resolves on the corpse's stored snapshot. False on a missing
            corpse / missing path / nesting into a scalar (no raise)."""
            if not isinstance(path, str) or not path:
                raise FormulaError("corpse_has(eid, path): path must be a non-empty string.")
            cv = _corpse_vars(str(_eid(eid_t)))
            if cv is None:
                return False
            cur: Any = cv
            for k in path.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    return False
                cur = cur[k]
            return True

        def _corpse_var(eid_t: Any, path: Any,
                        default: Any = _corpse_no_default) -> Any:
            """corpse_var(eid, path[, default]): read the dead entity's
            stored var at a dotted path (the corpse-snapshot equivalent of
            var_get). Raises if the corpse or the path is missing, UNLESS a
            `default` is supplied, in which case the default is returned."""
            if not isinstance(path, str) or not path:
                raise FormulaError("corpse_var(eid, path[, default]): path must be a non-empty string.")
            eid = str(_eid(eid_t))
            cv = _corpse_vars(eid)
            if cv is None:
                if default is _corpse_no_default:
                    raise FormulaError(f"corpse_var: no corpse with id '{eid}'.")
                return default
            cur: Any = cv
            for k in path.split("."):
                if not isinstance(cur, dict) or k not in cur:
                    if default is _corpse_no_default:
                        raise FormulaError(
                            f"corpse_var: corpse '{eid}' has no value at '{path}'.")
                    return default
                cur = cur[k]
            return cur

        ns["kill"]         = _kill
        ns["revive"]       = _revive
        ns["has_corpse"]   = _has_corpse
        ns["corpse_at"]    = _corpse_at
        ns["all_corpses"]  = _all_corpses
        ns["corpse_has"]   = _corpse_has
        ns["corpse_var"]   = _corpse_var

        def _schedule(delay: Any, body: Any, name: Any = None) -> str:
            """schedule(delay, body, name=None): queue a MATCH-level
            effect to run at on_round_start `delay` rounds from now
            (delay>=1; delay=1 fires next round). The body string runs
            with NO `self` bound — use explicit ids or `this`. Returns
            the schedule name (generated if name is falsy); cancel via
            cancel_schedule(name)."""
            nm = name if isinstance(name, str) and name else None
            try:
                return match.add_scheduled(delay, body, nm)
            except VTTError as ex:
                raise FormulaError(str(ex))

        def _schedule_on(eid_t: Any, delay: Any, body: Any,
                         name: Any = None) -> str:
            """schedule_on(eid, delay, body, name=None): queue an
            entity-attached effect to run at eid's on_turn_start after
            `delay` of its turns (delay>=1; delay=1 fires its next turn).
            The body runs with `self`=eid. Auto-dropped if the entity
            dies/despawns before firing. Returns the schedule name."""
            eid = _eid(eid_t)
            nm = name if isinstance(name, str) and name else None
            try:
                return match.add_scheduled_on(eid, delay, body, nm)
            except VTTError as ex:
                raise FormulaError(str(ex))

        def _cancel_schedule(name: Any) -> int:
            """cancel_schedule(name): remove every pending schedule with
            this name. Returns the number removed (0 if none matched)."""
            if not isinstance(name, str):
                raise FormulaError("cancel_schedule(name): name must be a string.")
            return match.cancel_scheduled(name)

        ns["schedule"]        = _schedule
        ns["schedule_on"]     = _schedule_on
        ns["cancel_schedule"] = _cancel_schedule

        def _log(message: Any) -> bool:
            """log(message): append a custom entry to the match's event
            log (the combat log). The message is stringified. Always
            recorded when event_log_enabled (the `custom` type is not
            gated by the event_log_events list). Returns True if it was
            recorded, False if logging is disabled. View with !log; the
            rendered text uses the event_log_format `custom` template
            (default '{message}')."""
            return match.log_event("custom", message=_stringify(message))

        ns["log"] = _log

        # ---- zone primitives ----
        # A zone is a named region (a set of cells) with free-form data
        # and hooks. These mirror the tile_* accessors plus membership
        # queries and footprint mutation. Coords are validated by the
        # Match methods; errors surface as FormulaError.
        def _zone_name(v: Any, fname: str) -> str:
            if not isinstance(v, str) or not v:
                raise FormulaError(f"{fname}: zone name must be a non-empty string.")
            return v

        def _zone_xy(x: Any, y: Any, fname: str) -> Tuple[int, int]:
            if (not isinstance(x, int) or isinstance(x, bool)
                    or not isinstance(y, int) or isinstance(y, bool)):
                raise FormulaError(f"{fname}: x and y must be integers.")
            return x, y

        def _zone_exists(name: Any) -> bool:
            """zone_exists(name): True iff a zone with this name exists."""
            return match.has_zone(_zone_name(name, "zone_exists"))

        def _zones_at(x: Any, y: Any) -> list:
            """zones_at(x, y): list of zone names covering cell (x,y)
            (sorted; loopable)."""
            xi, yi = _zone_xy(x, y, "zones_at")
            return sorted(match.zones_at(xi, yi))

        def _cell_in_zone(x: Any, y: Any, name: Any) -> bool:
            """cell_in_zone(x, y, name): True iff (x,y) is in the zone."""
            xi, yi = _zone_xy(x, y, "cell_in_zone")
            return match.cell_in_zone(xi, yi, _zone_name(name, "cell_in_zone"))

        def _in_zone(eid_t: Any, name: Any) -> bool:
            """in_zone(eid, name): True iff the entity's current cell is
            in the zone."""
            eid = _eid(eid_t)
            return name in match.entity_zones(eid) if isinstance(name, str) else False

        def _entity_zones(eid_t: Any) -> list:
            """entity_zones(eid): zone names the entity currently stands
            in (sorted; loopable)."""
            return sorted(match.entity_zones(_eid(eid_t)))

        def _entities_in_zone(name: Any) -> list:
            """entities_in_zone(name): alive entity ids standing in the
            zone (insertion order; loopable)."""
            return match.entities_in_zone(_zone_name(name, "entities_in_zone"))

        def _zone_cells(name: Any) -> list:
            """zone_cells(name): the zone's cells as [x,y] pairs (sorted;
            loop with `for (cx,cy) in zone_cells(name)`)."""
            return match.zone_cell_list(_zone_name(name, "zone_cells"))

        def _zone_names() -> list:
            """zone_names(): every zone name (sorted; loopable)."""
            return sorted(match.zones.keys())

        def _zone_size(name: Any) -> int:
            """zone_size(name): number of cells in the zone (0 if it
            doesn't exist)."""
            z = match.zones.get(_zone_name(name, "zone_size"))
            return len(z["cells"]) if z is not None else 0

        def _zone_get(name: Any, path: Any) -> Any:
            """zone_get(name, path): read the zone's data at a dotted
            path. Raises on a missing zone or path."""
            if not isinstance(path, str) or not path:
                raise FormulaError("zone_get(name, path): path must be a non-empty string.")
            try:
                return match.zone_get_path(_zone_name(name, "zone_get"), path)
            except VTTError as ex:
                raise FormulaError(str(ex))

        def _zone_has(name: Any, path: Any) -> bool:
            """zone_has(name, path): True iff the dotted data path
            resolves on the zone (False on a missing zone)."""
            if not isinstance(path, str) or not path:
                raise FormulaError("zone_has(name, path): path must be a non-empty string.")
            return match.zone_has_path(_zone_name(name, "zone_has"), path)

        def _zone_keys(name: Any, path: Any = "") -> list:
            """zone_keys(name, path=""): keys at a dotted data path (""
            = top-level). Non-dict at the path errors."""
            zn = _zone_name(name, "zone_keys")
            if not isinstance(path, str):
                raise FormulaError("zone_keys(name, path): path must be a string.")
            z = match.zones.get(zn)
            if z is None:
                raise FormulaError(f"zone_keys: zone '{zn}' not found.")
            cur: Any = z["data"]
            if path:
                try:
                    cur = match.zone_get_path(zn, path)
                except VTTError as ex:
                    raise FormulaError(str(ex))
            if not isinstance(cur, dict):
                raise FormulaError(
                    f"zone_keys('{zn}', '{path}'): not a dict "
                    f"({type(cur).__name__})."
                )
            return list(cur.keys())

        def _zone_set(name: Any, path: Any, value: Any) -> Any:
            """zone_set(name, path, value): write the zone's data at a
            dotted path (creates the zone + intermediates). Returns the
            written value."""
            if not isinstance(path, str) or not path:
                raise FormulaError("zone_set(name, path, value): path must be a non-empty string.")
            try:
                match.zone_set_path(_zone_name(name, "zone_set"), path, value)
            except VTTError as ex:
                raise FormulaError(str(ex))
            return value

        def _zone_del(name: Any, path: Any) -> bool:
            """zone_del(name, path): remove a dotted data key. Returns
            True iff it existed, False if absent (no error)."""
            if not isinstance(path, str) or not path:
                raise FormulaError("zone_del(name, path): path must be a non-empty string.")
            zn = _zone_name(name, "zone_del")
            if not match.zone_has_path(zn, path):
                return False
            try:
                match.zone_del_path(zn, path)
            except VTTError as ex:
                raise FormulaError(str(ex))
            return True

        def _create_zone(name: Any) -> bool:
            """create_zone(name): make an empty zone. Returns True if
            created, False if it already existed."""
            zn = _zone_name(name, "create_zone")
            if match.has_zone(zn):
                return False
            match.create_zone(zn)
            return True

        def _delete_zone(name: Any) -> bool:
            """delete_zone(name): remove a zone entirely. Returns True iff
            it existed."""
            zn = _zone_name(name, "delete_zone")
            if not match.has_zone(zn):
                return False
            match.delete_zone(zn)
            return True

        def _zone_add_cell(name: Any, x: Any, y: Any) -> bool:
            """zone_add_cell(name, x, y): add a cell (creating the zone if
            needed). Returns True iff newly added. Off-grid raises."""
            xi, yi = _zone_xy(x, y, "zone_add_cell")
            try:
                return match.zone_add_cell(_zone_name(name, "zone_add_cell"), xi, yi)
            except VTTError as ex:
                raise FormulaError(str(ex))

        def _zone_remove_cell(name: Any, x: Any, y: Any) -> bool:
            """zone_remove_cell(name, x, y): drop a cell. Returns True iff
            it was present."""
            xi, yi = _zone_xy(x, y, "zone_remove_cell")
            try:
                return match.zone_remove_cell(_zone_name(name, "zone_remove_cell"), xi, yi)
            except VTTError as ex:
                raise FormulaError(str(ex))

        def _zone_fill(name: Any, x1: Any, y1: Any, x2: Any, y2: Any) -> int:
            """zone_fill(name, x1, y1, x2, y2): add every in-bounds cell of
            the rectangle (creating the zone if needed). Returns the count
            newly added."""
            ax, ay = _zone_xy(x1, y1, "zone_fill")
            bx, by = _zone_xy(x2, y2, "zone_fill")
            try:
                return match.zone_fill_rect(_zone_name(name, "zone_fill"), ax, ay, bx, by)
            except VTTError as ex:
                raise FormulaError(str(ex))

        def _zone_shift(name: Any, dx: Any, dy: Any) -> int:
            """zone_shift(name, dx, dy): translate the whole zone by
            (dx,dy); cells pushed off-grid are dropped. Returns the cell
            count retained. The 'gas cloud drifts' primitive."""
            ddx, ddy = _zone_xy(dx, dy, "zone_shift")
            try:
                return match.zone_shift(_zone_name(name, "zone_shift"), ddx, ddy)
            except VTTError as ex:
                raise FormulaError(str(ex))

        def _zone_anchor(name: Any, eid: Any, radius: Any = 0,
                         metric: Any = "square_radius") -> int:
            """zone_anchor(name, eid, radius=0, metric="square_radius"):
            bind a zone to an entity as an AURA and stamp its cells now (a
            footprint-aware disc of `radius` around `eid`). The cells
            re-stamp whenever the anchor moves. radius 0 = exactly the
            anchor's footprint. Returns the cell count. The 'burning aura'
            / 'commander's banner' primitive."""
            if isinstance(radius, bool) or not isinstance(radius, (int, float)):
                raise FormulaError("zone_anchor(...): radius must be a number.")
            try:
                return match.anchor_zone(
                    _zone_name(name, "zone_anchor"), _eid(eid),
                    int(radius), str(metric))
            except (VTTError, NotFound) as ex:
                raise FormulaError(str(ex))

        def _zone_unanchor(name: Any) -> bool:
            """zone_unanchor(name): detach an aura's anchor, leaving its
            current cells as a static zone. Returns True iff it was
            anchored."""
            try:
                return match.unanchor_zone(_zone_name(name, "zone_unanchor"))
            except (VTTError, NotFound) as ex:
                raise FormulaError(str(ex))

        def _zone_anchor_of(name: Any) -> str:
            """zone_anchor_of(name): the entity id a zone is anchored to,
            or '' if it isn't an aura (or doesn't exist)."""
            z = match.zones.get(_zone_name(name, "zone_anchor_of"))
            anc = z.get("anchor") if isinstance(z, dict) else None
            return str(anc) if anc else ""

        ns["zone_anchor"]       = _zone_anchor
        ns["zone_unanchor"]     = _zone_unanchor
        ns["zone_anchor_of"]    = _zone_anchor_of
        ns["zone_exists"]       = _zone_exists
        ns["zones_at"]          = _zones_at
        ns["cell_in_zone"]      = _cell_in_zone
        ns["in_zone"]           = _in_zone
        ns["entity_zones"]      = _entity_zones
        ns["entities_in_zone"]  = _entities_in_zone
        ns["zone_cells"]        = _zone_cells
        ns["zone_names"]        = _zone_names
        ns["zone_size"]         = _zone_size
        ns["zone_get"]          = _zone_get
        ns["zone_has"]          = _zone_has
        ns["zone_keys"]         = _zone_keys
        ns["zone_set"]          = _zone_set
        ns["zone_del"]          = _zone_del
        ns["create_zone"]       = _create_zone
        ns["delete_zone"]       = _delete_zone
        ns["zone_add_cell"]     = _zone_add_cell
        ns["zone_remove_cell"]  = _zone_remove_cell
        ns["zone_fill"]         = _zone_fill
        ns["zone_shift"]        = _zone_shift

        def _rule_get(name: Any) -> Any:
            """rule_get(name): the effective value of a system rule on
            this match. Returns None for unknown rules (so formulas can
            test `rule_get('friendlyfire')` without first checking
            existence)."""
            if not isinstance(name, str):
                raise FormulaError("rule_get(name): name must be a string.")
            return match.rules.get(name)
        ns["rule_get"] = _rule_get

        def _round_number() -> int:
            """round_number(): the match's current round number (1-based;
            starts at 1, increments each time turn order wraps back to
            the first entity). The time primitive for cadence checks —
            'every 3 rounds', 'after round 5', cooldowns measured in
            rounds. During an on_round_start hook it already reflects
            the round just begun. Example: a tile pulse that fires on
            multiples of N -> `if round_number() % 3 == 0: ...`."""
            return int(match.round_number)
        ns["round_number"] = _round_number

        def _turn_index() -> int:
            """turn_index(): the 0-based position of the acting entity
            within the current round's turn order (match.active_index).
            Companion to round_number for finer-grained timing — e.g.
            'only on the first turn of the round' is `turn_index() ==
            0`. Returns 0 when turn order is empty."""
            return int(match.active_index)
        ns["turn_index"] = _turn_index

        def _all_entities() -> list:
            """all_entities(): every ALIVE, independent entity id, in
            match.entities insertion order. The match-wide iteration
            primitive (no reference entity required). Attached body parts
            are excluded (they're not field units — reach them with
            parts(parent) / part(parent, name))."""
            return [eid for eid, e in match.entities.items()
                    if e.is_alive and not e.is_glued_part]
        ns["all_entities"] = _all_entities

        def _parts(eid_token: Any) -> list:
            """parts(eid): the ids of every body part attached to `eid`,
            in order (loopable). Empty if it has none. Includes 0/0
            indestructible zones (which are 'not alive')."""
            eid, _e = _resolve_entity(eid_token, "parts")
            return [p.id for p in match.entity_parts(eid)]
        ns["parts"] = _parts

        def _part(parent_token: Any, handle: Any) -> str:
            """part(parent, name_or_id): the id of `parent`'s body part
            matching `name_or_id` — its part_name var OR its entity id (id
            wins on a clash). Raises if there's no such part."""
            pid, _pe = _resolve_entity(parent_token, "part")
            if not isinstance(handle, str) or not handle:
                raise FormulaError("part(parent, name): name must be a non-empty string.")
            p = match.find_part(pid, handle)
            if p is None:
                raise FormulaError(
                    f"part: `{pid}` has no body part `{handle}`.")
            return p.id
        ns["part"] = _part

        def _has_part(parent_token: Any, handle: Any) -> bool:
            """has_part(parent, name_or_id): True iff `parent` has a body
            part matching name_or_id. Never raises."""
            try:
                pid, _pe = _resolve_entity(parent_token, "has_part")
            except FormulaError:
                return False
            if not isinstance(handle, str) or not handle:
                return False
            return match.find_part(pid, handle) is not None
        ns["has_part"] = _has_part

        def _damage_part(part_token: Any, amount: Any) -> int:
            """damage_part(part, amount): deal `amount` to a body part,
            routing the configured share to the parent's main HP (the
            HD2 'damage to main' model — see the part_to_main_* rules and
            per-part to_main_percent / to_main_cap / vital vars). Returns
            the amount dealt to the parent's main HP. Destroying a part
            fires its on_death (destroy effects) and, if `vital`, kills
            the parent. Routing happens ONLY through this verb — a raw
            entity[part].hp write does not spill to main."""
            pid, _pe = _resolve_entity(part_token, "damage_part")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                raise FormulaError("damage_part(part, amount): amount must be a number.")
            to_main, _log = match.damage_part(pid, int(amount))
            return to_main
        ns["damage_part"] = _damage_part

        def _damage_spread(target_token: Any, total: Any, mode: Any = None,
                           fragments: Any = None) -> int:
            """damage_spread(target, total, mode=None, fragments=None):
            distribute `total` area damage across the target's body parts,
            returning the amount that reached the parent's main HP. The total
            is DIVIDED among parts (each routed via damage_part), never dealt
            in full to each. Modes (default aoe_default_mode): weighted /
            uniform / fragment / main_only — see the aoe_* rules and per-part
            aoe_weight var. A target with no parts takes the full total to
            main. Loop entities_in_area(...) and call this per entity for a
            blast; compute per-entity falloff yourself."""
            tid, _te = _resolve_entity(target_token, "damage_spread")
            if isinstance(total, bool) or not isinstance(total, (int, float)):
                raise FormulaError("damage_spread(...): total must be a number.")
            m = None if mode is None else str(mode)
            f = None
            if fragments is not None:
                if isinstance(fragments, bool) or not isinstance(fragments, (int, float)):
                    raise FormulaError("damage_spread(...): fragments must be a number.")
                f = int(fragments)
            to_main, _log = match.damage_spread(tid, int(total), m, f)
            return to_main
        ns["damage_spread"] = _damage_spread

        def _part_of(eid_token: Any) -> str:
            """part_of(eid): the parent entity id if `eid` is a body part,
            else '' (empty string). The query analog of the `parent`
            token — usable on any entity, no raise."""
            eid, e = _resolve_entity(eid_token, "part_of")
            po = e.part_of
            return po if (po and po in match.entities) else ""
        ns["part_of"] = _part_of

        # ---- stat modifiers (derived / effective stats) ------------------
        def _norm_mod_tags(tags: Any, fname: str) -> list:
            if tags is None:
                return []
            if isinstance(tags, str):
                return [tags]
            if isinstance(tags, (list, tuple)):
                return [str(t) for t in tags]
            raise FormulaError(
                f"{fname}(...): tags must be a string or a list of strings.")

        def _mod_context(target: Any, attacker: Any, defender: Any,
                         other: Any) -> dict:
            ctx: dict = {}
            if target is not None:
                ctx["target"] = target
            if attacker is not None:
                ctx["attacker"] = attacker
            if defender is not None:
                ctx["defender"] = defender
            if other is not None:
                ctx["other"] = other
            return ctx

        def _apply_mods(entity_token: Any, stat: Any, base: Any,
                        tags: Any = None, target: Any = None,
                        attacker: Any = None, defender: Any = None,
                        other: Any = None) -> float:
            """apply_mods(entity, stat, base, tags=None, target=, attacker=,
            defender=, other=): the effective value of `base` after the
            entity's modifiers for `stat` + `tags` (the HD-style derived
            stat). Modifiers come live from statuses / a direct
            `entity.modifiers` slot / scanned containers (e.g. equipped);
            each may carry a condition / value formula that reads `self`
            (the owner) plus the context entities passed here (e.g.
            condition `entity[target].undead` for '+X vs undead'). Folds
            per the modifier_op_priority / _order rules."""
            eid, _e = _resolve_entity(entity_token, "apply_mods")
            if not isinstance(stat, str) or not stat:
                raise FormulaError("apply_mods(...): stat must be a non-empty string.")
            if isinstance(base, bool) or not isinstance(base, (int, float)):
                raise FormulaError("apply_mods(...): base must be a number.")
            return match.apply_modifiers(
                eid, stat, base, _norm_mod_tags(tags, "apply_mods"),
                _mod_context(target, attacker, defender, other))
        ns["apply_mods"] = _apply_mods

        def _list_mods(entity_token: Any, stat: Any, tags: Any = None,
                       target: Any = None, attacker: Any = None,
                       defender: Any = None, other: Any = None) -> list:
            """list_mods(entity, stat, tags=None, target=, attacker=,
            defender=, other=): the active modifier records for stat+tags
            (same filtering as apply_mods), for introspection / breakdowns.
            Mostly useful at the command layer or via len() ('is any X
            modifier active?')."""
            eid, _e = _resolve_entity(entity_token, "list_mods")
            if not isinstance(stat, str) or not stat:
                raise FormulaError("list_mods(...): stat must be a non-empty string.")
            return match.gather_modifiers(
                eid, stat, _norm_mod_tags(tags, "list_mods"),
                _mod_context(target, attacker, defender, other))
        ns["list_mods"] = _list_mods

        def _entities_with_status(name: Any) -> list:
            """entities_with_status(name): alive entities that carry the
            named status, insertion order."""
            if not isinstance(name, str):
                raise FormulaError("entities_with_status(name): name must be a string.")
            return [eid for eid, e in match.entities.items()
                    if e.is_alive and name in e.status]
        ns["entities_with_status"] = _entities_with_status

        def _entities_with_var(path: Any) -> list:
            """entities_with_var(path): alive entities for which the
            dotted vars path resolves. Lets a formula find every
            "blessed" entity without hardcoding which ones can be."""
            if not isinstance(path, str) or not path:
                raise FormulaError("entities_with_var(path): path must be a non-empty string.")
            keys = path.split(".")
            out = []
            for eid, e in match.entities.items():
                if not e.is_alive:
                    continue
                cur: Any = e.vars
                ok = True
                for k in keys:
                    if not isinstance(cur, dict) or k not in cur:
                        ok = False
                        break
                    cur = cur[k]
                if ok:
                    out.append(eid)
            return out
        ns["entities_with_var"] = _entities_with_var

        def _entities_in_area(x: Any, y: Any, n: Any,
                              mode: Any = "square_radius_distance") -> list:
            """entities_in_area(x, y, n, mode): coord-rooted version of
            entities_within. Returns alive entity ids whose position is
            within distance n of the POINT (x, y), sorted by
            (distance, id). Use when the area is centered on a tile
            (e.g. AOE spell impact) instead of on an entity."""
            # Validate the same way distance() does — pass through and
            # let it raise if the args are bad.
            scored = []
            for eid, e in match.entities.items():
                if not e.is_alive or e.is_glued_part:
                    continue
                d = _distance(x, y, e.x, e.y, mode)
                if d <= n:
                    scored.append((d, eid))
            scored.sort(key=lambda t: (t[0], t[1]))
            return [eid for _, eid in scored]
        ns["entities_in_area"] = _entities_in_area

        # Shape-rooted entity queries: the entity-returning twins of the
        # cells_in_* helpers. Each builds the shape's cells then collects
        # the alive entities standing on them.
        def _alive_at(cellset):
            # Footprint-aware membership: a large entity is "in" the shape
            # if ANY cell it covers is in cellset (listed once, by anchor).
            return [(e.x, e.y, eid) for eid, e in match.entities.items()
                    if e.is_alive
                    and any(c in cellset for c in match.entity_cells(e))]

        def _occupants() -> dict:
            # Footprint-aware: a large entity registers under EVERY cell it
            # covers, so it's "on the line" / "in the way" if any part of
            # its body is. Consumers dedupe across cells.
            here: dict = {}
            for eid, e in match.entities.items():
                if e.is_alive:
                    for c in match.entity_cells(e):
                        here.setdefault(c, []).append(eid)
            return here

        def _entities_in_line_ignorelos(x1: Any, y1: Any,
                                        x2: Any, y2: Any) -> list:
            """entities_in_line_ignorelos(x1, y1, x2, y2): alive entity ids
            on the geometric line from (x1,y1) to (x2,y2), INCLUDING the
            endpoints, ordered near->far (same line walk as has_los, but
            opacity is ignored — walls don't stop the count). A large entity
            counts if any footprint cell is on the line (listed once, at its
            nearest cell). For the sight-aware version that stops at terrain
            and skips the shooter and target, use entities_on_los."""
            cx1 = _coord_int(x1, "entities_in_line_ignorelos", "x1")
            cy1 = _coord_int(y1, "entities_in_line_ignorelos", "y1")
            cx2 = _coord_int(x2, "entities_in_line_ignorelos", "x2")
            cy2 = _coord_int(y2, "entities_in_line_ignorelos", "y2")
            here = _occupants()
            out: list = []
            seen: set = set()
            for cell in match._line_cells(cx1, cy1, cx2, cy2):
                for eid in sorted(here.get(cell, ())):
                    if eid not in seen:
                        seen.add(eid)
                        out.append(eid)
            return out

        def _entities_on_los(x1: Any, y1: Any, x2: Any, y2: Any,
                             viewer: Any = None) -> list:
            """entities_on_los(x1, y1, x2, y2, viewer=None): alive entity ids
            STRICTLY BETWEEN (x1,y1) and (x2,y2) that the viewer has line of
            sight to (terrain past an opaque cell is cut off), ordered
            near->far. The endpoints (a shooter at the start, the target at
            the end) are excluded — these are the bodies IN THE WAY. Compose
            the block rule yourself, e.g.
            `for e in entities_on_los(sx,sy,tx,ty): if not entity[e].tiny:
            fail('blocked')`. viewer is for viewer-conditional opacity."""
            cx1 = _coord_int(x1, "entities_on_los", "x1")
            cy1 = _coord_int(y1, "entities_on_los", "y1")
            cx2 = _coord_int(x2, "entities_on_los", "x2")
            cy2 = _coord_int(y2, "entities_on_los", "y2")
            vid = None if viewer is None else _eid(viewer)
            here = _occupants()
            # Exclude the shooter and target BY ID, not just their anchor
            # cells: a large shooter/target's body can extend onto the
            # in-between cells, and those aren't "in the way" of its own shot.
            endpoint_ids = set(here.get((cx1, cy1), ())) | set(here.get((cx2, cy2), ()))
            out: list = []
            seen: set = set(endpoint_ids)
            for cell in match._line_cells(cx1, cy1, cx2, cy2)[1:-1]:
                ids = here.get(cell)
                if not ids:
                    continue
                if not match.has_los(vid, cx1, cy1, cell[0], cell[1]):
                    continue
                for eid in sorted(ids):
                    if eid not in seen:
                        seen.add(eid)
                        out.append(eid)
            return out

        def _first_opaque(x1: Any, y1: Any, x2: Any, y2: Any,
                          viewer: Any = None) -> Any:
            """first_opaque(x1, y1, x2, y2, viewer=None): the first opaque
            cell strictly between the two points (near->far) as an (x, y)
            pair, or None if the line is clear of terrain. Read the result
            with coord_x / coord_y. The terrain a beam would strike."""
            cx1 = _coord_int(x1, "first_opaque", "x1")
            cy1 = _coord_int(y1, "first_opaque", "y1")
            cx2 = _coord_int(x2, "first_opaque", "x2")
            cy2 = _coord_int(y2, "first_opaque", "y2")
            vid = None if viewer is None else _eid(viewer)
            return match.first_opaque(vid, cx1, cy1, cx2, cy2)

        def _entities_in_cone(x: Any, y: Any, direction: Any, length: Any,
                              half_angle: Any = 45) -> list:
            """entities_in_cone(x, y, direction, length, half_angle=45):
            alive entity ids inside the cone (see cells_in_cone), sorted
            by (distance from origin, id)."""
            cells = set(_cells_in_cone(x, y, direction, length, half_angle))
            cx0 = _coord_int(x, "entities_in_cone", "x")
            cy0 = _coord_int(y, "entities_in_cone", "y")
            # Order by NEAREST covered cell to the origin (so a large entity
            # is ranked by its closest part, matching the nearest-cell
            # distance convention).
            scored = [
                (min(_distance(cx0, cy0, fx, fy, "euclidean_distance")
                     for fx, fy in match.entity_cells(match.entities[eid])), eid)
                for (_ex, _ey, eid) in _alive_at(cells)
            ]
            scored.sort(key=lambda t: (t[0], t[1]))
            return [eid for _, eid in scored]

        def _entities_in_rect(x1: Any, y1: Any, x2: Any, y2: Any) -> list:
            """entities_in_rect(x1, y1, x2, y2): alive entity ids inside the
            axis-aligned rectangle (see cells_in_rect), sorted by
            (x, y, id)."""
            cells = set(_cells_in_rect(x1, y1, x2, y2))
            return [eid for (_x, _y, eid) in sorted(_alive_at(cells))]

        def _entities_in_line_until(x1: Any, y1: Any, x2: Any, y2: Any,
                                    max_targets: Any, viewer: Any = None) -> list:
            """entities_in_line_until(x1, y1, x2, y2, max_targets,
            viewer=None): the first `max_targets` alive entity ids the
            segment passes through, near->far, endpoints INCLUDED — the
            capped sibling of entities_in_line_ignorelos, for 'a shot that
            pierces up to N targets'. With a `viewer`, sight is cut at the
            first opaque cell (LOS-aware); without one, walls are ignored.
            A large body counts once, at its nearest cell. max_targets <= 0
            returns []. For armor-limited penetration (depth varies by what
            it hits) loop entities_in_line_ignorelos / entities_on_los with
            your own accumulator instead."""
            cx1 = _coord_int(x1, "entities_in_line_until", "x1")
            cy1 = _coord_int(y1, "entities_in_line_until", "y1")
            cx2 = _coord_int(x2, "entities_in_line_until", "x2")
            cy2 = _coord_int(y2, "entities_in_line_until", "y2")
            if isinstance(max_targets, bool) or not isinstance(max_targets, (int, float)):
                raise FormulaError(
                    "entities_in_line_until(...): max_targets must be a number.")
            cap = int(max_targets)
            if cap <= 0:
                return []
            vid = None if viewer is None else _eid(viewer)
            here = _occupants()
            out: list = []
            seen: set = set()
            for cell in match._line_cells(cx1, cy1, cx2, cy2):
                # With a viewer, stop the line at the first opaque cell
                # (a wall halts the shot); the start cell never blocks.
                if vid is not None and cell != (cx1, cy1) and \
                        not match.has_los(vid, cx1, cy1, cell[0], cell[1]):
                    break
                for eid in sorted(here.get(cell, ())):
                    if eid not in seen:
                        seen.add(eid)
                        out.append(eid)
                        if len(out) >= cap:
                            return out
            return out

        ns["entities_in_line_ignorelos"] = _entities_in_line_ignorelos
        ns["entities_on_los"] = _entities_on_los
        ns["entities_in_line_until"] = _entities_in_line_until
        ns["first_opaque"] = _first_opaque
        ns["entities_in_cone"] = _entities_in_cone
        ns["entities_in_rect"] = _entities_in_rect

        def _clip_cells(cells: Any) -> list:
            """clip_cells(cells): the subset of an (x,y) cell list that is
            on the grid. Wrap a cells_in_* call to drop off-grid cells, e.g.
            clip_cells(cells_in_burst(x, y, 3))."""
            if not isinstance(cells, (list, tuple)):
                raise FormulaError(
                    "clip_cells(...): expects a list of (x, y) cells.")
            out = []
            for c in cells:
                if not isinstance(c, (list, tuple)) or len(c) != 2:
                    raise FormulaError(
                        "clip_cells(...): each cell must be an (x, y) pair.")
                cxv = _coord_int(c[0], "clip_cells", "x")
                cyv = _coord_int(c[1], "clip_cells", "y")
                if match.in_bounds(cxv, cyv):
                    out.append((cxv, cyv))
            return out
        ns["clip_cells"] = _clip_cells

        # ---- directional / facing-relative primitives -------------------
        def _facing_of(eid_t: Any) -> str:
            """facing_of(eid): the entity's current facing as a direction
            name. Bridges the facing attribute (not a var) into formulas."""
            _, e = _resolve_entity(eid_t, "facing_of")
            return getattr(e, "facing", "up")

        def _arc_default(corner_arc: Any) -> float:
            """Resolve the corner_arc argument, falling back to the
            directional_corner_arc rule when not given."""
            if corner_arc is None:
                corner_arc = match.rules.get("directional_corner_arc", 30)
            return _coerce_corner_arc(corner_arc)

        def _check_sides(sides: Any, fname: str) -> int:
            if sides not in (4, 8):
                raise FormulaError(f"{fname}(...): sides must be 4 or 8.")
            return sides

        def _relative_side(facing: Any, abs_angle: Any,
                           sides: Any = 4, corner_arc: Any = None) -> str:
            """relative_side(facing, abs_angle, sides=4, corner_arc=None):
            bucket an ABSOLUTE compass bearing into a side of an entity
            facing `facing`. sides=4 -> front/back/left_side/right_side
            (each a 90deg face); sides=8 adds the diagonal corners
            front_right_side / back_right_side / back_left_side /
            front_left_side, each spanning corner_arc degrees (default the
            directional_corner_arc rule) while the cardinal faces span
            90 - corner_arc."""
            _check_sides(sides, "relative_side")
            rel = _relative_angle(facing, abs_angle)
            return _relative_side_name(rel, sides, _arc_default(corner_arc))

        def _side_hit(target_t: Any, from_x: Any, from_y: Any,
                      sides: Any = 4, corner_arc: Any = None) -> str:
            """side_hit(target, from_x, from_y, sides=4, corner_arc=None):
            which facing-relative side of `target` something coming FROM
            (from_x, from_y) strikes. Reads target's facing + position,
            takes the bearing from the target toward the source, and buckets
            it (see relative_side). Source where the target faces -> 'front';
            opposite -> 'back'. The basis for directional armor / weakspots."""
            _check_sides(sides, "side_hit")
            _, e = _resolve_entity(target_t, "side_hit")
            # Bearing from the target toward the source of the hit, measured
            # from the target's footprint CENTER (true, possibly-fractional
            # center for even sizes) so a hit's side is judged against the
            # body's middle, not its top-left anchor. 1×1 -> the anchor.
            # Full precision (as_int=False) so corner boundaries are exact.
            w, h = match.entity_footprint(e)
            cxf = e.x + (w - 1) / 2.0
            cyf = e.y + (h - 1) / 2.0
            ab = _angle(cxf, cyf, from_x, from_y, "up", "cw", False, False)
            rel = _relative_angle(getattr(e, "facing", "up"), ab)
            return _relative_side_name(rel, sides, _arc_default(corner_arc))

        def _directional_get(eid_t: Any, base_path: Any, from_x: Any,
                             from_y: Any, default: Any = None,
                             sides: Any = 4, corner_arc: Any = None) -> Any:
            """directional_get(eid, base_path, from_x, from_y, default=None,
            sides=4, corner_arc=None): compute the side of `eid` struck from
            (from_x, from_y) via side_hit, then read eid's var at
            base_path.<side>. Returns `default` when that path is missing,
            so a partial table (only some sides defined) is fine. The
            one-call form of var_get(eid, base_path + '.' + side_hit(...))."""
            if not isinstance(base_path, str) or not base_path:
                raise FormulaError(
                    "directional_get(...): base_path must be a non-empty string.")
            side = _side_hit(eid_t, from_x, from_y, sides, corner_arc)
            full = base_path + "." + side
            if not _var_has(eid_t, full):
                return default
            return _var_get(eid_t, full)

        ns["facing_of"] = _facing_of
        ns["relative_side"] = _relative_side
        ns["side_hit"] = _side_hit
        ns["directional_get"] = _directional_get

        def _hit_location(target_t: Any, from_x: Any, from_y: Any,
                          aim: Any = None, aim_weight: Any = None,
                          aim_bonus: Any = None, mode: Any = None,
                          sides: Any = 4, corner_arc: Any = None) -> str:
            """hit_location(target, from_x, from_y, aim=None, aim_weight=None,
            aim_bonus=None, mode=None, sides=4, corner_arc=None): roll WHICH
            body part of `target` a hit coming FROM (from_x, from_y) lands
            on, returning the part id. Modes (default hit_location_mode rule):
            `weighted` uses each part's hit_weights.<side> (side from
            side_hit — front/back/left_side/right_side); `uniform` gives
            equal odds. `aim` (a part name or id) biases toward that part:
            its weight is multiplied by aim_weight (default the
            hit_location_aim_weight rule) and aim_bonus (default 0) is added
            — so aiming raises but doesn't guarantee, and a 0-weight side
            stays 0 unless aim_bonus lifts it. With no parts (or no part
            eligible for this side) returns the target itself (the hit lands
            on the main body). RNG is the match RNG (replay-safe)."""
            tid, _te = _resolve_entity(target_t, "hit_location")
            body = match.entity_parts(tid)
            if not body:
                return tid
            mode_s = str(mode) if mode is not None else \
                str(match.rules.get("hit_location_mode", "weighted"))
            if mode_s not in ("weighted", "uniform"):
                raise FormulaError(
                    "hit_location(...): mode must be 'weighted' or 'uniform'.")
            side = _side_hit(target_t, from_x, from_y, sides, corner_arc)
            aim_id = None
            if aim is not None and aim != "":
                ap = match.find_part(tid, str(aim))
                if ap is None:
                    raise FormulaError(
                        f"hit_location: aim '{aim}' is not a part of '{tid}'.")
                aim_id = ap.id
            aw = (float(aim_weight) if aim_weight is not None
                  else float(match.rules.get("hit_location_aim_weight", 3)))
            ab = float(aim_bonus) if aim_bonus is not None else 0.0
            weighted: list = []
            total = 0.0
            for p in body:
                if mode_s == "uniform":
                    w = 1.0
                else:
                    table = p.vars.get("hit_weights", {})
                    try:
                        w = float(table.get(side, 0)) if isinstance(table, dict) else 0.0
                    except (TypeError, ValueError):
                        w = 0.0
                if aim_id is not None and p.id == aim_id:
                    w = w * aw + ab
                if w > 0:
                    weighted.append((p.id, w))
                    total += w
            if total <= 0:
                # Nothing exposed for this side (and no aim_bonus lifted a
                # part above 0): the hit lands on the main body.
                return tid
            r = _active_rng().random() * total
            acc = 0.0
            for pid, w in weighted:
                acc += w
                if r <= acc:
                    return pid
            return weighted[-1][0]
        ns["hit_location"] = _hit_location

        # ---- vision primitives (range / LOS) ----------------------------
        # The bare names (can_see / team_sees_cell / team_sees_entity) mean
        # range AND line-of-sight; the _rangeonly / _losonly variants isolate
        # each concept. All ignore the fog_los rule (raw geometry queries);
        # fog applies LOS via fog_los internally.
        def _tm(team: Any) -> Optional[str]:
            return None if team is None else str(team)

        def _team_sees_cell(team: Any, x: Any, y: Any) -> bool:
            return match._team_sees(_tm(team), int(x), int(y), los=True)

        def _team_sees_cell_rangeonly(team: Any, x: Any, y: Any) -> bool:
            return match._team_sees(_tm(team), int(x), int(y), los=False)

        def _team_sees_cell_losonly(team: Any, x: Any, y: Any) -> bool:
            return match._team_has_los(_tm(team), int(x), int(y))

        def _team_sees_entity(team: Any, eid_t: Any) -> bool:
            # Footprint-aware: a large target is seen if ANY of its cells is.
            return match._team_sees_entity(_tm(team), _eid(eid_t), los=True)

        def _team_sees_entity_rangeonly(team: Any, eid_t: Any) -> bool:
            return match.team_sees_entity(_tm(team), _eid(eid_t))

        def _team_sees_entity_losonly(team: Any, eid_t: Any) -> bool:
            return match._team_has_los_entity(_tm(team), _eid(eid_t))

        def _can_see(eid_t: Any, x: Any, y: Any) -> bool:
            return match._entity_sees(_eid(eid_t), int(x), int(y), los=True)

        def _can_see_rangeonly(eid_t: Any, x: Any, y: Any) -> bool:
            return match.entity_can_see(_eid(eid_t), int(x), int(y))

        def _can_see_losonly(eid_t: Any, x: Any, y: Any) -> bool:
            return match._entity_has_los(_eid(eid_t), int(x), int(y))

        def _has_los(x1: Any, y1: Any, x2: Any, y2: Any,
                     viewer: Any = None) -> bool:
            """has_los(x1,y1,x2,y2,viewer=None): clear line of sight between
            two cells? Pass a viewer entity for viewer-conditional opacity
            (without one, such conditions read transparent)."""
            vid = None if viewer is None else _eid(viewer)
            return match.has_los(vid, int(x1), int(y1), int(x2), int(y2))

        ns["team_sees_cell"] = _team_sees_cell
        ns["team_sees_cell_rangeonly"] = _team_sees_cell_rangeonly
        ns["team_sees_cell_losonly"] = _team_sees_cell_losonly
        ns["team_sees_entity"] = _team_sees_entity
        ns["team_sees_entity_rangeonly"] = _team_sees_entity_rangeonly
        ns["team_sees_entity_losonly"] = _team_sees_entity_losonly
        ns["can_see"] = _can_see
        ns["can_see_rangeonly"] = _can_see_rangeonly
        ns["can_see_losonly"] = _can_see_losonly
        ns["has_los"] = _has_los

        # User-defined formula functions. Each becomes a Python callable
        # in the namespace. The callable binds its arguments to the
        # function's parameters, then runs the (lazily compiled) body
        # with the SAME ctx as the caller — so entity[self] inside a
        # function body resolves to the caller's self, making functions
        # behave like inline macros rather than isolated frames.
        #
        # `ns` is captured by reference: the closures below see the
        # fully-populated namespace at call time (including each other),
        # so functions can call sibling functions and recurse. The
        # recursion guard (self._fn_depth vs the limit rule) prevents an
        # unbounded recursive function from blowing the stack.
        funcs = getattr(match, "formula_functions", None) or {}
        if funcs:
            limit = int(match.rules.get(
                "formula_function_recursion_limit", 64))

            def _make_callable(fdef):
                def _call(*args):
                    if len(args) != len(fdef.params):
                        raise FormulaError(
                            f"{fdef.signature()} takes "
                            f"{len(fdef.params)} argument(s), got "
                            f"{len(args)}."
                        )
                    if self._fn_depth >= limit:
                        raise FormulaError(
                            f"formula function recursion limit "
                            f"({limit}) exceeded while calling "
                            f"'{fdef.name}' — likely an unbounded "
                            f"recursive function."
                        )
                    body_code, expr_code = self._compile_function_body(fdef)
                    # Fresh per-call namespace so parameter bindings from
                    # one call don't leak into siblings. Inherits all the
                    # builtins / match funcs / sibling functions from ns.
                    call_ns = dict(ns)
                    for pname, pval in zip(fdef.params, args):
                        call_ns[pname] = pval
                    self._fn_depth += 1
                    try:
                        if body_code is not None:
                            exec(body_code, {"__builtins__": {}}, call_ns)
                        if expr_code is None:
                            return None
                        return eval(expr_code, {"__builtins__": {}}, call_ns)
                    finally:
                        self._fn_depth -= 1
                return _call

            for fname, fdef in funcs.items():
                ns[fname] = _make_callable(fdef)

        return ns

    def _compile_function_body(self, fdef):
        """Compile (and cache on the FormulaFunction) the body of a
        user-defined function into (body_code, trailing_expr_code).

        Mirrors eval_program's split: leading statements compile to an
        exec code object (or None if the body is a single expression),
        and a trailing expression compiles to an eval code object whose
        value is the function's return value (or None if the body has
        no trailing expression).

        Validation uses the current match's function names as
        known_funcs (so a body may call sibling functions / recurse)
        plus the function's own parameters as known_params (so the
        parameters are legal identifiers inside the body).
        """
        cached = getattr(fdef, "_compiled", None)
        if cached is not None:
            return cached
        known_funcs = self._known_funcs()
        known_params = frozenset(fdef.params)
        try:
            full = ast.parse(fdef.body, mode="exec")
        except SyntaxError as e:
            raise FormulaError(
                f"function '{fdef.name}' body syntax error: {e.msg}"
            )
        trailing_expr = None
        if full.body and isinstance(full.body[-1], ast.Expr):
            trailing_expr = full.body.pop().value
        # Pass known_params so the transformer treats parameter names used
        # as entity[<param>] indices as dynamic runtime values, not
        # literal ids.
        full = _EntityAccessTransformer(known_params).visit(full)
        ast.fix_missing_locations(full)
        _validate_tree(full, known_funcs=known_funcs,
                       known_params=known_params)
        body_code = None
        if full.body:
            body_code = compile(full, "<formula-fn>", "exec")
        expr_code = None
        if trailing_expr is not None:
            expr_tree = ast.Expression(body=trailing_expr)
            expr_tree = _EntityAccessTransformer(known_params).visit(expr_tree)
            ast.fix_missing_locations(expr_tree)
            _validate_tree(expr_tree, known_funcs=known_funcs,
                           known_params=known_params)
            expr_code = compile(expr_tree, "<formula-fn>", "eval")
        compiled = (body_code, expr_code)
        try:
            fdef._compiled = compiled
        except Exception:
            # FormulaFunction should always allow the attribute; if a
            # caller passes a different shape, just skip caching.
            pass
        return compiled

    @staticmethod
    def _prepare(
        src: str, mode: str,
        known_funcs: "frozenset[str]" = frozenset(),
        known_params: "frozenset[str]" = frozenset(),
        action_mode: bool = False,
    ) -> ast.AST:
        try:
            tree = ast.parse(src, mode=mode)
        except SyntaxError as e:
            raise FormulaError(f"Syntax error: {e.msg}")
        # In action mode every bare-name Assign target becomes an
        # implicit local; pre-collect them all so the validator allows
        # forward references (e.g. `x = x + 1` at line 1 — Python
        # exec will UnboundLocalError if x is never assigned before
        # that point, but the validator accepts the static program).
        if action_mode:
            known_params = known_params | _collect_action_locals(tree)
        tree = _EntityAccessTransformer(
            known_params, action_mode=action_mode,
        ).visit(tree)
        ast.fix_missing_locations(tree)
        _validate_tree(
            tree, known_funcs=known_funcs, known_params=known_params,
            action_mode=action_mode,
        )
        return tree

    def _known_funcs(self) -> "frozenset[str]":
        """The set of user-defined function names callable on this match.
        Empty when the match has no formula functions (or isn't set)."""
        funcs = getattr(self._match, "formula_functions", None)
        if not funcs:
            return frozenset()
        return frozenset(funcs.keys())

    def eval_expression(self, src: str, ctx: EvalCtx) -> Any:
        # Fresh per-evaluation accounting so the !eval / passive-runner
        # caller sees only what THIS formula touched. eval_expression /
        # eval_program are the two entry points, so resetting here (vs.
        # in __init__) means a single FormulaEngine instance can be
        # re-used across multiple evals if a future caller wants.
        if ctx.match is None:
            ctx.match = self._match  # enable `parent`-token resolution
        self._reset_affected()
        tree = self._prepare(src, "eval", known_funcs=self._known_funcs())
        code = compile(tree, "<formula>", "eval")
        try:
            return eval(code, {"__builtins__": {}}, self._namespace(ctx))
        except FormulaError:
            raise
        except Exception as e:
            # Action-system exceptions (ActionFail / ActionEngineFault)
            # propagate unwrapped — they carry user-visible messages the
            # runner uses to decide rollback vs surface vs branch, and
            # wrapping them as "Runtime error: ..." would hide that
            # signal from the action runner above us.
            from action import ActionFail, ActionEngineFault, ChoiceNeeded
            if isinstance(e, (ActionFail, ActionEngineFault, ChoiceNeeded)):
                raise
            raise FormulaError(f"Runtime error: {e}")

    def eval_program(self, src: str, ctx: EvalCtx,
                     *, action_mode: bool = False,
                     action_bindings: Optional[Dict[str, Any]] = None) -> Any:
        """Run statements; if the source ends with an expression, return its value.

        action_mode      enables the action body language (cmd/fail/
                         source/args/target bindings, bare-name local
                         assignments, looping over `target`). The
                         caller MUST supply the matching values via
                         action_bindings.
        action_bindings  dict merged into the eval namespace AFTER
                         _namespace(ctx) — so it can override the
                         engine's default `cmd`/`fail` stubs with
                         action-aware ones, and inject
                         source/args/target proxies."""
        if ctx.match is None:
            ctx.match = self._match  # enable `parent`-token resolution
        self._reset_affected()
        try:
            full = ast.parse(src, mode="exec")
        except SyntaxError as e:
            raise FormulaError(f"Syntax error: {e.msg}")

        known = self._known_funcs()
        trailing_expr = None
        if full.body and isinstance(full.body[-1], ast.Expr):
            trailing_expr = full.body.pop().value

        if action_mode:
            full_locals = _collect_action_locals(full)
            # The trailing-expr branch parses the trailing piece
            # separately below; collect locals from THAT too so a body
            # whose trailing expr references an earlier-assigned local
            # validates.
            if trailing_expr is not None:
                full_locals = full_locals | _collect_action_locals(
                    ast.Expression(body=trailing_expr)
                )
        else:
            full_locals = frozenset()

        full = _EntityAccessTransformer(
            full_locals, action_mode=action_mode,
        ).visit(full)
        ast.fix_missing_locations(full)
        _validate_tree(
            full, known_funcs=known, known_params=full_locals,
            action_mode=action_mode,
        )

        ns = self._namespace(ctx)
        if action_bindings:
            ns.update(action_bindings)
        try:
            if full.body:
                exec(compile(full, "<formula>", "exec"), {"__builtins__": {}}, ns)
            if trailing_expr is None:
                return None
            expr_tree = ast.Expression(body=trailing_expr)
            expr_tree = _EntityAccessTransformer(
                full_locals, action_mode=action_mode,
            ).visit(expr_tree)
            ast.fix_missing_locations(expr_tree)
            _validate_tree(
                expr_tree, known_funcs=known, known_params=full_locals,
                action_mode=action_mode,
            )
            return eval(compile(expr_tree, "<formula>", "eval"),
                        {"__builtins__": {}}, ns)
        except FormulaError:
            raise
        except Exception as e:
            # Action-system exceptions (ActionFail / ActionEngineFault)
            # propagate unwrapped — they carry user-visible messages the
            # runner uses to decide rollback vs surface vs branch, and
            # wrapping them as "Runtime error: ..." would hide that
            # signal from the action runner above us.
            from action import ActionFail, ActionEngineFault, ChoiceNeeded
            if isinstance(e, (ActionFail, ActionEngineFault, ChoiceNeeded)):
                raise
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

def normalize_body_source(src: str) -> str:
    """Translate the two-character documentation escapes `\\n` and
    `\\t` (literal backslash + n / backslash + t) into the real
    characters Python's parser expects.

    Necessary because every realistic input surface — Discord, the
    CLI's input(), shlex.split — preserves literal `\\n` rather than
    a real newline when the user types it inside a single-line quoted
    string. Without this normalization, a multi-line action or
    passive body typed at the CLI lands in storage with a literal
    backslash followed by `n`, and ast.parse rejects it with
    "unexpected character after line continuation character".

    The scenario harness does the same translation in
    `_interpret_escapes` for the same reason; this helper applies it
    everywhere user-supplied body text reaches the engine (action
    discovery, !passive add, !gpassive add, !func def). Idempotent:
    real newlines pass through unchanged because they don't contain
    the two-character `\\n` sequence.

    Trade-off: a formula whose source literally contains `"a\\nb"`
    (a string literal with an embedded backslash-n that the GM wants
    to KEEP as two characters) can't be expressed this way — that
    matches the harness's behavior and is an accepted limitation."""
    if not isinstance(src, str):
        return src
    return src.replace("\\n", "\n").replace("\\t", "\t")


def validate_program(
    src: str,
    known_funcs: "frozenset[str]" = frozenset(),
    *, action_mode: bool = False,
) -> None:
    """
    Parse and validate a formula in program mode (statements + optional
    trailing expression). Raises FormulaError on syntax errors or disallowed
    constructs. Used to eagerly catch broken passive formulas at add time.
    Does not need a match — purely a syntax check.

    Pass known_funcs (the active match's defined formula-function names)
    so a passive body that calls a user-defined function validates at
    registration time instead of erroring only when the hook fires.

    action_mode  enables the action body language extensions: cmd/fail
                 calls, source/args/target name bindings, bare-name
                 local assignments, and `for x in target:` loops. Used
                 at action-definition time to surface errors before
                 the action is ever invoked.
    """
    FormulaEngine._prepare(
        src, "exec", known_funcs=known_funcs, action_mode=action_mode,
    )