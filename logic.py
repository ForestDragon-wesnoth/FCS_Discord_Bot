## logic.py (Core, testable)

# logic.py
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal, Any, Dict, List, Optional, Tuple, Set
import uuid
import json

# -------------------------
# Exceptions
# -------------------------
class VTTError(Exception):
    pass

class OutOfBounds(VTTError):
    pass

class Occupied(VTTError):
    pass

class NotFound(VTTError):
    pass

class DuplicateId(VTTError):
    pass

class ReservedId(VTTError):
    pass

# -------------------------
# Data Models
# -------------------------

# -------------------------
# Types & helpers
# -------------------------
Direction = Literal["up", "down", "left", "right"]
ALLOWED_DIRECTIONS: Set[str] = {"up", "down", "left", "right"}


# ---- Rule registry & model ---------------------------------------------------
# Each rule now has a default, schema, and description. GameSystems store
# per-system overrides as Rule objects (key/value/schema/description).
#
# Schema types: "bool", "int", "enum"
RULES_REGISTRY: Dict[str, Dict[str, Any]] = {
    # --- Vital variable names (what var keys map to HP / MaxHP / turn-order) ---
    "hp_var": {
        "default": "hp",
        "schema": {"type": "str"},
        "desc": "Variable name in entity vars used as hit points (e.g. 'hp', 'hull').",
    },
    "max_hp_var": {
        "default": "max_hp",
        "schema": {"type": "str"},
        "desc": "Variable name in entity vars used as maximum hit points (e.g. 'max_hp', 'max_hull').",
    },
    "turnorder_var": {
        "default": "initiative",
        "schema": {"type": "str"},
        "desc": "Variable name in entity vars used for turn-order priority (e.g. 'initiative', 'ship_agility').",
    },
    # Spawning / facing
    "spawn_face_toward_center": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": "If True, new entities face toward the map center at spawn; if False, use spawn_default_facing.",
    },
    "spawn_default_facing": {
        "default": "up",
        "schema": {"type": "enum", "choices": ALLOWED_DIRECTIONS},
        "desc": "Default facing used when spawn_face_toward_center is False.",
    },
    # OPTIONS THAT ARE NOT YET IMPLEMENTED (examples for future)
    ##UI rules:
    ## Entity line formatting (shown in !state listings, turn order rows)
    "entity_line_format": {
        "default": "{name} ({id}): HP: {hp}/{max_hp} X,Y: {x},{y} facing {facing}",
        "schema": {"type": "str"},
        "desc": (
            "Single-line template for an entity row (used in !list, !state). "
            "Placeholders: {key} substitutes a value; {?key?}...{/?} renders inner "
            "only if the key is present and truthy. Dotted paths supported: "
            "{inventory.sword.damage}. Built-in keys: id, name, x, y, facing, team, "
            "initiative, hp, max_hp, status_csv, passives_csv. Any top-level "
            "entity var is also available by name. Use \\n for newlines."
        ),
    },
    ## Entity card formatting (shown in !ent info)
    "entity_info_format": {
        "default": (
            "**{name}** (`{id}`)\\n"
            "HP: {hp}/{max_hp}   Position: ({x},{y}) facing {facing}"
            "{?team?}   Team: {team}{/?}\\n"
            "{?status_csv?}Status: {status_csv}\\n{/?}"
            "{?passives_csv?}Passives: {passives_csv}\\n{/?}"
            "{?inventory?}Inventory: {inventory}\\n{/?}"
        ),
        "schema": {"type": "str"},
        "desc": (
            "Multi-line template for the !ent info card. Same syntax as "
            "entity_line_format. Use \\n for newlines. For complete raw info "
            "regardless of template, use !ent dump."
        ),
    },
    ## Var-hook system rules
    "var_hook_recursion_limit": {
        "default": 128,
        "schema": {"type": "int"},
        "desc": (
            "Maximum depth of var-event recursion before further hook firing "
            "is suppressed. A var-hook passive's formula may itself write vars, "
            "which triggers more events. If a chain exceeds this depth a "
            "warning is logged and further hooks in the chain don't fire — the "
            "underlying writes still happen, only event-firing is suppressed. "
            "Default 128 is plenty for legitimate cascades; if you hit it, "
            "you almost certainly have an infinite loop."
        ),
    },
    "var_hook_warning_verbosity": {
        "default": "minimal",
        "schema": {"type": "str"},
        "desc": (
            "Verbosity of soft warnings for destructive var writes. Levels: "
            "'off' = no warnings; 'minimal' = warn only on recursion-limit "
            "hits (default); 'detailed' = also annotate every destructive "
            "write with the count of removed keys."
        ),
    },
    "default_clamps": {
        # The canonical clamp: hp clamped by max_hp, soft mode, no min. This
        # preserves the historical "hp can't exceed max_hp" behavior that
        # heal_entity used to enforce by hand, now done generically.
        # NOTE: this default uses the literal var names "hp" and "max_hp".
        # If a GM changes hp_var / max_hp_var in their game system (e.g. to
        # "hull" / "max_hull" for a sci-fi system), this default clamp will
        # silently stop engaging — it references vars that don't exist. The
        # GM needs to add a replacement via !gclamp add. This is documented
        # behavior, not a bug; we don't auto-rewrite the clamp because that
        # would couple the clamp system to the vital-var rules in a way
        # that breaks composability.
        "default": [
            {"path": "hp", "max": "max_hp", "mode": "soft"}
        ],
        "schema": {"type": "list"},
        "desc": (
            "System-wide clamp specs applied to every entity in matches using "
            "this system. Each entry is a dict with: path (the var to clamp), "
            "max (numeric literal or var path string, optional), min (same "
            "options, optional), mode ('soft' or 'hard'). Edit via !gclamp "
            "add/remove/list — NOT via !system set, which can't handle "
            "structured values. Entity-level clamps (Entity.clamps) override "
            "system-level clamps on the same path; see !clamp add/remove/list."
        ),
    },
    # "movement_block_through": {
    #     "default": False,
    #     "schema": {"type": "bool"},
    #     "desc": "If True, stepwise movement collides with units.",
    # },
    # "friendlyfire": {
    #     "default": False,
    #     "schema": {"type": "bool"},
    #     "desc": "If True, attacks can hit allies; if False, allies are auto-excluded.",
    # },
}

# Backwards-compatibility helpers (engine defaults & bare schema maps)
DEFAULT_SYSTEM_SETTINGS: Dict[str, Any] = {
    k: v["default"] for k, v in RULES_REGISTRY.items()
}
RULE_SCHEMA: Dict[str, Dict[str, Any]] = {
    k: v["schema"] for k, v in RULES_REGISTRY.items()
}

@dataclass
class Rule:
    key: str
    value: Any
    schema: Dict[str, Any]
    description: str


#functions to use alongside RULE_SCHEMA
def _parse_bool(token: str) -> bool:
    """Parse a boolean from a command-line token.

    Accepts a wide variety of truthy/falsy spellings since these arrive from
    Discord and the CLI where typing conventions vary. Case-insensitive.
    Truthy: true, t, yes, y, on, 1.
    Falsy:  false, f, no, n, off, 0.
    Anything else raises VTTError.
    """
    t = token.strip().lower()
    if t in ("true", "t", "yes", "y", "on", "1"): return True
    if t in ("false", "f", "no", "n", "off", "0"): return False
    raise VTTError(
        "Expected a boolean: use 'true'/'false', 'yes'/'no', 'on'/'off', or '1'/'0'."
    )

def _coerce_rule_value(key: str, raw_value: str):
    # Unknown key? block (prevents typos or unimplemented settings)
    if key not in RULE_SCHEMA:
        allowed = ", ".join(sorted(RULE_SCHEMA.keys()))
        raise VTTError(f"Unknown setting '{key}'. Allowed: {allowed}")

    spec = RULE_SCHEMA[key]
    t = spec["type"]

    if t == "bool":
        return _parse_bool(raw_value)

    if t == "int":
        try:
            return int(raw_value, 10)
        except ValueError:
            raise VTTError(f"Setting '{key}' expects an integer.")

    if t == "enum":
        choices = spec["choices"]
        v = raw_value.strip().lower()
        if v in choices:
            return v
        allowed = ", ".join(sorted(choices))
        raise VTTError(f"Setting '{key}' must be one of: {allowed}")

    if t == "str":
        # keep raw as-is; CLI passes a single token
        # (you can extend later to join the rest of args for multi-word strings)
        return raw_value

    if t == "list":
        # Structured list-valued rules (currently: default_clamps) aren't
        # editable via the flat key=value !system set command. Route the GM
        # to the dedicated commands instead.
        raise VTTError(
            f"Setting '{key}' is a structured list and can't be edited via "
            f"!system set. Use the dedicated commands for this rule "
            f"(e.g. !gclamp add/remove/list for default_clamps)."
        )

    raise VTTError(f"Invalid schema for setting '{key}'.")


def _dominant_axis_dir(dx: int, dy: int) -> Direction:
    # ties prefer vertical
    if abs(dy) >= abs(dx):
        return "down" if dy > 0 else "up"
    else:
        return "right" if dx > 0 else "left"

def _default_facing_for(x: int, y: int, width: int, height: int) -> Direction:
    cx = (width + 1) / 2
    cy = (height + 1) / 2
    dx = cx - x
    dy = cy - y
    return _dominant_axis_dir(int(round(dx)), int(round(dy)))


# ---------- Special/reserved id registry (like how "this" or "current" is the entity whose turn it is right now, "self" is usually the entity directly affected ay a command regardless of turn order") ----------


RESERVED_IDS: Set[str] = {"current", "this", "self"}


#TODO: flesh it out into a proper system later


# -------------------------
# Passive hooks & Passive dataclass
# -------------------------

# Valid `when` values for passives. Entity passives fire on the owning
# entity's own turn/round; global passives fire once per entity in turn order
# on each hook (per-entity iteration).
# ----------------------------------------------------------------------------
# HOOK_NAMES: every valid value for Passive.when.
#
# Hooks split into two families:
#
# 1. Turn/lifecycle hooks (no var-event payload):
#    - on_turn_start       fired before each entity's turn
#    - on_turn_end         fired after each entity's turn
#    - on_round_start      fired before each round (once per entity in turn_order)
#    - on_round_end        fired after each round (once per entity in turn_order)
#    - on_entity_spawned   fired once when an entity is spawned via Entity.spawn
#
#    For these, formula context exposes: this, self. Var-event bindings
#    (changed_key, old_value, new_value, hook_name) are bound to None.
#
# 2. Var hooks (carry a payload describing a single var-event):
#    - on_var_created      a previously-nonexistent path was just created.
#                          Fired at EVERY level along a path that's newly
#                          created. Example: writing inventory.gold.coins=50
#                          when inventory didn't exist fires three events:
#                          'inventory', then 'inventory.gold', then
#                          'inventory.gold.coins' (top-down ordering).
#                          old_value is None; new_value is the value at that
#                          path (which may itself be a dict for non-leaf
#                          levels).
#
#    - on_var_changed      an existing path was overwritten with a different
#                          value. Fired only on the leaves whose values
#                          actually differ — overwriting a parent dict does
#                          NOT fire a change event on the parent unless the
#                          parent itself genuinely differs after the diff.
#                          old_value and new_value are both populated.
#
#    - on_var_removed      a previously-existing path no longer exists.
#                          Fired at every level that ceased to exist,
#                          bottom-up (leaves first, then their containers).
#                          old_value carries the full previous subtree (so
#                          'log what got destroyed' passives can inspect it);
#                          new_value is None.
#
#    - on_var_written      catch-all. Fires alongside every created/changed/
#                          removed event. Use the `hook_name` context binding
#                          to discriminate (it'll be one of the three above,
#                          NOT 'on_var_written' itself).
#                          NOTE: on_var_written does NOT include attempt
#                          events. on_var_write_attempt is a separate channel
#                          for "an attempt happened" vs "data changed." A
#                          passive that wants to audit literally every write
#                          activity should subscribe to BOTH on_var_written
#                          AND on_var_write_attempt.
#
#    - on_var_write_attempt
#                          Fires for every write call BEFORE the mutation,
#                          regardless of whether the resulting diff is empty.
#                          This closes the "no-event" gap: a heal at full HP
#                          produces no on_var_changed event (since old==new
#                          after clamping), but on_var_write_attempt still
#                          fires — so passives that need to count attempted
#                          actions (overheal magnitude tracking, "every heal
#                          ever logged") can observe them.
#
#                          The hook fires AFTER clamp computation but BEFORE
#                          the actual mutation. Context bindings:
#                            old_value      current state at the path
#                            new_value      what will be stored (post-clamp)
#                            intended_value what the caller requested (pre-clamp)
#                            was_clamped    True iff clamping changed new_value
#                                           (False on bypassed writes — bypass
#                                           skips clamping, so nothing was
#                                           clamped)
#                            hook_name      "on_var_write_attempt"
#
#                          Fires on bypassed writes (bypass affects clamping,
#                          not observability). Does NOT fire on remove_var or
#                          remove_var_silent — those have no "no-op" gap that
#                          needs filling (remove raises if the path is absent).
#
# Passives subscribing to var hooks use the `target` + `scope` fields on
# Passive to filter which events they care about. See the Passive class
# docstring for details.
# ----------------------------------------------------------------------------
HOOK_NAMES: Set[str] = {
    "on_turn_start",
    "on_turn_end",
    "on_round_start",
    "on_round_end",
    "on_entity_spawned",
    "on_var_created",
    "on_var_changed",
    "on_var_removed",
    "on_var_written",
    "on_var_write_attempt",
}

# Var hooks distinguished from lifecycle hooks. Useful for code paths that
# need to know whether to expect a var-event payload.
VAR_HOOKS: Set[str] = {
    "on_var_created",
    "on_var_changed",
    "on_var_removed",
    "on_var_written",
    "on_var_write_attempt",
}

# Valid values for Passive.scope (var-hook filtering).
#   exact     — fires only when changed_key == target
#   children  — fires when changed_key is a direct child of target
#               (exactly one path segment deeper)
#   deep      — fires when changed_key is target itself OR any descendant
#               (used with target="" for entity-wide watch)
PASSIVE_SCOPES: Set[str] = {"exact", "children", "deep"}


@dataclass
class Passive:
    """
    A passive ability — either entity-scoped (stored on Entity.passives) or
    global (stored on Match.global_passives). The `formula` is run as a
    program (assignments allowed; trailing expression value is logged) each
    time `when` fires for the appropriate target entity.

    Inside the formula:
      this = entity whose turn it currently is (Match.current_entity_id())
      self = the target entity for this firing
             - for entity passives: always the owning entity
             - for global passives: each entity iterated for the hook

    For var hooks (on_var_created/changed/removed/written), the formula also
    has access to:
      changed_key   the dotted path that fired this event (e.g. "inventory.sword")
      old_value     the value before the event (None for created)
      new_value     the value after the event  (None for removed)
      hook_name     one of "on_var_created", "on_var_changed", "on_var_removed"
                    (NEVER "on_var_written" — that's just the catch-all
                    subscription. Use hook_name to discriminate inside an
                    on_var_written passive.)

    For non-var hooks, all four bindings above are None.

    --- Filtering var hooks: `target` + `scope` ---

    Var hooks fire potentially many events per write. To prevent every var
    passive firing on every write, var-hook passives filter via `target` and
    `scope`:

      target   The dotted path being watched. Empty string ("") = entity root.
               Examples: "hp", "inventory", "inventory.sword.damage", "".

      scope    How the target relates to the event's changed_key:
                 "exact"    — fires only when changed_key == target.
                              Example: target="hp" catches writes to hp itself.
                 "children" — fires when changed_key is a direct child of target
                              (exactly one segment deeper).
                              Example: target="inventory" catches
                              "inventory.sword" but NOT "inventory.sword.damage".
                              With target="" (root), catches every top-level var.
                 "deep"     — fires when changed_key equals target OR is anywhere
                              below it. Example: target="inventory" catches
                              "inventory", "inventory.sword", and
                              "inventory.sword.damage". With target="" (root),
                              catches every var-event on the entity (entity-wide
                              watch).

    For non-var hooks (turn/round/spawn), target and scope are ignored.
    """
    id: str
    when: str
    formula: str
    # Filtering for var hooks. Defaults give "catch everything on this entity"
    # behavior, which is the safest default for non-var hooks too (since they
    # ignore these fields entirely).
    target: str = ""
    scope: str = "deep"

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip():
            raise VTTError("Passive id must be a non-empty string.")
        if self.when not in HOOK_NAMES:
            allowed = ", ".join(sorted(HOOK_NAMES))
            raise VTTError(
                f"Unknown passive hook '{self.when}'. Allowed: {allowed}"
            )
        if not isinstance(self.formula, str):
            raise VTTError("Passive formula must be a string.")
        if not isinstance(self.target, str):
            raise VTTError("Passive target must be a string (use '' for root).")
        if self.scope not in PASSIVE_SCOPES:
            allowed = ", ".join(sorted(PASSIVE_SCOPES))
            raise VTTError(
                f"Unknown passive scope '{self.scope}'. Allowed: {allowed}"
            )

    def matches_event(self, changed_key: str) -> bool:
        """
        Return True if a var-event on `changed_key` should fire this passive.
        Only meaningful for var hooks; non-var hooks ignore target/scope.

        The target is treated as a dotted path. Empty target = root, which
        always matches under "deep" scope (entity-wide watch) and matches
        the immediate top-level keys under "children" scope.
        """
        t = self.target
        if self.scope == "exact":
            return changed_key == t
        if self.scope == "children":
            # Direct children of root (target="") = top-level keys (no dots).
            if t == "":
                return "." not in changed_key
            prefix = t + "."
            if not changed_key.startswith(prefix):
                return False
            return "." not in changed_key[len(prefix):]
        if self.scope == "deep":
            if t == "":
                return True  # entity-wide watch
            if changed_key == t:
                return True
            return changed_key.startswith(t + ".")
        return False  # unreachable given __post_init__ validation

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "when": self.when,
            "formula": self.formula,
            "target": self.target,
            "scope": self.scope,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Passive":
        # `target` and `scope` are recent additions; older save files lack
        # them. Fall back to the defaults so old saves load cleanly.
        return Passive(
            id=d["id"],
            when=d["when"],
            formula=d["formula"],
            target=d.get("target", ""),
            scope=d.get("scope", "deep"),
        )


# ============================================================================
# Clamp infrastructure
# ============================================================================
# Clamps constrain numeric var writes. A clamp specifies a path being clamped,
# optional max and/or min bounds, and a mode (soft/hard). Clamps live in two
# places:
#   1. The game system's "default_clamps" rule — a list of clamp specs that
#      apply to all entities in matches using that system. Common cases like
#      "hp can't exceed max_hp" belong here.
#   2. Per-entity overrides on Entity.clamps — a dict keyed by path. When an
#      entity defines a clamp on the same path the system does, the entity's
#      clamp wholly replaces the system's for that path (not field-by-field
#      merge — see design discussion in commit notes).
#
# Bounds (max, min) can each be:
#   - a numeric literal (e.g. 100): the absolute bound
#   - a string path (e.g. "max_hp"): resolved against the entity's vars at
#     write time; if the referenced var doesn't exist or isn't numeric, the
#     bound is treated as absent ("no bound") and the clamp doesn't engage
#     in that direction.
#
# Modes:
#   - "hard": every numeric write that would violate the bound is clamped.
#     If hp is 70 (already over max=50), writing 80 still clamps to 50;
#     writing 40 stays at 40 (under bound, fine); writing 60 clamps to 50.
#   - "soft": engages only when a write would push the value across the
#     bound from the LEGAL side. If hp=70/max=50, writing 80 leaves hp at 80
#     (already past — soft is dormant). If hp=30/max=50, writing 80 clamps
#     to 50 (would push from below). Creation is treated as "crossing from
#     below" since there's no prior value — soft engages on initial writes
#     that land out of bounds.
#
# Non-numeric writes (strings, dicts, lists, bools) bypass clamps entirely.
# Booleans are explicitly excluded even though `bool` is a subclass of `int`,
# because semantic equality between True/1 and False/0 would lead to weird
# clamp behavior.
#
# Clamp bypass at command level: !ent set_var ... bypass_clamp=yes (and
# equivalent for other writing commands). When bypassed, the clamp is not
# consulted at all and the raw value is written. The resulting event still
# carries the (un-clamped) intended_value alongside new_value, and
# was_clamped=False since no clamping happened.
# ============================================================================

@dataclass
class ClampSpec:
    """One clamp definition. See module-level docstring for full semantics.

    path        Dotted var path being clamped (e.g. "hp" or "inventory.gold.amount").
    max         Upper bound: numeric literal, dotted path string, or None
                for no upper bound.
    min         Lower bound: same options, None = no lower bound.
    mode        "soft" (engage only when crossing from legal side) or
                "hard" (always enforce).

    At least one of max/min must be set; a clamp with neither is invalid.
    Bounds defined as path strings are resolved against the entity's vars
    at write time, so the bound can dynamically follow another variable
    (e.g. hp clamped by max_hp). If the referenced var is missing or
    non-numeric at write time, the clamp gracefully degrades to "no bound"
    in that direction.
    """
    path: str
    max: Any = None
    min: Any = None
    mode: str = "soft"

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path.strip():
            raise VTTError("Clamp path must be a non-empty string.")
        if self.mode not in ("soft", "hard"):
            raise VTTError(
                f"Unknown clamp mode '{self.mode}'. Allowed: soft, hard."
            )
        if self.max is None and self.min is None:
            raise VTTError(
                f"Clamp on '{self.path}' must specify at least one of max or min."
            )
        # Validate bound types: numeric literal (int/float, NOT bool) or string path
        for label, b in [("max", self.max), ("min", self.min)]:
            if b is None:
                continue
            if isinstance(b, bool):
                raise VTTError(
                    f"Clamp on '{self.path}' has {label}={b!r}: booleans are not "
                    f"valid bounds. Use a number or var path string."
                )
            if not isinstance(b, (int, float, str)):
                raise VTTError(
                    f"Clamp on '{self.path}' has {label}={b!r}: must be a number "
                    f"or var path string."
                )
        # If both bounds are numeric literals, validate min <= max at config time.
        # We can't validate when either is a path — those resolve at write time.
        if (isinstance(self.max, (int, float)) and not isinstance(self.max, bool)
                and isinstance(self.min, (int, float)) and not isinstance(self.min, bool)
                and self.min > self.max):
            raise VTTError(
                f"Clamp on '{self.path}': min ({self.min}) > max ({self.max})."
            )

    def to_dict(self) -> Dict[str, Any]:
        # Omit None fields to keep the saved form compact and so a clamp
        # without a min doesn't serialize "min": null which is a bit ugly.
        out: Dict[str, Any] = {"path": self.path, "mode": self.mode}
        if self.max is not None:
            out["max"] = self.max
        if self.min is not None:
            out["min"] = self.min
        return out

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ClampSpec":
        return ClampSpec(
            path=d["path"],
            max=d.get("max"),
            min=d.get("min"),
            mode=d.get("mode", "soft"),
        )


def _resolve_clamp_bound(bound: Any, entity_vars: Dict[str, Any]) -> Optional[float]:
    """Resolve a clamp bound to a numeric value, or None for "no bound."

    Numeric literal → returns as-is.
    String path → looks up in entity_vars, returns numeric value or None
                  if the path is missing or resolves to a non-numeric value.
    None / unknown type → None.
    """
    if bound is None:
        return None
    if isinstance(bound, bool):
        return None  # bools aren't valid bounds (caught at config time too)
    if isinstance(bound, (int, float)):
        return bound
    if isinstance(bound, str):
        # Resolve as a dotted path on entity vars
        cur: Any = entity_vars
        for seg in bound.split("."):
            if not isinstance(cur, dict) or seg not in cur:
                return None  # missing → no bound
            cur = cur[seg]
        if isinstance(cur, bool):
            return None  # value isn't numeric (semantically)
        if isinstance(cur, (int, float)):
            return cur
        return None  # non-numeric → no bound
    return None


def _apply_clamp(
    spec: ClampSpec,
    old_value: Any,
    new_value: Any,
    entity_vars: Dict[str, Any],
) -> Tuple[Any, bool]:
    """Apply a clamp to a numeric write. Returns (final_value, was_clamped).

    See module-level docstring for soft/hard semantics. If new_value isn't
    numeric (or is a bool), the value passes through unchanged with
    was_clamped=False.

    For soft clamps, "crossing from the legal side" is determined by checking
    whether old_value was within bounds. _MISSING (no prior value, i.e. a
    creation event) is treated as "crossing from below/above" — soft engages
    on initial writes that land outside the bounds. This matches the user
    intuition that creating a var with an out-of-range initial value should
    still respect the configured clamp.
    """
    # Non-numeric values bypass clamping entirely
    if isinstance(new_value, bool) or not isinstance(new_value, (int, float)):
        return new_value, False

    final = new_value
    clamped = False

    # --- max bound ---
    max_v = _resolve_clamp_bound(spec.max, entity_vars)
    if max_v is not None and new_value > max_v:
        if spec.mode == "hard":
            final = max_v
            clamped = True
        else:  # soft
            # Engage only if old_value was on the legal side (≤ max_v) or
            # didn't exist. _MISSING counts as "from below."
            was_legal = (
                old_value is _MISSING
                or isinstance(old_value, bool)
                or not isinstance(old_value, (int, float))
                or old_value <= max_v
            )
            if was_legal:
                final = max_v
                clamped = True

    # --- min bound (checked against `final`, not original new_value, so a
    #     max-clamp that lands above min doesn't trigger min-clamp spuriously) ---
    min_v = _resolve_clamp_bound(spec.min, entity_vars)
    if min_v is not None and final < min_v:
        if spec.mode == "hard":
            final = min_v
            clamped = True
        else:  # soft
            was_legal = (
                old_value is _MISSING
                or isinstance(old_value, bool)
                or not isinstance(old_value, (int, float))
                or old_value >= min_v
            )
            if was_legal:
                final = min_v
                clamped = True

    return final, clamped


# ============================================================================
# Var-event infrastructure
# ============================================================================
# A "var event" describes one observable change to entity.vars. Every write
# (via Entity.write_var) and every removal (via Entity.remove_var) produces
# a list of these events by diffing the old and new state of the affected
# subtree. The events are then fired as passive hooks.
#
# Why a separate VarEvent class rather than firing per-write inline:
#   1. A single user-visible write may produce many events. Writing
#      inventory.gold.coins=50 from an empty entity produces three creation
#      events (inventory, inventory.gold, inventory.gold.coins) plus three
#      on_var_written events.
#   2. The diff is computed once and the events are then materialized.
#      Keeping events as data lets us order them (top-down for created,
#      bottom-up for removed) before firing.
#   3. Tests can inspect the event list without hooking into the firing path.
# ============================================================================

@dataclass
class VarEvent:
    """A single observable change to entity.vars from one write or removal.

    Fields:
      kind             "created", "changed", or "removed" (never "written" — the
                       on_var_written catch-all subscribes to all three).
      key              dotted path that changed (e.g. "inventory.sword.damage").
      old_value        previous value at this path. None for "created". For
                       "removed" on a subtree, this carries the full removed
                       subtree as a (deep-copied) dict.
      new_value        new value at this path. None for "removed". This is
                       the value ACTUALLY stored — i.e. post-clamp if a clamp
                       engaged. Use `intended_value` to see what the caller
                       originally requested.
      intended_value   the value the caller passed to write_var before any
                       clamping. Equal to new_value when no clamp engaged.
                       None for "removed" events (the user intended absence,
                       not a value). Useful for overheal/overdamage tracking:
                       `intended_value - new_value` is the lost magnitude.
      was_clamped      True iff new_value differs from intended_value because
                       a clamp engaged on this write. False otherwise
                       (including bypass cases — a bypassed write didn't
                       clamp, even if the value would have been out of range).
    """
    kind: str       # "created" | "changed" | "removed"
    key: str
    old_value: Any
    new_value: Any
    intended_value: Any = None
    was_clamped: bool = False

    def __post_init__(self):
        # If the caller didn't explicitly set intended_value, mirror new_value
        # for create/change events (no clamping happened on this event), and
        # leave it as None for removal events (the caller intended absence).
        # write_var will override these for events where a clamp actually
        # engaged. We use a sentinel-style check rather than = field(default=
        # MISSING) to keep the dataclass declaration readable.
        if self.intended_value is None and self.kind != "removed":
            # For create/change we default intended_value to the actual new_value.
            # This means the common no-clamp case produces sensible event data
            # without every diff-side construction site having to set it.
            self.intended_value = self.new_value


def _deepcopy_value(v: Any) -> Any:
    """Deep-copy a value for safe inclusion in a VarEvent's old_value.

    We deep-copy old subtrees on removal events because the underlying dict
    has already been mutated by the time a passive observes the event — if
    the event held a reference into the old structure, a passive inspecting
    old_value would either see the post-mutation state or, worse, get a
    KeyError. Deep-copying decouples event data from current vars state.

    Only dicts/lists/tuples need deep-copying; scalars are immutable. We
    avoid importing copy.deepcopy because vars are expected to be plain
    JSON-like trees (dicts, lists, scalars) and a hand-rolled walk is both
    faster and won't choke on a stray non-pickleable object.
    """
    if isinstance(v, dict):
        return {k: _deepcopy_value(sub) for k, sub in v.items()}
    if isinstance(v, list):
        return [_deepcopy_value(sub) for sub in v]
    if isinstance(v, tuple):
        return tuple(_deepcopy_value(sub) for sub in v)
    return v  # scalar — immutable, safe to share


# Sentinel used internally to mean "no prior value at this path." We use a
# unique object rather than None because None can be a legitimate stored
# value (a formula could write `entity[hero].something = None`). Distinguishing
# "absent" from "set to None" matters for getting the created/changed split
# right.
_MISSING = object()


def _walk_subtree_keys(prefix: str, value: Any) -> List[Tuple[str, Any]]:
    """Enumerate every (dotted_key, value) pair in a subtree, including the
    root itself. Used when a whole new subtree is created or a whole subtree
    is removed: we need an event at every level inside, not just the root.

    Walk order is top-down (parent before children). The caller can reverse
    for bottom-up (removal) ordering.

    Non-dict values (scalars, lists) are leaves — we emit a single entry for
    them and don't recurse inside lists. (Lists of dicts are valid var
    content but list contents aren't subject to per-element hooks; a passive
    watching the list as a whole catches changes to it.)
    """
    out: List[Tuple[str, Any]] = [(prefix, value)]
    if isinstance(value, dict):
        for k, sub in value.items():
            sub_path = f"{prefix}.{k}" if prefix else k
            out.extend(_walk_subtree_keys(sub_path, sub))
    return out


def _diff_subtree(prefix: str, old: Any, new: Any) -> List[VarEvent]:
    """Compute the list of VarEvents that describes the transition from `old`
    to `new` at the given `prefix` path. The returned list is in a stable
    order suitable for firing as-is: creations come top-down, removals
    bottom-up, changes wherever they fall.

    Cases:
      - old is _MISSING and new is not: every (key, value) pair in the new
        subtree is a creation event, top-down.
      - old is not _MISSING and new is _MISSING: every (key, value) pair in
        the old subtree is a removal event, bottom-up. old_value for the
        root carries the whole removed subtree (deep-copied).
      - both are dicts: recurse — diff each key. Keys present in old but not
        new are removals; keys in new but not old are creations; keys in both
        are recursed-into (if either side is a dict) or directly compared
        (if both are scalars).
      - one is a dict and the other isn't (or scalar mismatch): treat the
        whole prefix as one change event. We don't emit per-leaf events for
        such a structural shift because the user replaced the whole node;
        there's no meaningful "old leaf at this path" to diff against.
      - both are scalars and equal: no event.
      - both are scalars and unequal: a single change event.
    """
    # Removal of a whole subtree
    if new is _MISSING:
        # Bottom-up: walk old, then reverse so leaves come first.
        pairs = _walk_subtree_keys(prefix, old)
        events: List[VarEvent] = []
        # For each level, old_value is the (deep-copied) subtree at that
        # level so passives can inspect what was lost. new_value is None.
        for key, val in reversed(pairs):
            events.append(VarEvent(
                kind="removed", key=key,
                old_value=_deepcopy_value(val), new_value=None,
            ))
        return events

    # Creation of a whole subtree
    if old is _MISSING:
        pairs = _walk_subtree_keys(prefix, new)
        events = []
        # Top-down: parents emitted first, then children.
        for key, val in pairs:
            events.append(VarEvent(
                kind="created", key=key, old_value=None, new_value=val,
            ))
        return events

    # Both present — diff their contents
    if isinstance(old, dict) and isinstance(new, dict):
        events = []
        old_keys = set(old.keys())
        new_keys = set(new.keys())
        # Creations first (top-down at this level; recursive walks inside
        # the subtree below will preserve top-down for nested creations).
        for k in new_keys - old_keys:
            sub_path = f"{prefix}.{k}" if prefix else k
            events.extend(_diff_subtree(sub_path, _MISSING, new[k]))
        # In-place changes for keys in both
        for k in old_keys & new_keys:
            sub_path = f"{prefix}.{k}" if prefix else k
            events.extend(_diff_subtree(sub_path, old[k], new[k]))
        # Removals last (each removal sequence is internally bottom-up)
        for k in old_keys - new_keys:
            sub_path = f"{prefix}.{k}" if prefix else k
            events.extend(_diff_subtree(sub_path, old[k], _MISSING))
        return events

    # Structural mismatch (e.g. dict <-> scalar) or scalar inequality
    if old != new:
        return [VarEvent(
            kind="changed", key=prefix,
            old_value=_deepcopy_value(old), new_value=new,
        )]
    return []


def _path_resolve(root: Any, path: str) -> Any:
    """Resolve a dotted path through a nested dict. Returns _MISSING if any
    segment is absent or descends through a non-dict. Used to capture the
    'before' value at a write site for diffing."""
    if path == "":
        return root
    cur: Any = root
    for seg in path.split("."):
        if not isinstance(cur, dict) or seg not in cur:
            return _MISSING
        cur = cur[seg]
    return cur


def _run_passive_safely(engine, p: "Passive", ctx, *, target_id: str, is_global: bool) -> str:
    """
    Run a single passive's formula and return a one-line human-readable log
    string. Catches FormulaError (and any other exception, defensively) so one
    broken passive can't cripple the whole hook fire.
    """
    # Lazy import to avoid logic <-> formula import cycle.
    from formula import FormulaError
    label = f"global passive `{p.id}`" if is_global else f"passive `{target_id}.{p.id}`"
    try:
        result = engine.eval_program(p.formula, ctx)
        if result is None:
            return f"⚙️ {label} fired on `{target_id}` ({p.when})"
        return f"⚙️ {label} fired on `{target_id}` ({p.when}) → {result!r}"
    except FormulaError as ex:
        return f"⚠️ {label} on `{target_id}` ({p.when}) FAILED: {ex}"
    except Exception as ex:
        # Defensive: programmer-error or unexpected bug shouldn't nuke a turn.
        return f"💥 {label} on `{target_id}` ({p.when}) CRASHED: {type(ex).__name__}: {ex}"


# -------------------------
# GameSystem (stores global rules for a game system, so not everything has to be manually set each match)
# -------------------------
@dataclass
class GameSystem:
    name: str
    # Per-system overrides now stored as Rule objects keyed by rule key
    settings: Dict[str, Rule] = field(default_factory=dict)
 
    def get(self, key: str) -> Any:
        if key in self.settings:
            return self.settings[key].value
        # fall back to engine default from registry
        return DEFAULT_SYSTEM_SETTINGS.get(key)
    
    def set(self, key: str, value: Any) -> None:
        # Build a Rule object from the registry entry and store/overwrite it
        if key not in RULES_REGISTRY:
            allowed = ", ".join(sorted(RULES_REGISTRY.keys()))
            raise VTTError(f"Unknown setting '{key}'. Allowed: {allowed}")
        reg = RULES_REGISTRY[key]
        self.settings[key] = Rule(
            key=key,
            value=value,
            schema=reg["schema"],
            description=reg["desc"],
        )
    
    def to_dict(self) -> Dict[str, Any]:
        # Serialize Rule objects in a stable shape
        return {
            "name": self.name,
            "settings": {
                k: {
                    "value": r.value,
                    "schema": r.schema,
                    "description": r.description,
                } for k, r in self.settings.items()
            },
        }
    
    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "GameSystem":
        raw = d.get("settings", {}) or {}
        settings: Dict[str, Rule] = {}
        # Back-compat: allow old saves that stored plain values
        for k, v in raw.items():
            if isinstance(v, dict) and "value" in v and "schema" in v:
                val = v["value"]
                schema = v.get("schema") or RULES_REGISTRY.get(k, {}).get("schema", {})
                desc = v.get("description") or RULES_REGISTRY.get(k, {}).get("desc", "")
            else:
                val = v
                schema = RULES_REGISTRY.get(k, {}).get("schema", {})
                desc = RULES_REGISTRY.get(k, {}).get("desc", "")
            settings[k] = Rule(key=k, value=val, schema=schema, description=desc)
        return GameSystem(name=d["name"], settings=settings)

# -------------------------
# Entity
# -------------------------

@dataclass
class Entity:
    name: str
    x: int
    y: int
    id: str# explicit, user-provided
    team: Optional[str] = None
    status: Set[str] = field(default_factory=set)

    # structured variable bag — hp, max_hp, and initiative now live HERE
    # under keys defined by the GameSystem (defaults: "hp", "max_hp", "initiative")
    vars: Dict[str, Any] = field(default_factory=dict)

#PASSIVES NOT YET TRULY IMPLEMENTED 
#    #entity-scoped passives
    passives: Dict[str, Passive] = field(default_factory=dict)

    # Entity-scoped clamp overrides. Keyed by the var path being clamped.
    # When an entity defines a clamp on a path, it WHOLLY replaces any
    # system-level (default_clamps) clamp on that same path — there's no
    # field-by-field merge. To customize one bound while keeping the other,
    # copy the system clamp and adjust. See ClampSpec docstring for details.
    clamps: Dict[str, "ClampSpec"] = field(default_factory=dict)

    facing: Direction = "up"# will be set to a default by Match when adding

    #connect Entity to the Match (so functions like moving an entity can still access map size data, etc.)
    _match: "Match | None" = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        tested_id = str(self.id.strip().lower())
        # Disallow reserved ids that the CLI uses as shorthands (current/this are used if you want to get the id of the entity whose turn it is right now)
        if tested_id in RESERVED_IDS:
            raise ReservedId(f"Entity id '{tested_id}' is a special reserved id and cannot be used when creating entities. Reserved ids are used for formulas and commands, like 'this' being used to automatically get the id of the entity whose turn it is.")
        if self.facing not in ALLOWED_DIRECTIONS:
            self.facing = "up"

    # ---------- vital variable name helpers ----------
    def _vital_var_names(self) -> Tuple[str, str, str]:
        """Return (hp_var, max_hp_var, turnorder_var) from bound match rules, or engine defaults."""
        if self._match:
            rules = self._match.rules
            return (
                rules.get("hp_var", "hp"),
                rules.get("max_hp_var", "max_hp"),
                rules.get("turnorder_var", "initiative"),
            )
        return ("hp", "max_hp", "initiative")

    def protected_var_names(self) -> Set[str]:
        """Return the set of top-level var keys that must not be deleted."""
        hp_v, mhp_v, init_v = self._vital_var_names()
        return {hp_v, mhp_v, init_v}

    # ---------- property accessors (transparent to rest of codebase) ----------
    @property
    def hp(self) -> int:
        hp_var, _, _ = self._vital_var_names()
        return int(self.vars.get(hp_var, 0))

    @hp.setter
    def hp(self, value: int):
        # Route through write_var so on_var_created / on_var_changed fire
        # for passives watching the entity's hp var. write_var is a no-op
        # event-firing-wise when the entity isn't yet bound to a match
        # (e.g. during deserialization), so this stays safe pre-bind.
        hp_var, _, _ = self._vital_var_names()
        self.write_var(hp_var, int(value))

    @property
    def max_hp(self) -> Optional[int]:
        _, max_hp_var, _ = self._vital_var_names()
        v = self.vars.get(max_hp_var)
        return int(v) if v is not None else None

    @max_hp.setter
    def max_hp(self, value):
        # Route through write_var (set) or remove_var (None unsets the var).
        # Unsetting fires on_var_removed; setting fires created/changed
        # depending on whether the var existed before.
        _, max_hp_var, _ = self._vital_var_names()
        if value is None:
            # Only remove if currently present — remove_var raises NotFound
            # if the var doesn't exist, which would surprise callers of the
            # setter who don't know the underlying state.
            if max_hp_var in self.vars:
                self.remove_var(max_hp_var)
        else:
            self.write_var(max_hp_var, int(value))

    @property
    def initiative(self) -> Optional[int]:
        _, _, init_var = self._vital_var_names()
        v = self.vars.get(init_var)
        return int(v) if v is not None else None

    @initiative.setter
    def initiative(self, value):
        # Same pattern as max_hp.setter: route through write_var or
        # remove_var so var hooks fire.
        _, _, init_var = self._vital_var_names()
        if value is None:
            if init_var in self.vars:
                self.remove_var(init_var)
        else:
            self.write_var(init_var, int(value))

    # ---------- binding ----------
    #bind this entity to a specific match
    def bind(self, match: "Match", *, set_spawn_facing: bool = True):
        self._match = match
        if set_spawn_facing and match.in_bounds(self.x, self.y):
            try:
                # Initial facing according to system (only when spawning, not loading)
                self.facing = match._spawn_facing(self.x, self.y)
            except Exception:
                pass

    def _require_match(self) -> "Match":
        if not self._match:
            raise VTTError("Entity is not bound to a match")
        return self._match

    # ---------- minimal primitives ----------
    @property
    def is_alive(self) -> bool:
        return self.hp > 0

    def move_to(self, x: int, y: int):
        self.x, self.y = x, y

    def take_damage(self, amount: int):
        # Pass the raw post-damage intent through the chokepoint. If a min
        # clamp on hp is configured, it'll engage; otherwise hp can go
        # negative (which is meaningful — negative hp distinguishes
        # "downed but revivable" from larger numbers indicating overkill).
        self.hp = self.hp - max(0, amount)

    def heal(self, amount: int):
        # Pass the raw post-heal intent through the chokepoint. The default
        # system ships with a soft clamp on hp ≤ max_hp, so normal heals
        # clamp to max_hp. To allow overheal, callers should write to hp
        # directly with bypass_clamp=True (only available at the command
        # layer / direct write_var call — not from formulas).
        self.hp = self.hp + max(0, amount)

    # ---------- high-level actions (Entity-owned) ----------
    def spawn(self, match: "Match", x: int, y: int, initiative: Optional[int] = None) -> Tuple[str, List[str]]:
        """
        Add this entity to a match at (x,y).
        Validates bounds/occupancy, sets facing, registers in turn order.
        Also validates that the entity has the required vital variables
        (HP var) for the match's game system.

        Returns (entity_id, log_lines). log_lines contains any passive-fire
        messages produced by the on_entity_spawned hook. NOTE: Match.from_dict
        uses Entity.bind() instead of spawn(), so loading a save does NOT
        re-trigger on_entity_spawned.
        """
        if self._match is not None:
            raise VTTError(f"Entity '{self.id}' is already in a match")
        if not match.in_bounds(x, y):
            raise OutOfBounds(f"({x},{y}) outside {match.grid_width}x{match.grid_height}")
        if match.is_occupied(x, y):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        if self.id in match.entities:
            raise DuplicateId(f"Entity id '{self.id}' already exists in this match")

        # --- Validate vital vars against game system ---
        hp_var = match.rules.get("hp_var", "hp")
        if hp_var not in self.vars:
            existing = ", ".join(sorted(self.vars.keys())) or "(none)"
            raise VTTError(
                f"Entity '{self.id}' is missing required HP variable '{hp_var}' "
                f"for game system '{match.system_name}'. "
                f"Entity vars have: {existing}"
            )
        # Auto-fill max_hp if missing. Routed through write_var so any
        # passives watching max_hp see this initial assignment as an
        # on_var_created event. (At this point the entity is about to be
        # bound to the match, but match._var_event_depth is 0 and entities
        # is set below — so events fire after the match.entities mapping
        # is established. We bind first to make write_var event-firing
        # work; the actual write happens after binding completes.)
        max_hp_var = match.rules.get("max_hp_var", "max_hp")
        needs_max_hp_default = max_hp_var not in self.vars
        default_max_hp_value: Optional[int] = (
            int(self.vars[hp_var]) if needs_max_hp_default else None
        )

        self.move_to(x, y)

        self.bind(match, set_spawn_facing=True)
        if initiative is not None:
            self.initiative = initiative

        match.entities[self.id] = self
        match._rebuild_turn_order()
        # Now that the entity is registered, apply any deferred max_hp
        # default via write_var so any passives watching max_hp see the
        # initial creation event.
        if needs_max_hp_default:
            self.write_var(max_hp_var, default_max_hp_value)
        log = match.fire_hook("on_entity_spawned", target_ids=[self.id])
        return (self.id, log)

    def remove(self):
        """
        Remove this entity from its match and turn order.
        """
        m = self._require_match()
        if self.id in m.entities:
            del m.entities[self.id]
        # scrub from turn order & clamp active index
        if self.id in m.turn_order:
            idx = m.turn_order.index(self.id)
            m.turn_order.remove(self.id)
            if m.active_index >= len(m.turn_order):
                m.active_index = max(0, len(m.turn_order) - 1)
            elif m.active_index > idx:
                m.active_index = max(0, m.active_index - 1)
        self._match = None
        m._rebuild_turn_order()

    # Teleport (absolute move)
    def tp(self, x: int, y: int):
        m = self._require_match()
        if not m.in_bounds(x, y):
            raise OutOfBounds(f"({x},{y}) outside {m.grid_width}x{m.grid_height}")
        if m.is_occupied(x, y, ignore_entity_id=self.id):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        self.move_to(x, y)

    # Stepwise move (final cell must be free; rotate per step)
    def move_dirs(self, moves: list[tuple[str, int]]):
        m = self._require_match()
        x, y = self.x, self.y
        for direction, count in moves:
            d = direction.lower()
            dx, dy = 0, 0
            if d in ("up", "u"): dy = -1
            elif d in ("down", "d"): dy = 1
            elif d in ("left", "l"): dx = -1
            elif d in ("right", "r"): dx = 1
            else: raise VTTError(f"Unknown direction '{direction}'")
            for _ in range(max(1, int(count))):
                nx, ny = x + dx, y + dy
                if not m.in_bounds(nx, ny):
                    raise OutOfBounds(f"({nx},{ny}) outside {m.grid_width}x{m.grid_height}")
                self.facing = {(-1,0):"left",(1,0):"right",(0,-1):"up",(0,1):"down"}[(dx,dy)]
                x, y = nx, ny
        if m.is_occupied(x, y, ignore_entity_id=self.id):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        self.move_to(x, y)

    # Stats/initiative (entity-owned)
    def damage_entity(self, amount: int):
        was_alive = self.is_alive
        self.take_damage(amount)
        if was_alive and not self.is_alive:
            self._require_match()._rebuild_turn_order()

    def heal_entity(self, amount: int):
        was_alive = self.is_alive
        self.heal(amount)
        if (not was_alive) and self.is_alive:
            self._require_match()._rebuild_turn_order()

    def set_initiative_entity(self, value: int):
        self.initiative = value
        self._require_match()._rebuild_turn_order()

    # ---------- var mutation chokepoint ----------
    # All writes to entity.vars should go through write_var / remove_var
    # rather than mutating self.vars directly. These methods:
    #   1. Diff the affected subtree to produce VarEvents
    #   2. Apply the mutation
    #   3. Fire matching passives via the parent Match (if bound)
    #
    # Direct mutation of self.vars is acceptable in two narrow cases:
    #   (a) Pre-bind construction — building up vars before Entity.spawn /
    #       bind, when there's no match to route events through. write_var
    #       gracefully no-ops the firing in this case, so routing through
    #       it is also fine — direct mutation is just slightly cheaper.
    #   (b) Deserialization (Entity.from_dict) — bulk-restores vars from
    #       saved state. Loading shouldn't fire as-if-new creation events
    #       since the data was already there before the save.
    # Outside these two cases, ALWAYS use write_var/remove_var. The hp,
    # max_hp, and initiative property setters now route through the
    # chokepoint, so callers using `e.hp = 30` correctly fire var hooks.
    #
    # The silent variant (remove_var_silent) is the documented escape hatch
    # for "drop this without cascading effects." Use it sparingly — passives
    # that depend on observing removals will miss the event.

    def write_var(self, path: str, value: Any, *, bypass_clamp: bool = False) -> List[str]:
        """Write `value` to vars at the dotted `path`, firing var hooks.

        Returns the list of human-readable log lines from any passives that
        fired. Empty list if no passives matched (the most common case).

        Path semantics:
          - "hp" writes to vars["hp"]
          - "inventory.sword.damage" writes to vars["inventory"]["sword"]["damage"],
            creating intermediate dicts as needed.
          - "" (empty) is rejected; we don't allow replacing vars wholesale
            through this API.

        Clamp handling:
          - If a clamp applies to `path` (entity-level overrides system-level
            via Match._effective_clamp), the value is run through _apply_clamp
            BEFORE the actual write. The stored value is the (possibly
            modified) clamped value. The resulting event's new_value reflects
            what was actually stored; intended_value carries the pre-clamp
            value the caller passed in; was_clamped indicates whether
            clamping changed anything.
          - Non-numeric values (strings, dicts, lists, bools) bypass clamping
            entirely — _apply_clamp recognizes them and passes through.
          - bypass_clamp=True skips the clamp lookup entirely. The event's
            was_clamped is False (no clamping happened), and intended_value
            equals new_value (since nothing was modified). Use this for
            overheal effects, GM debugging, or any deliberate override.

        Event semantics: see VarEvent and _diff_subtree. In short, the diff
        between the old and new value at `path` produces created/changed/
        removed events. Each event also fires the on_var_written catch-all.

        Ancestor handling: writing `inventory.sword.damage = 12` when
        `inventory` doesn't yet exist creates `inventory` and `inventory.sword`
        as side effects of the write. Without explicit handling those
        intermediates would be silent — a passive watching `inventory` for
        creation events would miss the event. So we walk up from the written
        path BEFORE applying the write and record which ancestors are
        missing; each missing ancestor produces its own creation event
        (top-down, parent before child) AFTER the leaf-level diff. The leaf
        diff itself sees `_MISSING -> value`, so it produces a creation event
        for the exact written path. Together this emits events for every
        newly-created level along the path, matching the documented "top-down
        for creation" semantics.
        """
        if not path:
            raise VTTError("Variable path cannot be empty.")

        # Snapshot whether this is the top-level entry into a write/event
        # chain. We only drain the warning buffer at top-level exit so
        # nested writes (from passive formulas) don't surface warnings to
        # their immediate caller — those bubble up to the outermost write.
        is_top_level_write = (
            self._match is not None and self._match._var_event_depth == 0
        )

        # Remember the raw intent BEFORE any clamping — this becomes
        # intended_value on the leaf event AND on the attempt hook.
        intended_value = value
        was_clamped = False

        # Capture the prior value at the exact path (may be _MISSING). Used
        # by both the clamp logic AND the attempt hook (the attempt fires
        # BEFORE the write, so old_value reflects pre-write state).
        old = _path_resolve(self.vars, path)

        # Apply clamp, unless bypassed or there's no match to consult for
        # clamp rules. Clamps only meaningful for numeric writes; _apply_clamp
        # short-circuits non-numeric values.
        if not bypass_clamp and self._match is not None:
            spec = self._match._effective_clamp(self, path)
            if spec is not None:
                value, was_clamped = _apply_clamp(
                    spec, old, value, self.vars
                )

        # Fire on_var_write_attempt BEFORE mutation. This fires for every
        # write call regardless of whether the resulting diff produces
        # events — so passives that need to track attempted actions
        # (overheal magnitude even when hp is already at cap) observe them.
        # The attempt log is collected and merged with the post-write event
        # log in the final return.
        attempt_log: List[str] = []
        if self._match is not None:
            attempt_log = self._match._fire_var_attempt(
                self.id, path, old, value, intended_value, was_clamped
            )

        # Identify which ancestor segments along the path are missing BEFORE
        # the write. The keys are accumulated dotted paths (e.g. "inventory",
        # then "inventory.sword"). We stop at the parent of the leaf — the
        # leaf itself is handled by the main diff below.
        segments = path.split(".")
        ancestor_events: List[VarEvent] = []
        for i in range(1, len(segments)):
            ancestor_path = ".".join(segments[:i])
            if _path_resolve(self.vars, ancestor_path) is _MISSING:
                # This ancestor doesn't exist yet — record a placeholder
                # creation event. We'll fill in new_value after the write
                # (which is when the actual ancestor dict exists).
                ancestor_events.append(VarEvent(
                    kind="created", key=ancestor_path,
                    old_value=None, new_value=None,  # filled after write
                ))

        # Apply the (possibly clamped) write (creating intermediates as needed).
        # NOTE: an attempt-hook passive that ran above may have already
        # mutated vars (legitimately, through write_var). That's fine — we
        # re-resolve `old` against the current state for the diff, so we
        # capture only the change attributable to THIS write call.
        old_post_attempt = _path_resolve(self.vars, path)
        self._set_path(path, value)
        # Diff old vs new at this path and collect the resulting events
        leaf_events = _diff_subtree(path, old_post_attempt, value)
        # Now we can fill in new_value for the ancestor events by re-resolving
        # each ancestor's current value (post-write, the intermediates exist).
        for ev in ancestor_events:
            ev.new_value = _path_resolve(self.vars, ev.key)
            # Mirror intended_value for ancestor creates (no clamping
            # happens at the ancestor level — the clamp only acts on the
            # leaf path that was written).
            ev.intended_value = ev.new_value

        # Stamp intended_value / was_clamped onto the leaf event(s) that
        # correspond exactly to the written path. _diff_subtree may have
        # produced multiple events for a subtree write, but only the
        # event with key == path represents "the write the caller made";
        # nested events inside a subtree write are themselves not clamped
        # (the clamp only applies to the leaf path being written).
        for ev in leaf_events:
            if ev.key == path and ev.kind != "removed":
                ev.intended_value = intended_value
                ev.was_clamped = was_clamped

        # Fire ancestors first (top-down), then the leaf events. The leaf
        # event for the written path itself is also a 'created' if old was
        # _MISSING — _diff_subtree already produced it as the first event.
        # Combine the attempt log (fired first) with the event log so the
        # caller sees everything in chronological order.
        event_log = self._fire_var_events(ancestor_events + leaf_events)
        combined = attempt_log + event_log
        # Drain accumulated warnings ONLY at top-level. Nested writes
        # (from passive formulas) accumulate warnings on the match but
        # don't surface them — the outermost call collects them all.
        if is_top_level_write and self._match is not None and self._match._var_event_warnings:
            combined.extend(self._match._var_event_warnings)
            self._match._var_event_warnings = []
        return combined

    def remove_var(self, path: str) -> List[str]:
        """Remove the var at `path`, firing on_var_removed (plus on_var_written).

        Returns log lines from any passives that fired. Raises NotFound if
        the path doesn't exist. For removal that bypasses events entirely,
        use remove_var_silent.

        If the removed value was a subtree, every level inside it fires its
        own removal event (bottom-up: leaves first, then containers).
        """
        if not path:
            raise VTTError("Variable path cannot be empty.")
        is_top_level_write = (
            self._match is not None and self._match._var_event_depth == 0
        )
        old = _path_resolve(self.vars, path)
        if old is _MISSING:
            raise NotFound(f"Variable '{path}' not found on entity '{self.id}'.")
        self._del_path(path)
        events = _diff_subtree(path, old, _MISSING)
        log = self._fire_var_events(events)
        if is_top_level_write and self._match is not None and self._match._var_event_warnings:
            log.extend(self._match._var_event_warnings)
            self._match._var_event_warnings = []
        return log

    def remove_var_silent(self, path: str) -> None:
        """Remove the var at `path` without firing any hooks. The escape
        hatch for cleanup operations where cascading effects would be
        unwanted. No log lines returned (none generated)."""
        if not path:
            raise VTTError("Variable path cannot be empty.")
        if _path_resolve(self.vars, path) is _MISSING:
            raise NotFound(f"Variable '{path}' not found on entity '{self.id}'.")
        self._del_path(path)

    # ---- internal: raw path-walk helpers (no event firing) ----
    # These are called by write_var/remove_var AFTER the diff is computed.
    # They are NOT public API — anything that should fire events goes through
    # write_var/remove_var instead.

    def _set_path(self, path: str, value: Any) -> None:
        """Raw deep-set on self.vars. Creates intermediate dicts."""
        keys = path.split(".")
        cur = self.vars
        for k in keys[:-1]:
            node = cur.get(k)
            if not isinstance(node, dict):
                node = {}
                cur[k] = node
            cur = node
        cur[keys[-1]] = value

    def _del_path(self, path: str) -> None:
        """Raw deep-delete on self.vars. Caller must have verified existence."""
        keys = path.split(".")
        cur = self.vars
        for k in keys[:-1]:
            cur = cur[k]
        del cur[keys[-1]]

    def _fire_var_events(self, events: List[VarEvent]) -> List[str]:
        """Fire matching passives for each event. Returns log lines.

        Events are processed one at a time in the order produced by the diff
        (top-down for creations, bottom-up for removals). For each event, we
        fire on_var_<kind> passives followed by on_var_written passives —
        within each, globals first then entity passives, both filtered by
        target/scope/matches_event.

        The recursion-depth safeguard lives in Match._fire_var_event (not
        here) since it's a property of the match-wide firing context. The
        warning buffer on the match is drained by the public entry points
        (write_var/remove_var) at top-level chain exit — NOT here, because
        attempt firing also accumulates warnings and we want a single drain
        site per top-level write call.
        """
        if not events:
            return []
        m = self._match
        if m is None:
            # Detached entity (rare — only during construction before bind/spawn).
            return []
        log: List[str] = []
        for ev in events:
            log.extend(m._fire_var_event(self.id, ev))
        return log

    # ---------- passive management (entity-scoped) ----------
    def add_passive(self, p: "Passive") -> None:
        """Attach a Passive to this entity. Validates that its formula parses."""
        if p.id in self.passives:
            raise DuplicateId(
                f"Passive id '{p.id}' already exists on entity '{self.id}'."
            )
        # Early-fail on a malformed formula so the user sees the error now,
        # not later when the hook fires.
        from formula import validate_formula
        validate_formula(p.formula, mode="exec")
        self.passives[p.id] = p

    def remove_passive(self, pid: str) -> None:
        if pid not in self.passives:
            raise NotFound(f"Passive '{pid}' not found on entity '{self.id}'.")
        del self.passives[pid]

    # ---------- serialization ----------
    def to_dict(self) -> Dict[str, Any]:
        # hp/max_hp/initiative now live in vars; include top-level copies for save-file readability
        # and backwards compatibility with older loaders
        hp_var, max_hp_var, init_var = self._vital_var_names()
        return {
            "name": self.name,
            "hp": self.vars.get(hp_var, 0),         # backwards-compat mirror
            "x": self.x,
            "y": self.y,
            "id": self.id,
            "max_hp": self.vars.get(max_hp_var),     # backwards-compat mirror
            "team": self.team,
            "status": list(self.status),
            "initiative": self.vars.get(init_var),   # backwards-compat mirror
            "vars": dict(self.vars),
            "_vital_in_vars": True,                  # flag: vital data lives in vars
            "passives": {pid: p.to_dict() for pid, p in self.passives.items()},
            "clamps": {path: c.to_dict() for path, c in self.clamps.items()},
            "facing": self.facing,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Entity":
        vars_dict = dict(data.get("vars", {}))
        # Migrate legacy saves ONLY (before vital vars moved into vars).
        # New-format saves set _vital_in_vars=True; skip migration for those
        # to avoid creating duplicate keys when custom var names are used.
        if not data.get("_vital_in_vars", False):
            if "hp" in data and "hp" not in vars_dict:
                vars_dict["hp"] = int(data["hp"])
            if "max_hp" in data and data.get("max_hp") is not None and "max_hp" not in vars_dict:
                vars_dict["max_hp"] = int(data["max_hp"])
            if "initiative" in data and data.get("initiative") is not None and "initiative" not in vars_dict:
                vars_dict["initiative"] = int(data["initiative"])
        e = Entity(
            name=data["name"],
            x=int(data["x"]),
            y=int(data["y"]),
            id=str(data["id"]),
            team=data.get("team"),
            status=set(data.get("status", [])),
            vars=vars_dict,
            facing=data.get("facing", "up"),
        )
        for pid, pd in (data.get("passives", {}) or {}).items():
            e.passives[pid] = Passive.from_dict(pd)
        # Clamps are a recent addition; old saves won't have the key.
        for path, cd in (data.get("clamps", {}) or {}).items():
            e.clamps[path] = ClampSpec.from_dict(cd)
        return e

# -------------------------
# Match
# -------------------------
@dataclass
class Match:
    id: str
    name: str
    grid_width: int
    grid_height: int

    # Game system binding - currently NOT YET DIRECTLY CONNECTED TO A GAMESYSTEM CLASS, JUST COPYING THE NAME AND DICTIONARY OF RULES FROM IT.
    system_name: str

    entities: Dict[str, Entity] = field(default_factory=dict)
    turn_order: List[str] = field(default_factory=list)
    active_index: int = 0
    #global turn counter, starts at 1. global turn here increments by 1 after EVERY entity had its turn and the cycle resets
    turn_number: int = 1
    # Tracks whether the very first `on_round_start`/`on_turn_start` have fired
    # for this match. False until the first `Match.next_turn()` call. Used to
    # make that first call begin the round (fire start-hooks for active_index)
    # rather than advance past entity 0.
    round_started: bool = False
    # Game system binding - currently NOT YET DIRECTLY CONNECTED TO A GAMESYSTEM CLASS, JUST COPYING THE DICTIONARY OF RULES FROM IT.
    rules: Dict[str, Any] = field(default_factory=dict)  # denormalized copy for fast access

    #global passives that apply to every entity in turn order on each hook fire
    global_passives: Dict[str, Passive] = field(default_factory=dict)

    # ---- runtime-only state for var-hook recursion safeguard ----
    # Tracks how deep we are in a chain of var-hook fires. A passive's
    # formula can write vars, which produces more events, which fire more
    # passives. When this exceeds rules["var_hook_recursion_limit"] the
    # firing path emits a single warning and stops firing further hooks.
    # NOT serialized — always starts at 0 on a fresh load.
    _var_event_depth: int = field(default=0, repr=False)
    # Latched once the depth limit is hit during a single chain, reset to
    # False when the chain unwinds (depth back to 0). Prevents the warning
    # from being spammed at every event past the threshold.
    _var_event_warned: bool = field(default=False, repr=False)
    # Buffer of warning log lines accumulated during a var-event chain.
    # The chain can fire nested writes whose warnings emerge deep in the
    # call stack — they need to bubble up to the top-level caller for
    # display, but the simple "return log lines" pattern only sees
    # whichever return value its direct callee produced. So we accumulate
    # warnings on the Match and the top-level entry point (write_var /
    # remove_var on Entity) flushes them at chain exit.
    _var_event_warnings: List[str] = field(default_factory=list, repr=False)

    # ---- global constraints / helpers (unchanged in spirit) ----
    def in_bounds(self, x: int, y: int) -> bool:
        return 1 <= x <= self.grid_width and 1 <= y <= self.grid_height

    def is_occupied(self, x: int, y: int, ignore_entity_id: Optional[str] = None) -> bool:
        for eid, e in self.entities.items():
            if ignore_entity_id and eid == ignore_entity_id:
                continue
            if e.x == x and e.y == y and e.is_alive:
                return True
        return False

    # ------------- turns -------------

    def _rebuild_turn_order(self):
        prev_current_id = None
        if 0 <= self.active_index < len(self.turn_order):
            prev_current_id = self.turn_order[self.active_index]

        ordered = sorted(
            [e for e in self.entities.values() if e.initiative is not None and e.is_alive],
            key=lambda e: (-e.initiative, e.name.lower(), e.id)
        )
        self.turn_order = [e.id for e in ordered]

        if prev_current_id and prev_current_id in self.turn_order:
            self.active_index = self.turn_order.index(prev_current_id)
        else:
            self.active_index = 0 if self.turn_order else 0

    def current_entity_id(self) -> Optional[str]:
        if not self.turn_order:
            return None
        return self.turn_order[self.active_index]

    def next_turn(self) -> Tuple[Optional[str], List[str]]:
        """
        Advance the turn. Returns (new_active_entity_id, log_lines) where
        log_lines contains human-readable messages for any passives that
        fired (or failed) during the transition.

        The FIRST call after match creation behaves specially: it does NOT
        advance, but instead fires `on_round_start` then `on_turn_start` for
        the entity already at active_index, marking the match as started.
        Subsequent calls run the normal end → advance → start cycle.
        """
        if not self.turn_order:
            return (None, [])
        log: List[str] = []
        # First-ever next_turn call: begin round 1 without advancing.
        if not self.round_started:
            self.round_started = True
            log.extend(self.fire_hook("on_round_start"))
            cur = self.turn_order[self.active_index]
            log.extend(self.fire_hook("on_turn_start", target_ids=[cur]))
            return (cur, log)

        # Normal transition.
        cur = self.turn_order[self.active_index]
        log.extend(self.fire_hook("on_turn_end", target_ids=[cur]))
        new_index = (self.active_index + 1) % len(self.turn_order)
        wrapped = (new_index == 0)
        if wrapped:
            log.extend(self.fire_hook("on_round_end"))
        self.active_index = new_index
        if wrapped:
            self.turn_number += 1
            log.extend(self.fire_hook("on_round_start"))
        new_cur = self.turn_order[self.active_index]
        log.extend(self.fire_hook("on_turn_start", target_ids=[new_cur]))
        return (new_cur, log)

    def _effective_clamp(self, entity: "Entity", path: str) -> Optional["ClampSpec"]:
        """Return the clamp that applies to `path` on this entity, or None.

        Entity-level clamps WHOLLY override system-level clamps on the same
        path (replace, not field-by-field merge). See ClampSpec docstring.
        Returns None if neither level defines a clamp for this path.

        The rule lookup goes through self.rules.get("default_clamps", []),
        which produces an empty list for matches whose rules dict predates
        this commit (graceful backward-compat for old saves).
        """
        # Entity-level override wins
        if path in entity.clamps:
            return entity.clamps[path]
        # System-level fallback. Rule values are stored as list[dict] (the
        # serialized form) for save/load compatibility. We convert per-lookup
        # rather than caching because there are few clamps per system and
        # this is simpler than tracking cache invalidation.
        for spec_dict in self.rules.get("default_clamps", []):
            if not isinstance(spec_dict, dict):
                continue
            if spec_dict.get("path") == path:
                try:
                    return ClampSpec.from_dict(spec_dict)
                except VTTError:
                    # Malformed clamp in saved data — skip rather than crash
                    # the whole write path. The GM can fix it with !gclamp.
                    continue
        return None

    def fire_hook(self, when: str, *, target_ids: Optional[List[str]] = None) -> List[str]:
        """
        Fire every passive matching `when` for each target entity.

        target_ids defaults to entities currently in `turn_order` (alive and
        with initiative). For each target, fires global passives first (in
        insertion order), then the target's own entity passives (in insertion
        order). Only passives whose own `when` matches are run.

        For each fire: `self` = the target entity; `this` = current_entity_id().

        Returns a list of human-readable log lines (one per fired passive).
        Empty if no passives matched.
        """
        if when not in HOOK_NAMES:
            return []
        # Lazy import to avoid logic <-> formula import cycle.
        from formula import FormulaEngine, EvalCtx

        log: List[str] = []
        if target_ids is None:
            target_ids = list(self.turn_order)
        this_id = self.current_entity_id()
        engine = FormulaEngine(self)

        for tid in target_ids:
            e = self.entities.get(tid)
            if e is None:
                continue
            ctx = EvalCtx(this=this_id, target=tid)
            # Globals first (in insertion order).
            for pid, p in list(self.global_passives.items()):
                if p.when != when:
                    continue
                log.append(_run_passive_safely(engine, p, ctx, target_id=tid, is_global=True))
            # Then entity-owned passives.
            for pid, p in list(e.passives.items()):
                if p.when != when:
                    continue
                log.append(_run_passive_safely(engine, p, ctx, target_id=tid, is_global=False))
        return log

    # ---- var-event firing ----
    # _fire_var_event is the per-event entry point called by Entity._fire_var_events
    # after a write or removal produces a list of VarEvents. Each event fires
    # two waves of passives:
    #   1. Passives subscribing to the event's exact hook (on_var_created,
    #      on_var_changed, or on_var_removed)
    #   2. Passives subscribing to on_var_written (the catch-all)
    # Within each wave: globals first (insertion order), then the affected
    # entity's own passives. Both are filtered through passive.matches_event
    # so that only passives whose target/scope match the changed_key fire.
    #
    # Recursion-depth guard: a passive's formula may itself write vars, which
    # produces more events and potentially infinite loops. We track depth on
    # self._var_event_depth; when it exceeds the limit from the
    # "var_hook_recursion_limit" rule, further var-event firing is suppressed
    # and a single warning log line is emitted. The actual writes still
    # happen — only the EVENT firing is suppressed — so data isn't lost.
    def _fire_var_event(self, entity_id: str, ev: VarEvent) -> List[str]:
        """Fire all matching passives for one VarEvent. Returns log lines."""
        # Recursion-depth guard (configured via gamerule, default 128)
        limit = int(self.rules.get("var_hook_recursion_limit", 128))
        if self._var_event_depth >= limit:
            # Stash the warning ONCE per chain. The buffer is drained by the
            # top-level entry point (Entity.write_var / remove_var) so the
            # warning surfaces to the caller even though it's generated deep
            # in the recursion stack.
            if not self._var_event_warned:
                self._var_event_warned = True
                self._var_event_warnings.append(
                    f"⚠️ var-hook recursion limit ({limit}) reached on event "
                    f"`{ev.kind}` for `{entity_id}.{ev.key}`; suppressing "
                    f"further hook firing for this chain. Check for infinite "
                    f"loops in your var passives."
                )
            return []

        self._var_event_depth += 1
        try:
            return self._fire_var_event_inner(entity_id, ev)
        finally:
            self._var_event_depth -= 1
            # When unwinding back to depth 0, reset the warning latch so the
            # next user operation gets a fresh shot at the limit. The
            # warnings buffer is drained externally (by the top-level entry
            # point) so we don't clear it here.
            if self._var_event_depth == 0:
                self._var_event_warned = False

    def _fire_var_event_inner(self, entity_id: str, ev: VarEvent) -> List[str]:
        """Inner firing logic (separated so the depth guard can wrap it)."""
        from formula import FormulaEngine, EvalCtx
        e = self.entities.get(entity_id)
        if e is None:
            return []  # entity gone mid-firing (defensive — shouldn't happen)

        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        # Var-event-specific context: changed_key/old_value/new_value/hook_name
        # /intended_value/was_clamped are exposed to the formula via
        # EvalCtx.extras. See formula.HOOK_CONTEXT_NAMES.
        kind_hook = f"on_var_{ev.kind}"  # on_var_created/changed/removed
        extras = {
            "changed_key":    ev.key,
            "old_value":      ev.old_value,
            "new_value":      ev.new_value,
            "hook_name":      kind_hook,
            "intended_value": ev.intended_value,
            "was_clamped":    ev.was_clamped,
        }
        ctx = EvalCtx(this=this_id, target=entity_id, extras=extras)

        log: List[str] = []

        # Helper to fire one passive if it matches the event
        def _maybe_fire(p: "Passive", is_global: bool) -> None:
            if not p.matches_event(ev.key):
                return
            log.append(_run_passive_safely(
                engine, p, ctx, target_id=entity_id, is_global=is_global,
            ))

        # Wave 1: passives subscribed to the exact event kind
        for p in self.global_passives.values():
            if p.when == kind_hook:
                _maybe_fire(p, is_global=True)
        for p in e.passives.values():
            if p.when == kind_hook:
                _maybe_fire(p, is_global=False)

        # Wave 2: on_var_written catch-all passives
        for p in self.global_passives.values():
            if p.when == "on_var_written":
                _maybe_fire(p, is_global=True)
        for p in e.passives.values():
            if p.when == "on_var_written":
                _maybe_fire(p, is_global=False)

        return log

    # ---- var-write-attempt firing ----
    # _fire_var_attempt is a separate channel from _fire_var_event for
    # "an attempt happened" vs "the data changed." Fires BEFORE the
    # mutation, so a passive subscribing to on_var_write_attempt can
    # observe the request even when it produces no diff (e.g. heal at
    # full HP that gets clamped back to the same value).
    #
    # Important design note: this method does NOT fire on_var_written
    # catch-all passives. on_var_written is reserved for actual data
    # events; conflating "attempted" and "happened" would muddy the
    # semantics. A passive that wants to observe both must subscribe to
    # both hooks.
    #
    # The recursion-depth guard from _fire_var_event is shared: an
    # attempt-hook formula that writes vars produces more attempts,
    # which produce more events, all under the same depth counter.
    def _fire_var_attempt(
        self,
        entity_id: str,
        path: str,
        old_value: Any,
        new_value: Any,
        intended_value: Any,
        was_clamped: bool,
    ) -> List[str]:
        """Fire on_var_write_attempt passives matching `path`. Returns log lines.

        Called from Entity.write_var AFTER clamp computation but BEFORE the
        actual mutation. Passives see the pre-write state (old_value reflects
        current data, since the write hasn't happened yet) and the proposed
        post-write state (new_value, possibly clamped). intended_value carries
        the caller's pre-clamp request, and was_clamped flags whether clamping
        modified anything.

        Shares the recursion-depth guard with _fire_var_event so that
        attempt-hook chains can't loop indefinitely.
        """
        # Recursion-depth guard (same limit as event firing)
        limit = int(self.rules.get("var_hook_recursion_limit", 128))
        if self._var_event_depth >= limit:
            if not self._var_event_warned:
                self._var_event_warned = True
                self._var_event_warnings.append(
                    f"⚠️ var-hook recursion limit ({limit}) reached on "
                    f"on_var_write_attempt for `{entity_id}.{path}`; "
                    f"suppressing further hook firing for this chain."
                )
            return []

        self._var_event_depth += 1
        try:
            return self._fire_var_attempt_inner(
                entity_id, path, old_value, new_value, intended_value, was_clamped
            )
        finally:
            self._var_event_depth -= 1
            if self._var_event_depth == 0:
                self._var_event_warned = False

    def _fire_var_attempt_inner(
        self,
        entity_id: str,
        path: str,
        old_value: Any,
        new_value: Any,
        intended_value: Any,
        was_clamped: bool,
    ) -> List[str]:
        """Inner firing logic for attempt hook (separated for depth guard)."""
        from formula import FormulaEngine, EvalCtx
        e = self.entities.get(entity_id)
        if e is None:
            return []

        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        extras = {
            "changed_key":    path,
            "old_value":      old_value if old_value is not _MISSING else None,
            "new_value":      new_value,
            "hook_name":      "on_var_write_attempt",
            "intended_value": intended_value,
            "was_clamped":    was_clamped,
        }
        ctx = EvalCtx(this=this_id, target=entity_id, extras=extras)

        log: List[str] = []

        def _maybe_fire(p: "Passive", is_global: bool) -> None:
            if not p.matches_event(path):
                return
            log.append(_run_passive_safely(
                engine, p, ctx, target_id=entity_id, is_global=is_global,
            ))

        # ONLY fire passives subscribed to on_var_write_attempt. We do NOT
        # fire on_var_written here — see comment on _fire_var_attempt.
        for p in self.global_passives.values():
            if p.when == "on_var_write_attempt":
                _maybe_fire(p, is_global=True)
        for p in e.passives.values():
            if p.when == "on_var_write_attempt":
                _maybe_fire(p, is_global=False)

        return log

    # ---- passive management (match-scoped / global) ----
    def add_global_passive(self, p: "Passive") -> None:
        """Attach a global Passive to this match. Validates formula parses."""
        if p.id in self.global_passives:
            raise DuplicateId(
                f"Global passive id '{p.id}' already exists in match '{self.id}'."
            )
        from formula import validate_formula
        validate_formula(p.formula, mode="exec")
        self.global_passives[p.id] = p

    def remove_global_passive(self, pid: str) -> None:
        if pid not in self.global_passives:
            raise NotFound(f"Global passive '{pid}' not found in match '{self.id}'.")
        del self.global_passives[pid]

    # ---- persistence ----
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "grid_width": self.grid_width,
            "grid_height": self.grid_height,
            "entities": {eid: e.to_dict() for eid, e in self.entities.items()},
            "turn_order": self.turn_order,
            "active_index": self.active_index,
            "system_name": self.system_name,
            "rules": self.rules,
            "turn_number": self.turn_number,
            "round_started": self.round_started,
            "global_passives": {pid: p.to_dict() for pid, p in self.global_passives.items()},
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Match":
        m = Match(
            id=d["id"],
            name=d["name"],
            grid_width=d["grid_width"],
            grid_height=d["grid_height"],
            system_name=d.get("system_name", "default"),
            rules=d.get("rules", {}),
        )
        for eid, ed in d.get("entities", {}).items():
            e = Entity.from_dict(ed)
            e.bind(m, set_spawn_facing=False)
            m.entities[eid] = e
        m.turn_order = d.get("turn_order", [])
        m.active_index = d.get("active_index", 0)
        # m.rules already set above
        m.turn_number = int(d.get("turn_number", 1))
        m.round_started = bool(d.get("round_started", False))
        m.global_passives = {
            pid: Passive.from_dict(pd)
            for pid, pd in (d.get("global_passives", {}) or {}).items()
        }
        return m

    # ------------- simple ASCII render for quick debugging -------------
    def render_ascii(self) -> str:
        # Build grid with an unused 0th row/col so coordinates can be 1-based
        grid = [
            ["." for _ in range(self.grid_width + 1)]
            for _ in range(self.grid_height + 1)
        ]
    
        arrows = {"up": "^", "down": "v", "left": "<", "right": ">"}
    
        for e in self.entities.values():
            # Keep old semantics: skip dead entities
            if not getattr(e, "is_alive", True):
                continue
            if self.in_bounds(e.x, e.y):
                sym = arrows.get(getattr(e, "facing", ""), "@")
                grid[e.y][e.x] = sym
    
        # Skip the 0th row entirely to preserve 1-based coordinates
        lines = [" ".join(row[1:]) for row in grid[1:]]
        return "\n".join(lines)

    def _spawn_facing(self, x: int, y: int) -> Direction:
        if self.rules.get("spawn_face_toward_center", True):
            return _default_facing_for(x, y, self.grid_width, self.grid_height)
        d = self.rules.get("spawn_default_facing", "up")
        return d if d in ALLOWED_DIRECTIONS else "up"

    def entities_in_turn_order(self) -> List["Entity"]:
        # Returns Entity objects in current turn order; appends any missing at the end
        ordered = []
        seen = set()
        for eid in getattr(self, "turn_order", []):
            if eid in self.entities:
                ordered.append(self.entities[eid])
                seen.add(eid)
        for eid, e in self.entities.items():
            if eid not in seen:
                ordered.append(e)
        return ordered

# -------------------------
# Match Manager (multi-match, now stores GameSystems and defaults))
# -------------------------
class MatchManager:
    def __init__(self):
        self.matches: Dict[str, Match] = {}
        # optional: track per-channel active match
        self.active_by_channel: Dict[str, str] = {}
        # GameSystems
        self.systems: Dict[str, GameSystem] = {
            "default": GameSystem("default", settings={})
        }
        self.default_system_name: str = "default"
        self.default_system_per_server: Dict[str, str] = {}
        self.default_system_per_channel: Dict[str, str] = {}

    # ----- game systems -----
    def list_systems(self) -> List[str]:
        return sorted(self.systems.keys())

    def get_system(self, name: str) -> GameSystem:
        if name not in self.systems:
            raise NotFound(f"GameSystem '{name}' not found")
        return self.systems[name]

    def create_system(self, name: str, settings: Optional[Dict[str, Any]] = None):
        if name in self.systems:
            raise DuplicateId(f"GameSystem '{name}' already exists")
        self.systems[name] = GameSystem(name, settings or {})

    def set_global_default_system(self, name: str):
        self.get_system(name)
        self.default_system_name = name

    def set_server_default_system(self, server_id: str, name: str):
        self.get_system(name)
        self.default_system_per_server[server_id] = name

    def set_channel_default_system(self, channel_key: str, name: str):
        self.get_system(name)
        self.default_system_per_channel[channel_key] = name

    def effective_system_name(self, channel_key: str) -> str:
        # channel_key is typically "<server_id>:<channel_id>"
        server_id = channel_key.split(":", 1)[0] if ":" in channel_key else channel_key
        return self.default_system_per_channel.get(
            channel_key,
            self.default_system_per_server.get(server_id, self.default_system_name)
        )

    def effective_system(self, channel_key: str) -> GameSystem:
        return self.get_system(self.effective_system_name(channel_key))

    # ----- matches -----
    def _build_rules_dict(self, sysobj) -> Dict[str, Any]:
        """Denormalized rule snapshot: engine defaults overlaid with system overrides."""
        rules = dict(DEFAULT_SYSTEM_SETTINGS)
        for k, r in (getattr(sysobj, "settings", {}) or {}).items():
            rules[k] = r.value
        return rules

    def create_match(self, match_id: str, name: str, width: int, height: int, channel_key: Optional[str] = None, system_name: Optional[str] = None) -> str:
        if match_id in self.matches:
            raise DuplicateId(f"Match id '{match_id}' already exists")
        sysobj = self.get_system(system_name) if system_name else (
            self.effective_system(channel_key or "CLI")
        )
        rules = self._build_rules_dict(sysobj)
        m = Match(id=match_id, name=name, grid_width=width, grid_height=height,
                  system_name=sysobj.name, rules=rules)
        self.matches[m.id] = m
        return m.id

    def refresh_match_rules(self, system_name: str) -> int:
        """Re-snapshot rules onto every match bound to `system_name`. Returns count refreshed.
        Call this after mutating a GameSystem so live matches pick up the change.
        """
        if system_name not in self.systems:
            return 0
        sysobj = self.systems[system_name]
        count = 0
        for m in self.matches.values():
            if m.system_name == system_name:
                m.rules = self._build_rules_dict(sysobj)
                count += 1
        return count

    def delete_match(self, match_id: str):
        if match_id not in self.matches:
            raise NotFound("Match not found")
        del self.matches[match_id]
        # clean up any actives
        to_remove = [ch for ch, mid in self.active_by_channel.items() if mid == match_id]
        for ch in to_remove:
            del self.active_by_channel[ch]

    def get(self, match_id: str) -> Match:
        m = self.matches.get(match_id)
        if not m:
            raise NotFound("Match not found")
        return m

    def list(self) -> List[Tuple[str, str]]:
        return [(mid, m.name) for mid, m in self.matches.items()]

    def set_active_for_channel(self, channel_key: str, match_id: str):
        if match_id not in self.matches:
            raise NotFound("Match not found")
        self.active_by_channel[channel_key] = match_id

    def get_active_for_channel(self, channel_key: str) -> Optional[str]:
        return self.active_by_channel.get(channel_key)

    # ---------- persistence ----------
    def save(self, path: str):
        data = {
            "matches": {mid: m.to_dict() for mid, m in self.matches.items()},
            "active_by_channel": self.active_by_channel,
            "systems": {name: s.to_dict() for name, s in self.systems.items()},
            "default_system_name": self.default_system_name,
            "default_system_per_server": self.default_system_per_server,
            "default_system_per_channel": self.default_system_per_channel,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except (OSError, TypeError) as e:
            # OSError: permission / dir missing; TypeError: unserializable data (shouldn't happen)
            raise VTTError(f"Failed to save to '{path}': {e}")
    
    def load(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            raise VTTError(f"File not found: '{path}'")
        except (OSError, json.JSONDecodeError) as e:
            raise VTTError(f"Failed to load from '{path}': {e}")
        try:
            self.matches = {mid: Match.from_dict(md) for mid, md in data.get("matches", {}).items()}
            self.active_by_channel = data.get("active_by_channel", {})
            # systems
            sysdict = data.get("systems", {"default": {"name": "default", "settings": dict(DEFAULT_SYSTEM_SETTINGS)}})
            self.systems = {name: GameSystem.from_dict(sd) for name, sd in sysdict.items()}
            self.default_system_name = data.get("default_system_name", "default")
            self.default_system_per_server = data.get("default_system_per_server", {})
            self.default_system_per_channel = data.get("default_system_per_channel", {})
            # re-bind matches to ensure entity defaults consistent
            for m in self.matches.values():
                # If system missing, fall back to global default
                if m.system_name not in self.systems:
                    m.system_name = self.default_system_name
                # refresh rules = defaults + system settings
                base = dict(DEFAULT_SYSTEM_SETTINGS)
                base = dict(DEFAULT_SYSTEM_SETTINGS)
                for k, r in (self.systems[m.system_name].settings or {}).items():
                    base[k] = r.value
                m.rules = base
                for e in m.entities.values():
                    e.bind(m, set_spawn_facing=False)
        except Exception as e:
            # Defensive: any schema mismatch should be surfaced as a friendly VTTError
            raise VTTError(f"Invalid save file format in '{path}': {e}")