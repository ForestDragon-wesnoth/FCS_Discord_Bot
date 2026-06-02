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
             nearest_entity(eid, relation, mode)  (scan alive entities
             relative to a reference; relation = ""/"hostile"/"ally"/
             "same_team"/"attackable")
  Teams:     is_same_team, is_hostile, is_part_of_team, is_attackable
  Groups:    group_has, group_size, group_add, group_remove,
             group_members(name)  (insertion-order id list; loopable)
  Statuses:  status_has, status_has_path, status_get, status_set,
             status_del, status_add, status_remove  (Entity.status is a
             dict-of-dicts: status_get raises on missing, status_del /
             status_add / status_remove return bool)
  Identity:  self_id, current_id  (bare identifiers `self`, `this`,
             `current` also work — bind to ctx.target / ctx.this as
             string ids, or to None when unbound)
  Tiles:     tile_get, tile_has, tile_set, tile_del, tile_clear
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
import math
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
    ast.Name, ast.Constant, ast.Load, ast.Store,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.UAdd, ast.USub, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
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

    def __init__(self, known_params: "frozenset[str]" = frozenset()):
        self.known_params = known_params

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

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        flat = self._flatten(node)
        if flat is None:
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
        flat = self._flatten(node.targets[0])
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


def _validate_for(node: ast.For) -> Tuple[List[str], "frozenset[str]"]:
    """Verify a For node's iterable is a Call to a _LOOPABLE_FUNCS name,
    and return the loop variable names. The caller augments
    known_params with these names before recursing into the body."""
    if node.orelse:
        # `for ... else:` is supported by Python but its semantics
        # ("else runs unless break") are surprising in a sandboxed
        # context where there's no break. Reject up front.
        raise FormulaError(
            "for-loop `else:` clause is not supported."
        )
    it = node.iter
    if not (isinstance(it, ast.Call)
            and isinstance(it.func, ast.Name)
            and it.func.id in _LOOPABLE_FUNCS):
        allowed = ", ".join(sorted(_LOOPABLE_FUNCS))
        raise FormulaError(
            f"for-loop iterable must be a direct call to one of "
            f"{{{allowed}}} — got "
            f"`{ast.unparse(it) if hasattr(ast, 'unparse') else type(it).__name__}`."
        )
    names = _for_target_names(node.target)
    return names, frozenset(names)


def _validate_tree(
    tree: ast.AST,
    known_funcs: "frozenset[str]" = frozenset(),
    known_params: "frozenset[str]" = frozenset(),
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
    """
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
                    and n.id not in scope_params):
                raise FormulaError(f"Unknown identifier '{n.id}'.")
        if isinstance(n, ast.Call):
            if not isinstance(n.func, ast.Name):
                raise FormulaError("Only direct function calls are allowed.")
            fname = n.func.id
            if (fname not in ("__read", "__write", "__loop_tick")
                    and fname not in _ALLOWED_FUNCS
                    and fname not in _MATCH_FUNC_NAMES
                    and fname not in known_funcs):
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
            _, loop_vars = _validate_for(node)
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
        # everything else defaults to None.
        _CONTEXT_DEFAULTS = {"was_clamped": False}
        for name in HOOK_CONTEXT_NAMES:
            ns[name] = extras.get(name, _CONTEXT_DEFAULTS.get(name))
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
            del e.status[name]
            return True

        ns["status_has"]      = _status_has
        ns["status_has_path"] = _status_has_path
        ns["status_get"]      = _status_get
        ns["status_set"]      = _status_set
        ns["status_del"]      = _status_del
        ns["status_add"]      = _status_add
        ns["status_remove"]   = _status_remove

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
    ) -> ast.AST:
        try:
            tree = ast.parse(src, mode=mode)
        except SyntaxError as e:
            raise FormulaError(f"Syntax error: {e.msg}")
        tree = _EntityAccessTransformer(known_params).visit(tree)
        ast.fix_missing_locations(tree)
        _validate_tree(tree, known_funcs=known_funcs, known_params=known_params)
        return tree

    def _known_funcs(self) -> "frozenset[str]":
        """The set of user-defined function names callable on this match.
        Empty when the match has no formula functions (or isn't set)."""
        funcs = getattr(self._match, "formula_functions", None)
        if not funcs:
            return frozenset()
        return frozenset(funcs.keys())

    def eval_expression(self, src: str, ctx: EvalCtx) -> Any:
        tree = self._prepare(src, "eval", known_funcs=self._known_funcs())
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

        known = self._known_funcs()
        trailing_expr = None
        if full.body and isinstance(full.body[-1], ast.Expr):
            trailing_expr = full.body.pop().value

        full = _EntityAccessTransformer().visit(full)
        ast.fix_missing_locations(full)
        _validate_tree(full, known_funcs=known)

        ns = self._namespace(ctx)
        try:
            if full.body:
                exec(compile(full, "<formula>", "exec"), {"__builtins__": {}}, ns)
            if trailing_expr is None:
                return None
            expr_tree = ast.Expression(body=trailing_expr)
            expr_tree = _EntityAccessTransformer().visit(expr_tree)
            ast.fix_missing_locations(expr_tree)
            _validate_tree(expr_tree, known_funcs=known)
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

def validate_program(
    src: str,
    known_funcs: "frozenset[str]" = frozenset(),
) -> None:
    """
    Parse and validate a formula in program mode (statements + optional
    trailing expression). Raises FormulaError on syntax errors or disallowed
    constructs. Used to eagerly catch broken passive formulas at add time.
    Does not need a match — purely a syntax check.

    Pass known_funcs (the active match's defined formula-function names)
    so a passive body that calls a user-defined function validates at
    registration time instead of erroring only when the hook fires.
    """
    FormulaEngine._prepare(src, "exec", known_funcs=known_funcs)