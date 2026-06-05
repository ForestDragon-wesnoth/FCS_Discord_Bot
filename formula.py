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
  Random:    random_int, random_string
  Geometry:  distance, angle, direction_to
  Areas:     cells_in_burst, cells_in_line, cells_in_cone  (return lists
             of (x,y) tuples; pair with len() OR iterate with a for-loop)
  Spatial:   entities_within(eid, n, mode, relation),
             nearest_entity(eid, relation, mode)   (scan alive entities
             relative to a reference; relation = ""/"hostile"/"ally"/
             "same_team"/"attackable"),
             entities_in_area(x, y, n, mode)   (the coord-rooted twin —
             scans alive entities around a POINT, not an entity)
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
The iterable MUST be a direct call to one of: entities_within,
group_members, cells_in_burst, cells_in_line, cells_in_cone. The
target may be a single name (entity id / scalar) or a tuple of
names (for coord unpacking). Total iterations across all loops in
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


_ALLOWED_FUNCS: Dict[str, Any] = {
    "min": min, "max": max, "abs": abs, "round": round,
    "int": int, "float": float, "str": str,
    "len": _len,
    "random_int": _random_int,
    "random_string": _random_string,
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
    # Introspection / query helpers (PR: introspection primitives). All
    # return lists, so all are loopable.
    "status_names",
    "var_keys",
    "tile_keys",
    "entity_groups",
    "all_entities",
    "entities_with_status",
    "entities_with_var",
    "entities_in_area",
    # Action introspection — returns a list of action names, loopable.
    "entity_actions",
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
)

# Entity-id sentinel identifiers. Inside `entity[X]` these get
# special-cased by the transformer; as bare identifiers they bind to
# the actual entity id string at namespace build time (or to None when
# unbound). Letting them appear bare lets a formula write
# `status_set(self, status_name, "remaining", 0)` instead of the more
# verbose `status_set(self_id(), ...)`.
_ENTITY_TOKEN_NAMES: Tuple[str, ...] = ("self", "this", "current")


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
_ACTION_BUILTINS = frozenset({"cmd", "fail"})
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
            if slice_node.id in ("self", "this", "current"):
                # Special token — pass the token string; resolve_who maps it.
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
            if (n.id not in ("__read", "__write", "__loop_tick")
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
            if (fname not in ("__read", "__write", "__loop_tick")
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

        # Match-bound group functions. Each takes string args; entity-id
        # args go through ctx.resolve_who so the special tokens "self",
        # "this", and "current" work the same as inside entity[X].path
        # — meaning a formula can write group_add("swarm", "self") if
        # the bare-identifier shorthand self_id() feels too verbose.
        match = self._match
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
            if match.is_occupied(nx, ny, ignore_entity_id=e.id):
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
            e = match.entities.get(eid)
            if e is None:
                raise FormulaError(f"push_entity: unknown entity id '{eid}'.")
            if not isinstance(direction, str):
                raise FormulaError(
                    f"push_entity(eid, direction, n): direction must be a "
                    f"string, got {type(direction).__name__}."
                )
            canon = _normalize_direction(direction)
            if canon is None:
                raise FormulaError(
                    f"push_entity(eid, direction, n): unknown direction "
                    f"'{direction}'."
                )
            allow_diag = bool(match.rules.get("allow_diagonal_movement", False))
            if canon in _DIAGONAL_DIRECTIONS and not allow_diag:
                raise FormulaError(
                    f"push_entity: diagonal direction '{direction}' "
                    f"requires allow_diagonal_movement=True."
                )
            if not isinstance(n, int) or isinstance(n, bool):
                raise FormulaError(
                    f"push_entity(eid, direction, n): n must be int, got "
                    f"{type(n).__name__}."
                )
            if n <= 0:
                return 0
            dx, dy = _DIRECTION_VECTORS[canon]
            # Walk forward to find the longest legal prefix. The push
            # stops at the cell BEFORE the first blocker (off-grid or
            # another entity). Intermediate occupancy matters — pushing
            # through an entity isn't allowed (matches the natural
            # "knockback hits the wall behind them" intuition).
            x, y = e.x, e.y
            steps = 0
            for _ in range(n):
                nx, ny = x + dx, y + dy
                if not match.in_bounds(nx, ny):
                    break
                if match.is_occupied(nx, ny, ignore_entity_id=e.id):
                    break
                x, y = nx, ny
                steps += 1
            if steps == 0:
                return 0
            # Delegate the actual stepwise commit to move_dirs so we get
            # per-step on_enter/on_exit, on_stop, and on_entity_moved
            # firing for free. We've already proven the path is legal.
            try:
                e.move_dirs([(canon, steps)])
            except (_OutOfBounds, _Occupied) as ex:
                # Should be unreachable — we validated above — but if a
                # passive mutates state between validation and commit,
                # surface the failure cleanly.
                raise FormulaError(str(ex))
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
            ea = match.entities.get(aid)
            eb = match.entities.get(bid)
            if ea is None:
                raise FormulaError(f"swap_entities: unknown entity id '{aid}'.")
            if eb is None:
                raise FormulaError(f"swap_entities: unknown entity id '{bid}'.")
            if aid == bid:
                return False
            ax, ay = ea.x, ea.y
            bx, by = eb.x, eb.y
            # Same-cell swap is degenerate (both entities are at the
            # same coords, which shouldn't normally happen but is
            # defended in case a future feature allows colocation):
            # nothing changes, no hooks fire.
            if (ax, ay) == (bx, by):
                return False
            # Fire on_exit for both at their CURRENT positions before
            # any state changes — passives see (ea.x,ea.y) == (ax,ay).
            match.fire_tile_hook("on_exit", aid, ax, ay)
            match.fire_tile_hook("on_exit", bid, bx, by)
            # Atomic position swap via direct move_to (bypasses
            # is_occupied — required, since A and B occupy each other's
            # target cells until the swap completes).
            ea.move_to(bx, by)
            eb.move_to(ax, ay)
            # Fire on_enter + on_stop at new positions.
            match.fire_tile_hook("on_enter", aid, bx, by)
            match.fire_tile_hook("on_stop", aid, bx, by)
            match.fire_tile_hook("on_enter", bid, ax, ay)
            match.fire_tile_hook("on_stop", bid, ax, ay)
            # Entity-side movement events.
            match.fire_entity_moved(aid, ax, ay, bx, by)
            match.fire_entity_moved(bid, bx, by, ax, ay)
            self._note_affected(aid)
            self._note_affected(bid)
            return True

        ns["move_entity"] = _move_entity
        ns["move_step"]   = _move_step
        ns["push_entity"] = _push_entity
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
                if not _relation_ok(relation, ref_eid, oid):
                    continue
                yield oid, oe, ref_e

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
                d = _distance(ref_e.x, ref_e.y, oe.x, oe.y, mode)
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
                d = _distance(ref_e.x, ref_e.y, oe.x, oe.y, mode)
                # Tie-break by id so the result is deterministic.
                if best_d is None or d < best_d or (d == best_d and oid < best_id):
                    best_d = d
                    best_id = oid
            return best_id

        ns["entities_within"] = _entities_within
        ns["nearest_entity"]  = _nearest_entity

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

        ns["status_has"]      = _status_has
        ns["status_has_path"] = _status_has_path
        ns["status_get"]      = _status_get
        ns["status_set"]      = _status_set
        ns["status_del"]      = _status_del
        ns["status_add"]      = _status_add
        ns["status_remove"]   = _status_remove

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
            """all_entities(): every ALIVE entity id, in
            match.entities insertion order. The match-wide iteration
            primitive (no reference entity required)."""
            return [eid for eid, e in match.entities.items() if e.is_alive]
        ns["all_entities"] = _all_entities

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
                if not e.is_alive:
                    continue
                d = _distance(x, y, e.x, e.y, mode)
                if d <= n:
                    scored.append((d, eid))
            scored.sort(key=lambda t: (t[0], t[1]))
            return [eid for _, eid in scored]
        ns["entities_in_area"] = _entities_in_area

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
            from action import ActionFail, ActionEngineFault
            if isinstance(e, (ActionFail, ActionEngineFault)):
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
            from action import ActionFail, ActionEngineFault
            if isinstance(e, (ActionFail, ActionEngineFault)):
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