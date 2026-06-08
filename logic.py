## logic.py (Core, testable)

# logic.py
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Literal, Any, Dict, List, Optional, Tuple, Set
import uuid
import json
import copy
import re

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

# Imported AFTER the exception classes so match_history.py can subclass
# VTTError for HistoryError. logic and match_history have a circular
# dependency, broken by importing match_history late here and importing
# VTTError eagerly from match_history's side.
from match_history import MatchHistory

# -------------------------
# Data Models
# -------------------------

# -------------------------
# Types & helpers
# -------------------------
# Directions cover the full 8-way set. Whether diagonals are actually
# usable for movement or facing is gated by per-system rules
# (allow_diagonal_movement, allow_diagonal_facing), checked at the
# command/move layer. The constant sets below are engine-wide and
# don't change with system settings.
Direction = Literal[
    "up", "down", "left", "right",
    "up_left", "up_right", "down_left", "down_right",
]
CARDINAL_DIRECTIONS: Set[str] = {"up", "down", "left", "right"}
DIAGONAL_DIRECTIONS: Set[str] = {"up_left", "up_right", "down_left", "down_right"}
ALLOWED_DIRECTIONS: Set[str] = CARDINAL_DIRECTIONS | DIAGONAL_DIRECTIONS

# Per-step deltas. y grows downward (1-based grid; (1,1) is the top-left).
DIRECTION_VECTORS: Dict[str, Tuple[int, int]] = {
    "up":         ( 0, -1),
    "down":       ( 0,  1),
    "left":       (-1,  0),
    "right":      ( 1,  0),
    "up_left":    (-1, -1),
    "up_right":   ( 1, -1),
    "down_left":  (-1,  1),
    "down_right": ( 1,  1),
}

# Reverse of DIRECTION_VECTORS: a unit (dx, dy) step maps back to its
# canonical direction name. Used by point-directed movement (pull_entity)
# that recomputes a heading each step rather than walking a fixed
# direction like push_entity does.
_VECTOR_TO_DIRECTION: Dict[Tuple[int, int], str] = {
    vec: name for name, vec in DIRECTION_VECTORS.items()
}

# Clockwise rotation orders. The 4-way variant is used when
# allow_diagonal_facing is off; the 8-way when it's on. Both start at "up".
DIRECTION_CW_ORDER_4: Tuple[str, ...] = ("up", "right", "down", "left")
DIRECTION_CW_ORDER_8: Tuple[str, ...] = (
    "up", "up_right", "right", "down_right",
    "down", "down_left", "left", "up_left",
)

# Single-cell ASCII glyphs for ASCII map rendering. Diagonals use the
# slash whose line matches the diagonal axis: '\' for the NW-SE pair
# (up_left, down_right) and '/' for the NE-SW pair (up_right, down_left).
# That means the two glyphs in each pair are AMBIGUOUS — you can't tell
# from the map alone whether a '\' is facing NW or SE. The exact facing
# is always visible via !ent dump / !ent info. We use ASCII here rather
# than the prettier unicode arrows (↖↗↘↙) because those four glyphs
# aren't in CP437 and don't render in the default Windows terminal font.
# The cardinal pair (^ v < >) is unambiguous and matches the historical
# pre-diagonal symbol set.
DIRECTION_ARROWS: Dict[str, str] = {
    "up":         "^",
    "down":       "v",
    "left":       "<",
    "right":      ">",
    "up_left":    "\\",
    "up_right":   "/",
    "down_left":  "/",
    "down_right": "\\",
}

# Maps every accepted alias to the canonical direction name. The parser
# also strips hyphens and lowercases before lookup, so "Up-Left" matches
# "up_left" without needing a separate entry.
_DIRECTION_ALIASES: Dict[str, str] = {
    # Cardinals
    "up": "up", "u": "up", "north": "up", "n": "up",
    "down": "down", "d": "down", "south": "down", "s": "down",
    "left": "left", "l": "left", "west": "left", "w": "left",
    "right": "right", "r": "right", "east": "right", "e": "right",
    # Diagonals — canonical underscore form, separator-free, two-letter,
    # and the four compass abbreviations.
    "up_left": "up_left", "upleft": "up_left", "ul": "up_left",
    "northwest": "up_left", "north_west": "up_left", "nw": "up_left",
    "up_right": "up_right", "upright": "up_right", "ur": "up_right",
    "northeast": "up_right", "north_east": "up_right", "ne": "up_right",
    "down_left": "down_left", "downleft": "down_left", "dl": "down_left",
    "southwest": "down_left", "south_west": "down_left", "sw": "down_left",
    "down_right": "down_right", "downright": "down_right", "dr": "down_right",
    "southeast": "down_right", "south_east": "down_right", "se": "down_right",
}


def normalize_direction(token: str) -> Optional[str]:
    """Map any user-provided direction alias to its canonical name.

    Accepts cardinals (up/down/left/right + u/d/l/r + n/s/e/w + compass
    names), diagonals (up_left/up-left/upleft/ul/nw and so on), and is
    case-insensitive. Returns None for unrecognized input — callers
    typically raise VTTError with their own message in that case.

    Does NOT check whether the canonical direction is permitted by the
    current game system; that's the caller's job (see Entity.move_dirs
    and the !ent face command for the allow_diagonal_* rule gating).
    """
    if not isinstance(token, str):
        return None
    t = token.strip().lower().replace("-", "_")
    return _DIRECTION_ALIASES.get(t)


def rotate_direction(facing: str, *, clockwise: bool, eight_way: bool) -> str:
    """Return the next direction one rotation step from `facing`.

    eight_way=True  rotates through all 8 directions (cardinals + diagonals).
    eight_way=False rotates only through the 4 cardinals.

    If `facing` is a diagonal but eight_way is False (e.g. the system was
    flipped after an entity was already facing diagonally), we snap to
    the nearest cardinal in the rotation direction in a single step,
    rather than spinning through diagonals the system has just disallowed.
    Unknown facings degrade to "up".
    """
    order = DIRECTION_CW_ORDER_8 if eight_way else DIRECTION_CW_ORDER_4
    if facing not in order:
        # Off-axis: snap to the nearest in `order` along the rotation
        # direction. We walk the full 8-way ring one step at a time and
        # return the first entry that lives in our reduced order.
        full = DIRECTION_CW_ORDER_8
        if facing not in full:
            return order[0]
        idx_full = full.index(facing)
        step = 1 if clockwise else -1
        for off in range(1, len(full) + 1):
            cand = full[(idx_full + off * step) % len(full)]
            if cand in order:
                return cand
        return order[0]
    idx = order.index(facing)
    step = 1 if clockwise else -1
    return order[(idx + step) % len(order)]


# ---- Event log (combat log) --------------------------------------------------
# The engine appends structured event records to Match.event_log at its
# chokepoints. Each record is a dict {type, round, turn, ...fields}; the
# text shown by !log is produced at READ time from a per-type template,
# so the stored data stays un-opinionated. EVENT_LOG_AUTO_TYPES are the
# engine-emitted types (custom is reserved for the log() primitive). Each
# is gated by the event_log_enabled master switch AND membership in the
# event_log_events rule. The default templates below are overridable per
# type via the event_log_format rule (a dict, edited with `!log format`).
EVENT_LOG_AUTO_TYPES: Tuple[str, ...] = (
    "death", "revive", "spawn", "move",
    "damage", "heal", "status_added", "status_removed",
    "action_used", "action_failed",
)

# Built-in render templates, keyed by event type. Same placeholder syntax
# as entity_line_format ({key}, {?key?}...{/?}, dotted paths, \n). The
# fields available per type are whatever Match.log_event stores for it
# (see each call site). A type with no override and no entry here renders
# as a compact key=value dump (see the !log command).
EVENT_LOG_DEFAULT_FORMATS: Dict[str, str] = {
    "death":          "💀 {name} (`{entity}`) died at ({x},{y}).",
    "revive":         "✨ {name} (`{entity}`) was revived.",
    "spawn":          "➕ {name} (`{entity}`) spawned at ({x},{y}).",
    "move":           "👣 {name} (`{entity}`) moved ({from_x},{from_y})→({to_x},{to_y}).",
    "damage":         "🩸 {name} (`{entity}`) took {amount} damage ({var} {old}→{new}).",
    "heal":           "💚 {name} (`{entity}`) healed {amount} ({var} {old}→{new}).",
    "status_added":   "🔆 {name} (`{entity}`) gained status `{status}`.",
    "status_removed": "🔅 {name} (`{entity}`) lost status `{status}`.",
    "action_used":    "⚔️ {actor_name} (`{actor}`) used `{action}`{?target?} → {target}{/?}.",
    "action_failed":  "✖️ {actor_name} (`{actor}`) failed `{action}`: {message}",
    "custom":         "{message}",
}

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
    "team_var": {
        "default": "team",
        "schema": {"type": "str"},
        "desc": (
            "Variable name in entity vars used for team / faction grouping "
            "(e.g. 'team', 'faction', 'side'). Stored as a string in entity "
            "vars; absent or empty means 'no team'. Formula access works via "
            "the configured name — for team_var='faction', use "
            "entity[X].faction in formulas. Unlike hp_var the team value is "
            "optional: entities don't have to belong to a team, and removing "
            "the var (e.g. !ent del_var hero team) puts the entity back to "
            "'no team' rather than erroring."
        ),
    },
    # --- Turn-order shape ---
    # The five rules below control how _rebuild_turn_order produces the
    # turn list from the candidate set (alive entities whose turnorder_var
    # is not None). Together they cover the common axes of variation:
    # which direction to sort, whether to cluster by team, where teamless
    # entities go, how to break exact ties, and when value-driven
    # rebuilds take effect.
    "turnorder_direction": {
        "default": "highest_first",
        "schema": {"type": "enum", "choices": ["highest_first", "lowest_first"]},
        "desc": (
            "Whether higher or lower turnorder_var values act FIRST in the "
            "round. 'highest_first' (the default) is what most d20-style "
            "systems use; 'lowest_first' fits e.g. roll-under or "
            "agility-based systems where the variable is conceptually "
            "'time to act' rather than initiative."
        ),
    },
    "turnorder_team_grouping": {
        "default": "none",
        "schema": {
            "type": "enum",
            "choices": ["none", "highest_per_team", "average_per_team"],
        },
        "desc": (
            "How teams are clustered within the turn order. 'none' (default) "
            "ignores teams entirely — every entity sorts on its own "
            "turnorder_var. 'highest_per_team' clusters team members "
            "together and ranks each team's slot by the best individual "
            "turnorder_var in that team. 'average_per_team' uses the team's "
            "mean turnorder_var as its slot. Within a clustered team, "
            "members sort by individual turnorder_var (using "
            "turnorder_direction and turnorder_tiebreaker the same way "
            "the top-level sort does). Teamless entities are placed "
            "according to turnorder_teamless_position."
        ),
    },
    "turnorder_teamless_position": {
        "default": "interleaved",
        "schema": {
            "type": "enum",
            "choices": ["interleaved", "first", "last"],
        },
        "desc": (
            "Where teamless entities sit when turnorder_team_grouping is "
            "enabled. 'interleaved' (default): each teamless entity is "
            "treated as a 'team of one' and slotted by its own "
            "turnorder_var alongside real teams. 'first': all teamless "
            "entities form a single block placed before all teamed "
            "entities. 'last': all teamless go after all teamed. The "
            "teamless block is always internally sorted by individual "
            "turnorder_var. Ignored entirely when team_grouping='none'."
        ),
    },
    "turnorder_tiebreaker": {
        "default": "name",
        "schema": {
            "type": "enum",
            "choices": ["name", "id", "insertion_order", "random_stable"],
        },
        "desc": (
            "How to resolve ties when two entities have the same "
            "turnorder_var (or two teams have the same group score). "
            "'name' (default): alphabetical by display name, then id. "
            "'id': by entity id only. 'insertion_order': order entities "
            "were added to the match (oldest first). 'random_stable': "
            "seeded by (match.id, round_number) so the order is stable "
            "within a round but reshuffles between rounds — useful for "
            "systems that want randomized initiative ties without "
            "rerolling each turn."
        ),
    },
    "turnorder_change_policy": {
        "default": "immediate",
        "schema": {"type": "enum", "choices": ["immediate", "deferred"]},
        "desc": (
            "When a write to an entity's turnorder_var or team_var "
            "occurs mid-round (typically from a formula or a passive's "
            "side effect), should turn order rebuild immediately? "
            "'immediate' (default): rebuild on the spot. The currently "
            "acting entity keeps acting — active_index follows them to "
            "their new position. 'deferred': leave this round's order "
            "untouched; emit a warning and stash the change. The "
            "rebuild happens at round end (after on_round_end fires, "
            "before on_round_start fires for the next round). "
            "Structural changes (spawn, death, removal) always rebuild "
            "immediately regardless of this rule — only value-driven "
            "changes are deferrable."
        ),
    },
    # Status auto-tick. Each status the engine knows about is just a
    # named dict on Entity.status — there is no hardcoded "remaining"
    # field or auto-decay. Instead the GM configures a tick:
    #   status_tick_when     selects when (or never) the tick fires
    #   status_tick_formula  is the GM-defined body that runs once per
    #                        (entity, status) at that time
    # The formula has the usual bindings (entity[self] = bearing
    # entity, this = current_entity_id()) PLUS `status_name` bound to
    # the status being ticked. Read/write status data via the status_*
    # formula functions (status_get / status_set / status_has /
    # status_remove / ...). A typical "tick a remaining counter and
    # auto-clear at 0" formula:
    #   if status_has_path(self, status_name, "remaining"):
    #       status_set(self, status_name, "remaining",
    #                  status_get(self, status_name, "remaining") - 1)
    #       if status_get(self, status_name, "remaining") <= 0:
    #           status_remove(self, status_name)
    "status_tick_when": {
        "default": "never",
        "schema": {
            "type": "enum",
            "choices": ["never", "turn_start", "turn_end",
                        "round_start", "round_end"],
        },
        "desc": (
            "When the status_tick_formula fires. 'never' (default) "
            "disables ticking entirely. 'turn_start' / 'turn_end' fire "
            "once per status on the entity whose turn is starting / "
            "ending; 'round_start' / 'round_end' fire once per status "
            "across every entity in turn order. Empty / unset status "
            "dicts on an entity simply contribute no ticks. Tile-hook "
            "ordering applies: round-end ticks fire AFTER the "
            "on_round_end hook, round-start ticks BEFORE on_round_start."
        ),
    },
    "status_tick_formula": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Formula body run once per (entity, status) at the time "
            "specified by status_tick_when. Empty (default) is a no-op "
            "even if status_tick_when is set. Context bindings: "
            "entity[self] = the bearing entity, this = "
            "current_entity_id(), status_name = the status being "
            "ticked (as a string). Use the status_* formula functions "
            "to read/write the status's data. A status_remove call "
            "during the tick safely removes the current status without "
            "breaking the iteration (the engine snapshots the per-"
            "entity status name list before running). Errors in the "
            "formula are logged with a ⚠️ marker but don't crash the "
            "tick — sibling statuses keep ticking."
        ),
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
        "desc": (
            "Default facing used when spawn_face_toward_center is False. "
            "Diagonal values (up_left/up_right/down_left/down_right) only "
            "take effect when allow_diagonal_facing is also True; otherwise "
            "they fall back to 'up' at spawn time."
        ),
    },
    # Diagonal direction support
    "allow_diagonal_movement": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "If True, !ent move accepts diagonal directions (up_left, "
            "up_right, down_left, down_right and their aliases like ul/ne). "
            "Each diagonal step moves one cell on both axes simultaneously. "
            "Off by default — diagonal-aware systems must opt in."
        ),
    },
    "allow_diagonal_facing": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "If True, entities may face one of the four diagonals; "
            "!ent face accepts diagonal tokens; !ent face <cw|ccw> rotates "
            "through 8 directions; and a diagonal move (if allowed) updates "
            "facing to the matching diagonal. When False, all of those "
            "collapse to the nearest cardinal. Independent of "
            "allow_diagonal_movement — you can have diagonal facing without "
            "diagonal movement, or vice versa."
        ),
    },
    # ---- UI / display templates ----
    # These rules drive !list and !ent info rendering — see
    # vtt_commands._entity_line / _entity_card. Template syntax:
    # {key} substitutes a value (missing -> "<?key?>" sentinel),
    # {?key?}...{/?} conditionally renders the inner only when the key
    # is present and truthy. Dotted paths supported. Scenarios 4-6
    # cover the templating behavior.
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
    "formula_function_recursion_limit": {
        "default": 64,
        "schema": {"type": "int"},
        "desc": (
            "Maximum call depth for user-defined formula functions (see "
            "!func). A function may call other functions (or itself); if "
            "the nested-call depth exceeds this limit the call raises a "
            "FormulaError instead of blowing Python's stack. Default 64 is "
            "ample for legitimate composition/recursion; hitting it almost "
            "always means an unbounded recursive function."
        ),
    },
    "formula_loop_limit": {
        "default": 10000,
        "schema": {"type": "int"},
        "desc": (
            "Maximum total iterations across all for-loops in a single "
            "formula evaluation. Loops over entities_within / "
            "cells_in_burst / etc. are bounded so a runaway "
            "loop-within-a-loop can't hang the bot. Default 10000 is "
            "ample for any realistic spell area; hitting it likely means "
            "an O(n²) pattern on a large board. Each iteration of any "
            "for-loop counts toward the total."
        ),
    },
    "summon_event_limit": {
        "default": 50,
        "schema": {"type": "int"},
        "desc": (
            "Maximum number of entities that may be summoned during a "
            "single top-level command (including all hook fires and "
            "action bodies it triggers). Guards against a runaway "
            "summon loop — e.g. an on_entity_spawned passive that "
            "summons another entity whose spawn hook summons again. "
            "The counter resets at the start of every command; "
            "exceeding the cap raises a clear error rather than "
            "hanging. Default 50 is far above any legitimate "
            "single-command burst (a mass-summon ritual); raise it "
            "if a system genuinely needs bigger waves."
        ),
    },
    # ---- Death system ---------------------------------------------------
    # An entity "dies" when a configurable formula condition evaluates
    # truthy on it. The check fires after every var-event chain (the
    # condition can reference any var, not just hp), at the chokepoint
    # inside Entity.write_var. On death the entity is removed from the
    # match per `death_result` — either deleted entirely or turned into
    # a corpse stored in the cell's tile data (the default — corpse
    # carries the full Entity.to_dict so `revive` can resurrect it).
    # Per-entity vars (`__death_condition` / `__death_result`) override
    # the system defaults; see Match._evaluate_death_condition.
    "death_condition": {
        "default": "entity[self].hp <= 0",
        "schema": {"type": "str"},
        "desc": (
            "Formula expression evaluated on an entity after any var "
            "change. When it returns truthy the entity dies. Default "
            "is the classic `hp <= 0` check, but any boolean formula "
            "works — `entity[self].hp <= 0 or status_has(self, "
            "'doomed')` for status-based death, or "
            "`entity[self].sanity <= 0` for a non-hp game. The formula "
            "runs with `self` bound to the entity being checked. "
            "Per-entity override: set `__death_condition` in the "
            "entity's vars; combined with this rule per "
            "`death_condition_mode`."
        ),
    },
    "death_condition_mode": {
        "default": "additive",
        "schema": {"type": "enum", "choices": ["additive", "replace"]},
        "desc": (
            "How a per-entity `__death_condition` var combines with "
            "this system's `death_condition` rule. `additive` "
            "(default): the entity dies when EITHER condition is true "
            "— useful for 'undead also die if they take radiant damage' "
            "without losing the hp check. `replace`: the per-entity "
            "condition WHOLLY replaces the rule — for entities the "
            "engine should track by entirely different state (a robot "
            "that dies on `core_damaged == true` regardless of hp)."
        ),
    },
    "death_result": {
        "default": "corpse",
        "schema": {"type": "enum", "choices": ["delete", "corpse"]},
        "desc": (
            "What happens when an entity dies. `corpse` (default) "
            "stores the Entity.to_dict snapshot in the entity's cell "
            "tile data under a corpses-by-id map "
            "(`tile[(x,y)].corpses.<eid>`) and removes the entity from "
            "turn order — actions can later `revive(<eid>)` it. "
            "`delete` removes the entity outright (no corpse left). "
            "Per-entity override: set `__death_result` in the "
            "entity's vars."
        ),
    },
    "show_corpses_in_entity_list": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "When true (default), `!list` and `!state` show a `Dead:` "
            "section below the live entity roster, displaying every "
            "corpse's id, name, position, and final hp/max_hp. False "
            "hides corpses from the entity-list output (they still "
            "exist in tile data; `!find` / `entity_actions` etc. on "
            "corpses use the explicit corpse-targeting surface)."
        ),
    },
    "corpse_id_uniqueness": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "When true (default), entity-id allocation considers CORPSE "
            "ids as well as live entity ids: `summon` / `mint_entity_id` "
            "won't reuse a corpse's id, and `!ent add` / `Entity.spawn` "
            "refuses an explicit id that matches a corpse. Prevents the "
            "'goblin1 dies → new goblin1 spawned → revive(goblin1) is "
            "forced to a different id' breakage. Turn off only if a "
            "system wants id reuse and the GM is OK with revive deciding "
            "which entity gets the id back (the current naming-collision "
            "semantics)."
        ),
    },
    "default_kill_function_effects": {
        "default": "entity[self].hp = 0",
        "schema": {"type": "str"},
        "desc": (
            "Formula program run on the entity BEFORE the death pipeline "
            "fires when the `kill()` primitive is invoked. The default "
            "zeroes the entity's hp so the resulting corpse reads "
            "naturally (otherwise a `kill('tough')` on a 99/99 entity "
            "would leave a 99/99 corpse). Empty string disables the "
            "effect — kill then just runs the death pipeline with the "
            "entity's current state. Runs with `self` bound to the "
            "killed entity; any formula constructs are legal "
            "(`entity[self].hp = 0`, `status_add(self, 'dead')`, "
            "`entity[self].hp = -entity[self].max_hp` for overkill "
            "tracking, etc.)."
        ),
    },
    "default_revive_function_effects": {
        "default": "entity[self].hp = entity[self].max_hp",
        "schema": {"type": "str"},
        "desc": (
            "Formula program run on the freshly-revived entity AFTER "
            "spawn (before on_revive fires). The default restores hp "
            "to max — the classic 'revive at full' rule — but any "
            "formula works. Empty string disables the effect, leaving "
            "the entity's stored hp as-is from the corpse snapshot "
            "(typically the negative value at death, so the entity "
            "would re-die immediately unless an on_revive passive "
            "heals it). Runs with `self` bound to the revived entity."
        ),
    },
    "corpse_line_format": {
        "default": "{name} (`{id}`): HP: {hp}/{max_hp} X,Y: {x},{y} (corpse)",
        "schema": {"type": "str"},
        "desc": (
            "Single-line template for a corpse row (used in the Dead: "
            "section of !list / !state). Same placeholder syntax as "
            "entity_line_format: {key}, dotted paths, {?key?}...{/?} "
            "conditionals, \\n for newlines. Built-in keys: id, name, "
            "x, y (the AUTHORITATIVE tile coords, NOT the embedded "
            "snapshot's x/y), facing, hp, max_hp, status_csv, "
            "passives_csv, died_round. Any vars on the snapshot are "
            "available by name; useful for system-specific death "
            "info like `{?death_cause?}slain by {death_cause}{/?}`."
        ),
    },
    # ---- Action system --------------------------------------------------
    # Actions are GM-defined effect bundles stored under entity.vars at any
    # path ending in `.actions.<name>`. Each action is a dict with `body`
    # (required, a formula program with the cmd()/source/fail extensions),
    # optional `description`, and optional `target` (one of: entity,
    # location, entity_list, location_list, none). Discovery walks the
    # whole vars tree, so `inventory.sword.actions.slice`,
    # `equipped.bow.actions.fire`, and `species.dragon.actions.breathe`
    # are all surfaced as available actions on the entity. The four
    # rules below shape that discovery and the body's command-dispatch
    # surface; see also Match.discover_actions and the !action command.
    "action_recursion_limit": {
        "default": 8,
        "schema": {"type": "int"},
        "desc": (
            "Maximum action call depth — an action body that calls "
            "use_action() into another action counts as a nested call. "
            "Default 8 is enough for legitimate composition (an attack "
            "calling a damage_calculation action calling a crit_check "
            "action ...); hitting it almost always means a cyclic "
            "definition. The dispatcher raises an ActionFail on "
            "overflow rather than blowing Python's stack."
        ),
    },
    "action_cmd_allowlist": {
        # Default deliberately conservative: only `ent` (the entity-state
        # CRUD surface) is allowed by default. Adding things like
        # `history`, `batch`, `run`, or `store` to this list explicitly
        # opts the GM into letting actions fork the timeline / persist
        # to disk / etc. — those side effects belong on the GM's head,
        # not the engine's defaults.
        "default": "ent",
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated list of command names that an action's "
            "body can dispatch via `cmd('<name> ...')`. The default "
            "`ent` covers all entity-state commands (hp/move/set_var/"
            "etc.). Add others (e.g. `tile,passive`) only when you "
            "want actions to manipulate those surfaces too. Commands "
            "like `history`, `batch`, `run`, `store` are NOT in the "
            "default — adding them is the GM's explicit choice. "
            "Whitespace around names is ignored; an empty list "
            "disables `cmd()` entirely (formula-only actions)."
        ),
    },
    "action_container_mode": {
        "default": "all",
        "schema": {"type": "enum", "choices": ["all", "whitelist", "blacklist"]},
        "desc": (
            "How the action-discovery walker chooses which top-level "
            "var containers to descend into. `all` (default) searches "
            "every top-level var. `whitelist` searches ONLY the "
            "containers named in action_container_paths. `blacklist` "
            "searches everything EXCEPT those containers. Useful when "
            "an entity has a `notes` or `__meta` container the GM "
            "wants the engine to ignore."
        ),
    },
    "action_container_paths": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated list of top-level var names. Interpreted "
            "as a whitelist or blacklist depending on "
            "action_container_mode. Ignored when mode is `all`. The "
            "names match the FIRST segment of a vars path only "
            "(`inventory`, `equipped`, `species`, ...); deeper paths "
            "inside an allowed container are always searched."
        ),
    },
    "action_names_case_sensitive": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "Whether action-name lookups treat `slice` and `Slice` as "
            "different actions. True (default) matches every other "
            "vars-key lookup in the engine. False folds both halves "
            "of a lookup to lowercase — letting a system tolerate "
            "casing drift in GM-typed names. Storage is always case-"
            "preserving; this rule affects matching only."
        ),
    },
    "random_seed": {
        "default": "",
        "schema": {"type": "str"},
        "desc": (
            "Seed for the match's formula RNG (random_int / random_string). "
            "Empty (default) means UNSEEDED — every roll is independent. "
            "Set a non-empty value to make rolls reproducible: the match "
            "uses a dedicated random.Random seeded with this string, so a "
            "given sequence of rolls replays identically. The RNG resets "
            "to the seed when the match (re)loads or when the seed value "
            "changes — the seed itself is saved with the match, but the "
            "live RNG cursor position is not, so reproducibility is from "
            "the seed-set / load point, not across arbitrary mid-session "
            "save/load. Mainly useful for testing encounters and for "
            "deterministic replays."
        ),
    },
    # ---- Match history / autosave / undo --------------------------------
    # The MatchHistory module (see match_history.py) takes three kinds of
    # autosave: round-start (after on_round_start hooks), turn-start
    # (after on_turn_start hooks), and pre-command (before each mutating
    # command). The three rules below cap how much of each is kept; the
    # three thresholds below them gate the "are you sure?" prompt for
    # destructive undos. Manual saves (!history save <name>) are exempt
    # from all retention pruning.
    #
    # For all six values, -1 means "unlimited / disable check" and 0
    # means "disable that snapshot kind / prompt entirely".
    "autosave_round_retention": {
        "default": -1,
        "schema": {"type": "int"},
        "desc": (
            "Cap on retained round-start autosaves. -1 = unlimited (default; "
            "the original spec is 'every start of round, for the entirety of "
            "the match'). 0 = disable round autosaves. N>0 = keep the most "
            "recent N round-start snapshots. Round-start snapshots are taken "
            "AFTER on_round_start hooks fire, so restoring one puts the "
            "match at the state players would have seen as the round began."
        ),
    },
    "autosave_turn_retention_rounds": {
        "default": 3,
        "schema": {"type": "int"},
        "desc": (
            "How many rounds' worth of turn-start autosaves to keep. -1 = "
            "unlimited (one per entity per round, forever — memory-heavy "
            "for long matches). 0 = disable turn autosaves entirely. "
            "Default 3 = three most recent rounds. Turn-start snapshots "
            "are taken AFTER on_turn_start hooks fire, so restoring one "
            "puts the match at the state players would have seen as that "
            "turn began."
        ),
    },
    "autosave_command_retention_turns": {
        "default": 3,
        "schema": {"type": "int"},
        "desc": (
            "How many turns' worth of pre-command autosaves to keep. -1 = "
            "unlimited. 0 = disable command autosaves entirely. Default 3 = "
            "all commands from the last three turns. Pre-command snapshots "
            "are taken before each MUTATING command runs and skipped when "
            "the command turns out to be a no-op (e.g. !ent info, !map). "
            "This gives users fine-grained undo at the cost of more "
            "snapshots than the round/turn levels."
        ),
    },
    "undo_confirmation_turn_threshold": {
        "default": 3,
        "schema": {"type": "int"},
        "desc": (
            "Undoing this many or more turns requires the user to repeat the "
            "command with a trailing `confirm` token. Smaller undos go "
            "through silently. Default 3. -1 disables the prompt entirely; "
            "0 means every turn-undo prompts (since 'n >= 0' is always true "
            "for a valid undo)."
        ),
    },
    "undo_confirmation_round_threshold": {
        "default": 1,
        "schema": {"type": "int"},
        "desc": (
            "Undoing this many or more rounds requires `confirm`. Default 1 "
            "(any round-undo prompts, since a round is a lot of state). -1 "
            "disables. The same `confirm` token applies to `!history undo "
            "to round X` regardless of distance — that command is "
            "conceptually a 'big jump' and always prompts."
        ),
    },
    "undo_confirmation_command_threshold": {
        "default": -1,
        "schema": {"type": "int"},
        "desc": (
            "Undoing this many or more commands requires `confirm`. Default "
            "-1 = never prompt (commands are small-grain, low-risk). Set a "
            "positive value to require confirmation past that count."
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
    # Default passives injected onto every entity at creation. Each entry
    # is a passive-spec dict (id/when/formula, optional target/scope —
    # the same shape Passive.to_dict produces). When an entity is created
    # (via !ent add, summon, OR revive — every path routes through
    # Entity.spawn), each default passive whose id the entity does not
    # already carry is copied onto it BEFORE on_entity_spawned fires, so
    # a default passive may itself observe the spawn. This is an
    # injection, NOT an enforcement: the GM (or a formula) can remove a
    # default passive from an individual entity afterward and it stays
    # gone — until that entity is re-spawned (e.g. revived), at which
    # point the defaults are re-applied. Edit via !defpassive add/remove/
    # list (NOT !system set, which can't handle structured values), the
    # same pattern default_clamps uses with !gclamp. Empty (default) =
    # no injection.
    "default_entity_passives": {
        "default": [],
        "schema": {"type": "list"},
        "desc": (
            "List of passive specs (each {id, when, formula, target?, "
            "scope?}) copied onto every entity at creation (add / summon "
            "/ revive — all route through Entity.spawn), applied before "
            "on_entity_spawned fires. A default whose id the entity "
            "already has is skipped (so revive is idempotent for kept "
            "passives). Injection only, never re-enforced — remove one "
            "from an entity and it stays gone until that entity is "
            "re-spawned. Edit via !defpassive add/remove/list, not "
            "!system set."
        ),
    },
    # ---- Event log (combat log) ----------------------------------------
    "event_log_enabled": {
        "default": True,
        "schema": {"type": "bool"},
        "desc": (
            "Master switch for the structured event log (the combat log). "
            "When True (default), the engine records curated combat events "
            "at its chokepoints and the log() formula primitive appends "
            "custom entries; view with `!log`. False disables ALL logging "
            "(auto events and log()) — existing entries are kept but no "
            "new ones are added. The log is a separate store: it never "
            "changes command replies, only what `!log` shows."
        ),
    },
    "event_log_events": {
        "default": ",".join(EVENT_LOG_AUTO_TYPES),
        "schema": {"type": "str"},
        "desc": (
            "Comma-separated list of which AUTO event types the engine "
            "records (subject to event_log_enabled). Default is all of "
            "them: " + ", ".join(EVENT_LOG_AUTO_TYPES) + ". Trim it to cut "
            "noise — e.g. drop `move` and `damage`/`heal` for a "
            "lifecycle-only log. The `custom` type (from the log() "
            "primitive) is always allowed when logging is enabled and is "
            "not gated by this list."
        ),
    },
    "event_log_retention": {
        "default": 200,
        "schema": {"type": "int"},
        "desc": (
            "Cap on the number of event-log entries kept. When the log "
            "exceeds this, the oldest entries are dropped. Default 200. "
            "-1 = unlimited (memory grows with the match); 0 = keep "
            "nothing (logging effectively off even if enabled)."
        ),
    },
    "event_log_format": {
        "default": {},
        "schema": {"type": "dict"},
        "desc": (
            "Per-type render-template OVERRIDES for the event log, keyed "
            "by event type. A type with no override uses the engine's "
            "built-in default template; a type with neither renders as a "
            "compact key=value dump. Same placeholder syntax as "
            "entity_line_format ({key}, {?key?}...{/?}, dotted paths, "
            "\\n). Each event's available fields are whatever the engine "
            "stored for it (see `!log` / the type's default template). "
            "Edit via `!log format <type> <template>` (NOT !system set — "
            "it's a structured value)."
        ),
    },
    "command_access": {
        "default": {},
        "schema": {"type": "dict"},
        "desc": (
            "Per-match OVERRIDES for the command access gate, keyed by "
            "command name or 'name sub' (e.g. \"find\" or \"ent dump\"). "
            "The value is one of: \"all\" (anyone may run it), \"host\" "
            "(hosts run directly, a non-host's invocation is held for "
            "approval), \"host_only\" (hosts only, non-hosts rejected — no "
            "queue), \"owner\" (owner only). The engine's defaults make "
            "every state-mutating command \"host\" and read-only inspect "
            "subcommands (list/info/dump/cells/diff/channels/hosts) "
            "\"all\"; use this rule to TIGHTEN reads that would leak "
            "hidden information in a fog-of-war / invisibility match "
            "(e.g. {\"ent dump\": \"host\", \"find\": \"host\"}) or to "
            "loosen a normally-gated command. A 'name sub' key beats a "
            "bare 'name' key. Edit via `!system set` is awkward for a "
            "dict; set it through the system's settings or a future "
            "dedicated command."
        ),
    },
    # "movement_block_through": {
    #     "default": False,
    #     "schema": {"type": "bool"},
    #     "desc": "If True, stepwise movement collides with units.",
    # },
    "friendlyfire": {
        "default": False,
        "schema": {"type": "bool"},
        "desc": (
            "Whether attacks can hit allies. False (default) means same-team "
            "entities are not legal targets; True means anyone can be "
            "targeted, friend or foe. This is consulted by the "
            "is_attackable(a, b) formula function — passives that gate on "
            "'can A attack B' should use is_attackable rather than "
            "is_hostile so they pick up changes to this rule automatically. "
            "is_hostile(a, b) remains a pure same/different-team check "
            "regardless of this rule, so formulas that want the strict "
            "ally-vs-enemy distinction (e.g. healing only hostiles) still "
            "behave the same."
        ),
    },
}

# Precomputed views of RULES_REGISTRY for fast lookup at call sites that
# only need the default value or the schema (not the full registry entry).
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
    # Unknown key? block (prevents typos or unimplemented settings).
    # When the rule schema gets large the bare "Allowed: ..." dump is
    # noise, so we put close-match suggestions FIRST (the common case
    # for unknown rules is a typo) and only show the full list if we
    # have no good guesses.
    if key not in RULE_SCHEMA:
        import difflib as _difflib
        keys = sorted(RULE_SCHEMA.keys())
        hits = _difflib.get_close_matches(key, keys, n=3, cutoff=0.55)
        if hits:
            suggestions = ", ".join(f"'{h}'" for h in hits)
            raise VTTError(
                f"Unknown setting '{key}'. Did you mean: {suggestions}? "
                f"Use `!system rules` to see all settings."
            )
        allowed = ", ".join(keys)
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

    if t == "dict":
        # Structured dict-valued rules (currently: event_log_format) aren't
        # editable via the flat key=value !system set command either.
        raise VTTError(
            f"Setting '{key}' is a structured dict and can't be edited via "
            f"!system set. Use the dedicated command for this rule "
            f"(e.g. !log format <type> <template> for event_log_format)."
        )

    raise VTTError(f"Invalid schema for setting '{key}'.")


def _dominant_axis_dir(dx: int, dy: int, *, eight_way: bool = False) -> Direction:
    """Pick a facing direction from a (dx, dy) vector pointing where the
    entity should look. Ties prefer vertical for the cardinal case
    (preserves the historical behavior of the 4-way version).

    In 8-way mode, when both components are non-zero we return a diagonal;
    a pure-axis vector still resolves to the matching cardinal.
    """
    if eight_way and dx != 0 and dy != 0:
        v = "up" if dy < 0 else "down"
        h = "left" if dx < 0 else "right"
        return f"{v}_{h}"
    if abs(dy) >= abs(dx):
        return "down" if dy > 0 else "up"
    return "right" if dx > 0 else "left"


def _default_facing_for(
    x: int, y: int, width: int, height: int, *, eight_way: bool = False,
) -> Direction:
    cx = (width + 1) / 2
    cy = (height + 1) / 2
    dx = cx - x
    dy = cy - y
    return _dominant_axis_dir(int(round(dx)), int(round(dy)), eight_way=eight_way)


# ---------- Special/reserved id registry (like how "this" or "current" is the entity whose turn it is right now, "self" is usually the entity directly affected ay a command regardless of turn order") ----------


RESERVED_IDS: Set[str] = {"current", "this", "self"}


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
    # Death event. Fires on the entity once the death-condition formula
    # evaluates truthy, BEFORE the entity is removed from the match (so
    # `self` is still bound and entity[self].x/.y still resolve). The
    # corpse-vs-delete result also runs after this fires. No special
    # extras — passives observe via the normal `self`/`this` bindings.
    "on_death",
    # Revive event. Fires on the freshly-restored entity AFTER revive()
    # has spawned it back from a corpse (so on_entity_spawned has
    # already fired). Useful for "remove the doomed status when I come
    # back" or "regen kicks in only after the first revive". `self` =
    # the revived entity; `this` = current-turn entity as usual.
    "on_revive",
    # Movement event (any position change — tp / step / clone-spawn-
    # via-move; NOT the initial spawn placement, which has its own
    # on_entity_spawned hook). Context bindings during fire:
    #   from_x, from_y, to_x, to_y  — the move's endpoints
    # Tile on_enter / on_exit fire alongside this for tile-attached
    # logic; on_entity_moved is the entity-side complement so a
    # passive can react regardless of which tile is involved.
    "on_entity_moved",
    # Action usage event. Fires AFTER an action body has run to
    # completion (and after its outer snapshot has been committed). A
    # passive watching this hook sees the final state, not an in-flight
    # one. Fires on the ACTOR entity (`self` = the entity that used the
    # action; `this` = current-turn entity, as usual). Context bindings:
    #   action_name   the bare name (e.g. "slice")
    #   action_path   the full vars path the action was loaded from
    #                 (e.g. "inventory.sword.actions.slice"). Useful
    #                 for category filtering (`if action_path.startswith
    #                 ("inventory.")`).
    #   target        the resolved target — an entity id (target=entity),
    #                 a (x, y) tuple (target=location), a list of either,
    #                 or None (target=none).
    #   args          the args dict the GM passed (key=value tokens).
    # Failed actions (fail() / rollback) do NOT fire this hook — only
    # successful completions do; the on_action_failed hook below
    # covers the rollback path.
    "on_action_used",
    # Target-side companion to on_action_used. Fires on EACH ENTITY
    # the action targeted, AFTER the actor-side on_action_used has
    # run. For target=entity, fires once on that target. For
    # target=entity_list, fires once per eid (insertion order).
    # For location / location_list / none, does NOT fire — there's
    # no entity-side target. Inside the hook, `self` binds to the
    # target (the entity being acted on); `actor` binds to the
    # entity that USED the action. Other bindings (action_name,
    # action_path, args) match on_action_used. This is where
    # reactive defender logic lives — "shield reflects melee",
    # "trap counters attacker", "this entity has resistance to
    # actions whose action_path starts with `inventory.poison.`".
    "on_action_used_on_target",
    # Failure event. Fires on the ACTOR after a clean fail() abort —
    # AFTER the runner has rolled back to pre-state, so passives see
    # the unchanged world (matching on_action_used's "see settled
    # state" contract). Bindings:
    #   action_name, action_path, target, args  — same as
    #   on_action_used
    #   fail_reason   the GM-supplied tag from fail(reason, msg),
    #                 or "" for the single-arg fail(msg) form
    #   fail_message  the human-readable failure message
    # Engine faults (recursion limit, internal invariants) DON'T
    # fire this hook — they're bugs, not GM-controlled outcomes,
    # and observing them with a passive would mask the diagnostic.
    "on_action_failed",
    # Per-step movement event. Fires ONCE PER CELL traversed during a
    # stepwise move (Entity.move_dirs), AFTER the position changes for
    # that step — so `entity[self].x/.y` refer to the just-entered
    # cell when the passive runs. Context bindings:
    #   from_x, from_y  — the cell just left (the previous step's
    #                     position; equals the entity's pre-move origin
    #                     for the first step)
    #   to_x, to_y      — the cell just entered (== entity[self].x/.y)
    # Single-tp moves (Entity.tp, including formula `move_entity`) fire
    # `on_entity_moved` only, not `on_entity_step` — there are no
    # intermediate cells to "step into". So on_entity_step is the right
    # hook for "thorns trigger per cell of traversal", "attacks of
    # opportunity fire per step adjacent to me", and "distance-cost
    # accumulators". For "did this entity move at all this command",
    # use on_entity_moved (fires once, after all steps).
    "on_entity_step",
    # Status lifecycle. Fire on the entity whose status changed.
    # Context bindings:
    #   status_name  — the affected status
    #   old_value    — for changed/removed: the prior data dict (the
    #                  whole subtree under status[name]); None for
    #                  added (the status didn't exist before).
    #   new_value    — for added/changed: the resulting data dict;
    #                  None for removed (the status no longer exists).
    # `on_status_changed` fires on data-field writes within an
    # already-present status (status_set on a path; the field-level
    # equivalent of on_var_changed). For applied/removed lifecycle,
    # use on_status_added / on_status_removed — they cover the
    # whole-status appear/disappear distinction.
    "on_status_added",
    "on_status_removed",
    "on_status_changed",
    "on_var_created",
    "on_var_changed",
    "on_var_removed",
    "on_var_written",
    "on_var_write_attempt",
}

# Tile hooks live in tiles[(x, y)]["hooks"][<name>] as formula strings.
# Movement hooks (on_enter / on_exit / on_stop) fire when an entity
# transits a tile via Entity.tp / Entity.move_dirs (and
# Match.move_group_dirs for group-shift moves). Time hooks
# (on_round_*, on_turn_*) fire on the tile itself at the corresponding
# round/turn lifecycle moment — they don't need an entity standing on
# the tile, just the tile existing with the hook registered. These
# names are kept separate from HOOK_NAMES because entity / global
# passives don't subscribe to them (only stored tile-side formulas
# do). See Match.fire_tile_hook / Match.fire_tile_time_hooks.
TILE_HOOK_NAMES: Set[str] = {
    # Movement
    "on_enter",   # entity arrived on this tile (fired post-position-change)
    "on_exit",    # entity is about to leave this tile (fired PRE-position-change
                  # so entity[self].x / .y still refer to the exit tile)
    "on_stop",    # entity stopped here: either a tp landed here, or this
                  # was the last cell of a stepwise move. Fires after on_enter
                  # at the same coord.
    # Time hooks. Fire once per relevant tile (anything in match.tiles
    # carrying that hook) at the round/turn lifecycle moment. The
    # firing entity (for `self`) is whatever entity's turn is current
    # at the time the hook fires — except for round_* hooks where no
    # specific entity is acting; there `self` resolves to the
    # current-turn entity if there is one, else `self` is unbound.
    # Bindings: tile_x, tile_y for both movement and time hooks.
    "on_round_start",
    "on_round_end",
    "on_turn_start",
    "on_turn_end",
}

# Zone hooks live in zones[name]["hooks"][<name>] as formula strings. A
# zone is a NAMED set of cells (Match.zones) — unlike a tile (one cell),
# a zone covers many and can be reshaped/moved. Zones carry TWO movement
# hook families plus the time hooks:
#   Boundary hooks (on_enter/on_exit/on_stop) fire ONCE when an entity
#     CROSSES the zone's edge — entering from outside, leaving to outside,
#     or stopping inside. Moving cell-to-cell WITHIN the zone fires none
#     of these. This is the "you entered the room" semantics.
#   Per-cell hooks (on_cell_enter/on_cell_exit/on_cell_stop) mirror tile
#     movement hooks: they fire for EVERY cell of the zone stepped into /
#     out of / stopped on, even while moving within the zone. This is the
#     "the gas damages you each cell you walk through" semantics.
#   Time hooks (on_round_*, on_turn_*) fire once per zone carrying them
#     at the lifecycle moment, like tile time hooks.
# Bindings: zone_name, plus tile_x/tile_y (the crossed/stepped cell) for
# movement hooks. `self` = the moving/acting entity (or current-turn
# entity for time hooks; unbound if none). Kept separate from HOOK_NAMES
# and TILE_HOOK_NAMES — only zone-stored formulas subscribe. See
# Match.fire_zone_hook / fire_zone_move_hooks / fire_zone_time_hooks.
ZONE_HOOK_NAMES: Set[str] = {
    # Boundary (fire once on a zone-edge crossing)
    "on_enter",
    "on_exit",
    "on_stop",
    # Per-cell (fire per cell of the zone, like tile hooks)
    "on_cell_enter",
    "on_cell_exit",
    "on_cell_stop",
    # Time
    "on_round_start",
    "on_round_end",
    "on_turn_start",
    "on_turn_end",
}
# Reserved top-level keys inside a zone dict (everything else under
# zones[name]["data"] is free-form GM data).
ZONE_RESERVED_KEYS = frozenset({"cells", "data", "hooks", "glyph"})

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
        # `target=""` is the "no filter" sentinel and `scope="deep"` is the
        # most permissive option — these defaults are the semantic
        # "unspecified" state, not back-compat for missing fields.
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
# SpecialTileTemplate (reusable tile definition: data defaults + hooks)
# -------------------------
# A template defines a KIND of special tile (e.g. "flame", "spike",
# "treasure_chest") that the GM can instantiate at any number of
# coordinates. Templates have:
#   - data:  dict of default field values copied into the tile dict at
#            placement time. Each placed instance carries its own copy
#            so edits on one instance don't propagate to others.
#   - hooks: dict[when, formula_src] of formulas that fire when an
#            entity transits any instance of this template. Looked up
#            LIVE every fire — editing template hooks immediately
#            affects all placed instances. The two halves intentionally
#            differ: data is instance state (drifts), hooks are shared
#            behavior (canonical).
#
# Unique one-off tiles ("a treasure chest at (5,5) with custom loot")
# don't need a template — they're written as ad-hoc instances via
# !tile set + !tile hook add, which keep working unchanged. Templates
# exist purely as a reuse convenience for tile KINDS that recur.
TILE_RESERVED_KEYS = frozenset({"_template", "hooks"})
# Keys in a tile dict that aren't user data:
#   _template — name of the template this instance was placed from
#               (absent on ad-hoc instances)
#   hooks     — per-instance hook overrides (override the template's
#               hooks per-when; primarily the !tile hook surface)
# Template definitions must not carry these names in their data dict;
# !tile def data rejects writes to either key.


@dataclass
class SpecialTileTemplate:
    name: str
    data: Dict[str, Any] = field(default_factory=dict)
    hooks: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "data": copy.deepcopy(self.data),
            "hooks": dict(self.hooks),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SpecialTileTemplate":
        return SpecialTileTemplate(
            name=str(d["name"]),
            data=copy.deepcopy(d.get("data", {}) or {}),
            hooks={
                str(k): str(v)
                for k, v in (d.get("hooks", {}) or {}).items()
                if isinstance(v, str)
            },
        )


# -------------------------
# FormulaFunction (a reusable, user-defined formula callable)
# -------------------------
# A FormulaFunction is a named formula body plus a list of parameter
# names. Once defined on a match (via !func def), any formula can call
# it by name — `damage_after_armor(entity[self].atk, entity[target].def)`
# — and the body runs with the parameters bound as plain identifiers.
#
# The body is a full formula program: it may have statements (including
# entity[X] writes, which take effect as side effects) and an optional
# trailing expression whose value becomes the function's return value.
# A body with no trailing expression returns None.
#
# Inside the body:
#   - the parameter names are bound to the call's argument values
#   - entity[self] / entity[this] resolve against the CALLER's context
#     (functions are macros that run in the caller's frame, not a fresh
#     one) — so a function called from a passive sees that passive's
#     `self`
#   - other formula functions (and builtins) are callable, enabling
#     composition and recursion (bounded by the recursion limit rule)
#
# Compilation of the body is cached on the instance (the _compiled
# field, excluded from equality / repr / serialization). Redefining a
# function replaces the instance, so the cache is naturally fresh.
_FUNC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass
class FormulaFunction:
    name: str
    params: List[str] = field(default_factory=list)
    body: str = ""
    # Lazily-populated compiled artifact, set by the formula engine on
    # first call. Opaque to logic.py. Excluded from comparison/repr so
    # two functions with the same source are still equal, and from
    # to_dict so it never leaks into save files.
    _compiled: Any = field(default=None, compare=False, repr=False)

    def signature(self) -> str:
        """Human-readable `name(p1, p2)` for listings and errors."""
        return f"{self.name}({', '.join(self.params)})"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "params": list(self.params),
            "body": self.body,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "FormulaFunction":
        return FormulaFunction(
            name=str(d["name"]),
            params=[str(p) for p in (d.get("params", []) or [])],
            body=str(d.get("body", "")),
        )


# -------------------------
# GameSystem (stores global rules for a game system, so not everything has to be manually set each match)
# -------------------------
@dataclass
class GameSystem:
    name: str
    # Per-system overrides now stored as Rule objects keyed by rule key
    settings: Dict[str, Rule] = field(default_factory=dict)
    # System-level special-tile templates. Copied into Match.tile_templates
    # on match creation so a game system can ship a default library of
    # tile kinds (e.g. "fire", "wall", "pit") that matches inherit
    # automatically. Matches can extend or shadow these with their own
    # !tile def commands; the system-level definitions are unaffected.
    # Stored as plain dicts (template name -> SpecialTileTemplate.to_dict
    # output) rather than live SpecialTileTemplate objects so the
    # GameSystem stays self-contained and easy to serialize.
    tile_templates: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # System-level formula-function library, copied into
    # Match.formula_functions at match creation — same inherit-at-create
    # pattern as tile_templates. Lets a game system ship a standard set
    # of reusable formula functions (e.g. armor mitigation, crit math)
    # that every new match starts with. Stored as plain dicts
    # (FormulaFunction.to_dict output) for self-contained serialization.
    formula_functions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # System-level alias library, copied into Match.aliases at match
    # creation — same inherit-at-create pattern as tile_templates /
    # formula_functions. Lets a system ship a standard set of GM
    # shorthands (`dmg`, `heal`, `mv`, ...) that every match in this
    # system starts with. Stored as plain `name -> expansion` strings.
    aliases: Dict[str, str] = field(default_factory=dict)

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
            "tile_templates": {
                tname: copy.deepcopy(tdef)
                for tname, tdef in sorted(self.tile_templates.items())
            },
            "formula_functions": {
                fname: copy.deepcopy(fdef)
                for fname, fdef in sorted(self.formula_functions.items())
            },
            "aliases": {
                aname: str(self.aliases[aname])
                for aname in sorted(self.aliases.keys())
            },
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "GameSystem":
        raw = d.get("settings", {}) or {}
        settings: Dict[str, Rule] = {}
        for k, v in raw.items():
            settings[k] = Rule(
                key=k,
                value=v["value"],
                schema=v.get("schema") or RULES_REGISTRY.get(k, {}).get("schema", {}),
                description=v.get("description") or RULES_REGISTRY.get(k, {}).get("desc", ""),
            )
        raw_templates = d.get("tile_templates", {}) or {}
        templates: Dict[str, Dict[str, Any]] = {}
        for tname, tdef in raw_templates.items():
            if isinstance(tdef, dict) and isinstance(tname, str):
                templates[tname] = copy.deepcopy(tdef)
        raw_funcs = d.get("formula_functions", {}) or {}
        funcs: Dict[str, Dict[str, Any]] = {}
        for fname, fdef in raw_funcs.items():
            if isinstance(fdef, dict) and isinstance(fname, str):
                funcs[fname] = copy.deepcopy(fdef)
        raw_sys_aliases = d.get("aliases", {}) or {}
        sys_aliases: Dict[str, str] = {
            str(k): str(v) for k, v in raw_sys_aliases.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        return GameSystem(
            name=d["name"], settings=settings,
            tile_templates=templates, formula_functions=funcs,
            aliases=sys_aliases,
        )

# -------------------------
# Entity
# -------------------------

def _coerce_status_dict(raw: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize a serialized `status` field into the dict-of-dicts shape.

    The status field is `Dict[str, Dict[str, Any]]` — each flag name
    maps to its own data dict. Serializers emit that shape directly,
    but we accept a couple of equivalent inputs for robustness:
      - dict           -> keep as-is, ensuring each value is a dict
      - list of names  -> {name: {} for name in list}  (each flag
                          becomes a status with no data)
      - anything else  -> {}  (silently coerce to empty)
    """
    if isinstance(raw, dict):
        out: Dict[str, Dict[str, Any]] = {}
        for k, v in raw.items():
            if not isinstance(k, str):
                continue
            out[k] = dict(v) if isinstance(v, dict) else {}
        return out
    if isinstance(raw, (list, tuple, set)):
        return {str(n): {} for n in raw if isinstance(n, str)}
    return {}


@dataclass
class Entity:
    name: str
    x: int
    y: int
    id: str# explicit, user-provided
    # Team lives in vars under the key configured by the team_var rule
    # (default "team"), same as hp/max_hp/initiative. Read/write goes
    # through the team property pair below so external callers using
    # e.team still work — direct vars[team_var] access is also fine.
    # Status flags carry their own data dicts (the "what X does is stored
    # in X" design rule). A status like "stunned" might hold {"skips_turn":
    # True, "remaining": 3}; "poisoned" might hold {"damage": 5,
    # "remaining": 3}. The data shape is GM-defined — the engine only
    # knows TWO conventions:
    #   skips_turn (bool)  -> if True, the bearer is skipped when their
    #                          turn comes up (see Match._skip_to_eligible).
    #                          Absent / False means a normal turn.
    # Everything else is GM data, accessed via the status_* formula
    # functions (status_get / status_set / status_has / status_remove /
    # ...). Auto-decay of a "remaining" counter (or any field the GM
    # chooses) is GM-configurable via the status_tick_when /
    # status_tick_formula rules — there is NO hardcoded auto-decay.
    status: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # structured variable bag — hp, max_hp, and initiative now live HERE
    # under keys defined by the GameSystem (defaults: "hp", "max_hp", "initiative")
    vars: Dict[str, Any] = field(default_factory=dict)

    # Entity-scoped passives: formulas that fire on a given hook for
    # this entity specifically. See Passive (defined above) for the
    # event surface and target/scope filtering semantics.
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

    def _team_var_name(self) -> str:
        """Var name used to store this entity's team. Pulled from
        the bound match's team_var rule, or the default 'team' if
        the entity isn't bound yet (e.g. during deserialization)."""
        if self._match:
            return self._match.rules.get("team_var", "team")
        return "team"

    def protected_var_names(self) -> Set[str]:
        """Return the set of top-level var keys that must not be deleted.

        Note: team_var is intentionally NOT included. Team is optional —
        an entity can legitimately have no team, and deleting the team
        var is the way to express "remove this entity from its team".
        Compare hp/max_hp/initiative, which the engine assumes always
        exist (deleting hp would break combat math)."""
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

    @property
    def team(self) -> Optional[str]:
        """Team / faction name, sourced from vars[team_var]. None when
        the entity isn't on a team (var absent OR set to None). The
        getter doesn't coerce to str so that formula readers see the
        actual stored type — a GM who sets the team var to an int via
        !ent set_var still gets that int back through e.team, which
        surfaces the type mismatch instead of silently hiding it."""
        v = self.vars.get(self._team_var_name())
        return v if v is not None else None

    @team.setter
    def team(self, value):
        # Mirrors max_hp.setter / initiative.setter: None unsets the
        # var (firing on_var_removed if it existed), otherwise we
        # write through write_var (which fires created/changed events
        # as appropriate). Pre-bind entities — i.e. during from_dict
        # before bind() runs — get a plain dict assign because
        # write_var no-ops on event firing without a bound match.
        name = self._team_var_name()
        if value is None:
            if name in self.vars:
                self.remove_var(name)
        else:
            self.write_var(name, value)

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

    @property
    def is_cell_stackable(self) -> bool:
        """True when this entity's `__cell_stackable` var is truthy.
        A stackable entity (a) doesn't count as occupying its cell for
        Match.is_occupied — other entities can move onto a tile that
        only contains stackable residents — and (b) faces no occupancy
        check when it itself moves, so it can enter cells already
        holding non-stackable entities. Together that's the "very
        small entity" / overlapping-units pattern. Per-entity opt-in
        via vars; absent / false-y means the normal "one entity per
        cell" enforcement applies."""
        return bool(self.vars.get("__cell_stackable", False))

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
        # Stackable spawners skip the occupancy check (same rule as
        # Entity.tp / move_dirs / summon_entity). Lets `!ent add` drop
        # a stackable entity onto a cell that already holds another.
        if not self.is_cell_stackable and match.is_occupied(x, y):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        # ID collision check honors `corpse_id_uniqueness` — under the
        # default true setting a corpse's id reserves the name too, so
        # spawning a fresh `goblin1` while a `goblin1` corpse exists
        # raises here instead of orphaning the corpse's identity.
        if self.id in match._taken_entity_ids():
            raise DuplicateId(
                f"Entity id '{self.id}' already exists in this match "
                f"(as a live entity or a corpse — see the "
                f"`corpse_id_uniqueness` rule)."
            )

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
        # Inject the system's default passives BEFORE on_entity_spawned
        # fires, so a default passive can itself react to the spawn. Skip
        # any whose id the entity already carries — keeps revive (which
        # re-runs spawn on a corpse snapshot that already has them)
        # idempotent, and lets a per-entity definition shadow a default.
        match._apply_default_passives(self)
        log = match.fire_hook("on_entity_spawned", target_ids=[self.id])
        # Spawn event — but NOT during a revive (the entity sits in the
        # death-suppressed set only across the revive's spawn window), so
        # a revive emits just its own "revive" event, not a duplicate
        # "spawn". A normal !ent add / summon is never in that set.
        if self.id not in match._death_check_suppressed_ids:
            match.log_event("spawn", entity=self.id, name=self.name,
                            x=self.x, y=self.y)
        return (self.id, log)

    def remove(self):
        """
        Remove this entity from its match and turn order.
        Also scrubs the entity from every group it was a member of, so
        no group is left holding a dangling id.
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
        m._scrub_entity_from_groups(self.id)
        # Drop any entity-attached scheduled effects bound to this entity
        # — a dead/despawned entity's pending turn-delayed effects don't
        # fire and don't dangle to a future same-id entity.
        m._prune_entity_scheduled(self.id)
        self._match = None
        m._rebuild_turn_order()

    # Teleport (absolute move)
    def tp(self, x: int, y: int, *, fire_hooks: bool = True) -> List[str]:
        """Teleport this entity to (x, y).

        Validates bounds and occupancy, then mutates the position. Tile
        hooks fire (when `fire_hooks=True`, the default) in this order:
          1. on_exit at the OLD coords, before the move — entity[self].x/.y
             still refer to the exit tile while the formula runs
          2. on_enter at the NEW coords, after the move
          3. on_stop at the NEW coords, after on_enter

        Returns the accumulated log lines from all three firings. Empty
        list when the moved-from and moved-to tiles have no hooks. The
        `fire_hooks=False` escape hatch is for callers that need a raw
        position change without trigger effects (e.g. history-restore
        rebuilds positions from a snapshot rather than re-running the
        moves that produced them).
        """
        m = self._require_match()
        if not m.in_bounds(x, y):
            raise OutOfBounds(f"({x},{y}) outside {m.grid_width}x{m.grid_height}")
        # Stackable movers skip the occupancy check entirely — they're
        # allowed to enter cells already holding non-stackable entities.
        # Combined with is_occupied skipping stackable RESIDENTS, the
        # net rule is: occupancy fails only when a non-stackable mover
        # enters a cell containing a non-stackable resident.
        if not self.is_cell_stackable and m.is_occupied(x, y, ignore_entity_id=self.id):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        log: List[str] = []
        old_x, old_y = self.x, self.y
        if fire_hooks and (old_x, old_y) != (x, y):
            # Skip on_exit when teleporting to the current cell (no
            # actual move). Matches the intuition that "tp to where I
            # am" is a no-op rather than a fire-and-reverse cycle.
            log.extend(m.fire_tile_hook("on_exit", self.id, old_x, old_y))
            log.extend(m.fire_zone_exit_hooks(self.id, old_x, old_y, x, y))
        self.move_to(x, y)
        if fire_hooks:
            log.extend(m.fire_tile_hook("on_enter", self.id, x, y))
            log.extend(m.fire_tile_hook("on_stop", self.id, x, y))
            # A tp is a single, final step; zone enter + stop fire here.
            # (For a same-cell tp, fire_zone_enter_hooks still fires
            # on_stop/on_cell_stop for the standing-on zones — a tp that
            # "lands" re-affirms its stop, matching on_stop's tile
            # semantics which fire even on a same-cell tp.)
            log.extend(m.fire_zone_enter_hooks(self.id, old_x, old_y, x, y, True))
            # Entity-side movement event. Fires once per tp call (not
            # per step), AFTER the tile hooks so a passive observing
            # the move sees the final (x, y) and any side-effects the
            # tile hooks already applied. Skip when the position
            # didn't actually change.
            if (old_x, old_y) != (x, y):
                log.extend(m.fire_entity_moved(
                    self.id, old_x, old_y, x, y,
                ))
        return log

    # Stepwise move (final cell must be free; rotate per step)
    def move_dirs(self, moves: list[tuple[str, int]], *,
                  fire_hooks: bool = True) -> List[str]:
        """Walk through `moves` step by step.

        Tile hooks fire per intermediate cell — on_exit at the cell
        you're leaving (pre-move), on_enter at the cell you arrive in
        (post-move). After the final step, on_stop fires once at the
        final cell. Walking through three fire tiles in a row therefore
        triggers three on_enter hooks (one per tile), accumulating
        damage across the path — matching the user's "walking through
        fire takes damage per tile" requirement.

        Two passes still: phase 1 simulates the path to validate
        bounds and final-cell occupancy without mutating anything;
        phase 2 commits cell-by-cell, firing hooks as it goes. If
        phase 1 raises (out-of-bounds, occupied) no hooks fire and no
        position change is applied — matching the all-or-nothing
        semantics of move_group_dirs.
        """
        m = self._require_match()
        allow_diag_move = bool(m.rules.get("allow_diagonal_movement", False))
        allow_diag_face = bool(m.rules.get("allow_diagonal_facing", False))

        # Phase 1: build the per-step path and validate bounds without
        # mutating. step_path is a list of (cell, facing) entries: each
        # cell is the post-step position, paired with the facing the
        # entity should adopt arriving there.
        step_path: List[Tuple[int, int, str]] = []
        x, y = self.x, self.y
        for direction, count in moves:
            canon = normalize_direction(direction)
            if canon is None:
                raise VTTError(f"Unknown direction '{direction}'")
            if canon in DIAGONAL_DIRECTIONS and not allow_diag_move:
                raise VTTError(
                    f"Diagonal direction '{direction}' is not allowed by "
                    f"the active game system. Enable rule "
                    f"'allow_diagonal_movement' to permit it."
                )
            dx, dy = DIRECTION_VECTORS[canon]
            # Per-step facing: diagonal moves update facing to the matching
            # diagonal IFF the system allows diagonal facing. Otherwise we
            # snap to the vertical component (preserves the legacy
            # "ties-prefer-vertical" feel for cardinal-only systems).
            step_facing = canon
            if canon in DIAGONAL_DIRECTIONS and not allow_diag_face:
                step_facing = "up" if dy < 0 else "down"
            for _ in range(max(1, int(count))):
                nx, ny = x + dx, y + dy
                if not m.in_bounds(nx, ny):
                    raise OutOfBounds(f"({nx},{ny}) outside {m.grid_width}x{m.grid_height}")
                step_path.append((nx, ny, step_facing))
                x, y = nx, ny
        # Stackable movers skip the final-cell occupancy check (see
        # Entity.tp for the symmetric rationale). move_dirs only
        # validates the final cell for occupancy, so this is the one
        # gate that needs the bypass.
        if not self.is_cell_stackable and m.is_occupied(x, y, ignore_entity_id=self.id):
            raise Occupied(f"Cell ({x},{y}) already occupied")

        # Phase 2: commit each step, firing hooks. No more validation —
        # phase 1 already proved the whole path is legal.
        log: List[str] = []
        origin_x, origin_y = self.x, self.y
        for nx, ny, step_facing in step_path:
            step_from_x, step_from_y = self.x, self.y
            if fire_hooks:
                log.extend(m.fire_tile_hook("on_exit", self.id, self.x, self.y))
                log.extend(m.fire_zone_exit_hooks(
                    self.id, step_from_x, step_from_y, nx, ny))
            self.facing = step_facing
            self.move_to(nx, ny)
            if fire_hooks:
                log.extend(m.fire_tile_hook("on_enter", self.id, nx, ny))
                # Per-step zone enter (boundary on_enter + per-cell
                # on_cell_enter); the stop hooks fire once after the loop
                # at the final cell, like tile on_stop below.
                log.extend(m.fire_zone_enter_hooks(
                    self.id, step_from_x, step_from_y, nx, ny, False))
                # Per-step entity hook: fires AFTER on_enter so
                # passives see whatever the just-entered tile already
                # applied (e.g. a damage tile's hp delta) on top of
                # the position change. Same binding shape as
                # on_entity_moved (from_x/from_y/to_x/to_y) so a
                # passive body can swap hooks without rewriting refs.
                log.extend(m.fire_entity_step(
                    self.id, step_from_x, step_from_y, nx, ny,
                ))
        if fire_hooks and step_path:
            # on_stop fires once at the final cell, even if no actual
            # movement happened (zero-step move_dirs) — empty step_path
            # means no transit and therefore no stop.
            log.extend(m.fire_tile_hook("on_stop", self.id, self.x, self.y))
            log.extend(m.fire_zone_stop_hooks(self.id, self.x, self.y))
            # on_entity_moved fires ONCE for the whole stepwise move,
            # with from = origin and to = final position. Step-level
            # observation is via tile on_enter/on_exit; on_entity_moved
            # answers "did this entity move at all this command?"
            log.extend(m.fire_entity_moved(
                self.id, origin_x, origin_y, self.x, self.y,
            ))
        return log

    # Stats/initiative (entity-owned)
    def damage_entity(self, amount: int):
        was_alive = self.is_alive
        self.take_damage(amount)
        # The death-chokepoint inside write_var (which take_damage
        # routed through) may have already triggered death — which
        # detaches the entity from the match and rebuilds turn order.
        # In that case we have nothing more to do; calling
        # _require_match() here would raise. Otherwise, preserve the
        # legacy "hp went to 0 without the death pipeline firing"
        # rebuild, which can still happen when the GM has set
        # death_condition to something that doesn't gate on hp.
        if was_alive and not self.is_alive and self._match is not None:
            self._match._rebuild_turn_order()

    def heal_entity(self, amount: int):
        was_alive = self.is_alive
        self.heal(amount)
        if (not was_alive) and self.is_alive:
            self._require_match()._rebuild_turn_order()

    def set_initiative_entity(self, value: int) -> List[str]:
        # Route through write_var directly (rather than the property
        # setter) so we can capture and return the resulting log
        # lines — !ent init then surfaces them. write_var dirties
        # turn order through the active change_policy rule
        # (immediate rebuild by default; deferred queues it for round
        # end). No explicit _rebuild_turn_order call here — that
        # would bypass the policy for !ent init specifically and
        # split the behavior model from formula-driven writes.
        _, _, init_var = self._vital_var_names()
        return self.write_var(init_var, int(value))

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
        # If this write touched the turn-order or team variable, the
        # current turn_order is potentially stale. Route through
        # _mark_turn_order_dirty so the active change_policy decides
        # whether to rebuild now (immediate) or queue for round end
        # (deferred). Done AFTER events fire so any var-hook side
        # effects from this write are also accounted for in the
        # subsequent rebuild — and so a clamp on the init var
        # produces an accurate "stored value" in the reason string.
        if self._match is not None:
            init_var = self._match.rules.get("turnorder_var", "initiative")
            team_var = self._match.rules.get("team_var", "team")
            for ev in (ancestor_events + leaf_events):
                if ev.key == init_var:
                    self._match._mark_turn_order_dirty(
                        f"`{self.id}`.{init_var}: "
                        f"{ev.old_value!r} → {ev.new_value!r}"
                    )
                    break
                if ev.key == team_var:
                    self._match._mark_turn_order_dirty(
                        f"`{self.id}`.{team_var}: "
                        f"{ev.old_value!r} → {ev.new_value!r}"
                    )
                    break
        # Drain accumulated warnings ONLY at top-level. Nested writes
        # (from passive formulas) accumulate warnings on the match but
        # don't surface them — the outermost call collects them all.
        if is_top_level_write and self._match is not None and self._match._var_event_warnings:
            combined.extend(self._match._var_event_warnings)
            self._match._var_event_warnings = []
        # Damage / heal event logging. A top-level NUMERIC change to the
        # hp var becomes a damage (decrease) or heal (increase) event —
        # logged before the death check so a fatal blow reads
        # "damage ... then death" in the log. Only 'changed' events count
        # (an initial 'created' hp write at spawn isn't damage). Skipped
        # while this entity is in the revive window (its hp-restore is the
        # revive mechanic, not combat healing — the revive emits a single
        # 'revive' event, same reason spawn is suppressed there). No-op
        # unless those event types are enabled.
        if (is_top_level_write and self._match is not None
                and self.id in self._match.entities
                and self.id not in self._match._death_check_suppressed_ids
                and self._match.rules.get("event_log_enabled", True)):
            hp_var = self._match.rules.get("hp_var", "hp")
            for ev in leaf_events:
                if ev.key != hp_var or ev.kind != "changed":
                    continue
                old_v, new_v = ev.old_value, ev.new_value
                if (isinstance(old_v, (int, float)) and not isinstance(old_v, bool)
                        and isinstance(new_v, (int, float)) and not isinstance(new_v, bool)
                        and new_v != old_v):
                    self._match.log_event(
                        "damage" if new_v < old_v else "heal",
                        entity=self.id, name=self._match._entity_name(self.id),
                        var=hp_var, old=old_v, new=new_v,
                        amount=abs(new_v - old_v),
                    )
                break
        # Death check. Runs at TOP-LEVEL writes only — a passive whose
        # body writes more vars accumulates events in the same chain;
        # we don't want to evaluate the death condition mid-chain
        # (it might transiently look truthy before a healing passive
        # has fired). The check is also skipped during in-flight death
        # processing via Match._death_processing so an on_death passive
        # writing vars doesn't recursively trigger another death of
        # the same entity. Self.id may already be gone from the match
        # if a passive earlier in the chain removed it — check_death
        # handles that. This is the single chokepoint for "did this
        # write kill the entity?" — it covers hp damage, status writes,
        # arbitrary GM conditions, and writes via formulas/actions.
        if (is_top_level_write and self._match is not None
                and self.id in self._match.entities):
            combined.extend(self._match.check_death(self.id))
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
        # Removing the init or team var also dirties turn order — same
        # policy gate as write_var above. Initiative being removed means
        # the entity drops out of turn_order entirely (since
        # _rebuild_turn_order filters on initiative is not None), so
        # this matters even under the deferred policy at round end.
        if self._match is not None:
            init_var = self._match.rules.get("turnorder_var", "initiative")
            team_var = self._match.rules.get("team_var", "team")
            if path == init_var:
                self._match._mark_turn_order_dirty(
                    f"`{self.id}`.{init_var} removed (was {old!r})"
                )
            elif path == team_var:
                self._match._mark_turn_order_dirty(
                    f"`{self.id}`.{team_var} removed (was {old!r})"
                )
        if is_top_level_write and self._match is not None and self._match._var_event_warnings:
            log.extend(self._match._var_event_warnings)
            self._match._var_event_warnings = []
        # Death check — same chokepoint policy as write_var (removing
        # a var can change the death condition's verdict: e.g. removing
        # a 'protected' status that gated death). Top-level only;
        # skipped during in-flight death processing.
        if (is_top_level_write and self._match is not None
                and self.id in self._match.entities):
            log.extend(self._match.check_death(self.id))
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
        return {
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "id": self.id,
            "status": copy.deepcopy(self.status),
            "vars": dict(self.vars),
            "passives": {pid: p.to_dict() for pid, p in self.passives.items()},
            "clamps": {path: c.to_dict() for path, c in self.clamps.items()},
            "facing": self.facing,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Entity":
        e = Entity(
            name=data["name"],
            x=int(data["x"]),
            y=int(data["y"]),
            id=str(data["id"]),
            # status is now dict-per-flag; support both shapes so saves
            # written by clones via Entity.to_dict (the common path) load
            # cleanly. Anything else is treated as an empty status set.
            status=_coerce_status_dict(data.get("status")),
            vars=dict(data.get("vars", {})),
            facing=data.get("facing", "up"),
        )
        for pid, pd in (data.get("passives", {}) or {}).items():
            e.passives[pid] = Passive.from_dict(pd)
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
    #global round counter, starts at 1. Increments by 1 each time every
    #entity in the turn order has had their turn and the cycle wraps
    #back to the first entity. A "turn" is per-entity (active_index
    #points to whose turn it is); a "round" is the full lap through
    #turn_order. The two are deliberately distinct: on_turn_* hooks
    #fire once per entity per round, while on_round_* hooks fire
    #once per round across the whole table.
    round_number: int = 1
    # Tracks whether the very first `on_round_start`/`on_turn_start` have fired
    # for this match. False until the first `Match.next_turn()` call. Used to
    # make that first call begin the round (fire start-hooks for active_index)
    # rather than advance past entity 0.
    round_started: bool = False
    # Game system binding - currently NOT YET DIRECTLY CONNECTED TO A GAMESYSTEM CLASS, JUST COPYING THE DICTIONARY OF RULES FROM IT.
    rules: Dict[str, Any] = field(default_factory=dict)  # denormalized copy for fast access

    #global passives that apply to every entity in turn order on each hook fire
    global_passives: Dict[str, Passive] = field(default_factory=dict)

    # Per-match save history (round/turn/command autosaves + manual
    # saves + undo). Lives in memory on the Match instance so it
    # vanishes when the match is removed from the MatchManager —
    # explicit cleanup isn't needed. Excluded from to_dict() by
    # default; pass include_history=True to bundle it into the save
    # file (used by `!store save <path> include_history=yes`).
    history: MatchHistory = field(default_factory=MatchHistory)

    # Group registry. Keyed by group name → ordered list of entity ids
    # belonging to that group. An entity can belong to any number of
    # groups; a group can be empty. Groups live separately from entity
    # vars (they're an organizational concept, not gameplay state) and
    # are mutated through Match.add_to_group / remove_from_group /
    # delete_group rather than directly. Entity.remove() scrubs the
    # removed entity from every group it belonged to so we never end
    # up with stale references. Ordering within each group is
    # insertion order, which is also the iteration order for any
    # command that targets the group.
    groups: Dict[str, List[str]] = field(default_factory=dict)

    # Special-tile data store. Sparse dict keyed by (x, y) tuple — only
    # coordinates the GM has explicitly written to live here. Each value
    # is a free-form dict of named "features" (e.g. {"flame":
    # {"burn_damage": 5}, "loot": {"gold": 50}, "glyph": "*"}); a single
    # tile can hold any number of side-by-side features. Reads from
    # formulas go through tile_get / tile_has in formula.py — the
    # validator rejects raw attribute chains on non-entity objects, so
    # tile data uses dotted-string paths instead of dot-attribute syntax.
    #
    # The "glyph" key, if set to a single character, overrides the
    # blank-cell rendering in !map (entities still take precedence).
    # Other keys are GM-defined and have no engine-level meaning until
    # later PRs add hooks and tile-attached passives on top.
    #
    # JSON serialization rewrites tuple keys as "x,y" strings (see the
    # _tile_key / _parse_tile_key helpers further down) because JSON
    # objects can't have non-string keys.
    tiles: Dict[Tuple[int, int], Dict[str, Any]] = field(default_factory=dict)

    # Zone registry. A zone is a NAMED region — a set of cells plus a
    # free-form data dict, optional hook formulas, and an optional map
    # glyph. Keyed by zone name. Each value is a dict:
    #   {"cells": Set[(x,y)], "data": {...}, "hooks": {when: src},
    #    "glyph": "g"}
    # Unlike tiles (one cell, keyed by coord), a zone spans many cells,
    # can overlap other zones and tiles, and can be reshaped or shifted
    # after creation (the "3x3 gas cloud that drifts" case). `cells` is a
    # set of (x,y) tuples in memory for O(1) membership; to_dict encodes
    # it as a sorted list of [x,y]. Data is read/written via the zone_*
    # formula functions and the !zone command (dotted paths under
    # "data"); hooks fire on zone-boundary crossings (on_enter/exit/stop),
    # per cell within the zone (on_cell_enter/exit/stop), and at
    # round/turn lifecycle moments. See the zone_* methods below.
    zones: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # Special-tile TEMPLATE registry. Reusable tile-kind definitions
    # (e.g. "flame", "spike", "treasure_chest"). Each template carries
    # default data and hook formulas; a placed tile that references a
    # template via its reserved `_template` field inherits the hooks
    # live (template-edit propagates) and a deep copy of the data
    # (instance-edit doesn't propagate). System-level templates are
    # copied in at match creation (see MatchManager.new_match); the
    # match can extend/shadow with !tile def commands.
    tile_templates: Dict[str, SpecialTileTemplate] = field(default_factory=dict)

    # User-defined formula functions, keyed by name. Defined via !func
    # def and callable by name from any formula on this match. Seeded
    # from the bound GameSystem's formula_functions at creation (same
    # inherit-at-create pattern as tile_templates) and serialized with
    # the match. See the FormulaFunction dataclass for the call model.
    formula_functions: Dict[str, "FormulaFunction"] = field(default_factory=dict)

    # Per-match command aliases. `aliases[name] = expansion` means a
    # subsequent `!<name> <args>` expands to `!<expansion> <args>`. The
    # expansion can itself include trailing literal tokens
    # ("dmg" -> "ent hp this") so `!dmg -5` becomes `!ent hp this -5`.
    # Aliases live on the match (not the system) because they're a
    # session-shaping convenience the GM tweaks on the fly; serialized
    # with the match so they survive save/load. Resolution happens at
    # the registry dispatch layer (see CommandRegistry.run) BEFORE the
    # handler lookup, so an alias can shadow a built-in name on this
    # match without affecting other matches.
    aliases: Dict[str, str] = field(default_factory=dict)

    # Match-level scratchpad vars. A free-form dict of GM-defined global
    # state that belongs to the MATCH rather than any single entity —
    # "alarm_level", "objective_progress", "weather", a doom counter,
    # etc. Mirrors the dict-shaped, dotted-path conventions used by
    # entity vars and tile data: formulas read/write via the reserved
    # `match` root (`match.alarm_level = match.alarm_level + 1`) or the
    # match_var_* functions for runtime paths; the command surface is
    # `!match var set/get/del/list`. Deliberately UN-opinionated — the
    # engine attaches no meaning to any key. Unlike entity vars, writes
    # here do NOT fire var hooks (there's no per-entity passive owner to
    # observe them); a global passive that needs to react should watch
    # the entity-side effect it triggers. Serialized with the match.
    vars: Dict[str, Any] = field(default_factory=dict)

    # Scheduled / delayed effects queue. Each entry is a dict:
    #   {name, kind, body, eid?, fire_round? / turns_left?}
    # kind "round" entries are MATCH-level (eid=None) and fire at
    # on_round_start once round_number reaches fire_round; kind "turn"
    # entries are ENTITY-attached (eid set) and fire at that entity's
    # on_turn_start once turns_left counts down to 0. Created via the
    # schedule() / schedule_on() formula primitives, cancellable by name
    # via cancel_schedule(). Entity-attached entries are pruned when their
    # entity is removed (death or despawn) — that's the "dropped if the
    # entity dies" contract. Serialized with the match. See
    # Match.add_scheduled / fire_scheduled_round / fire_scheduled_turn.
    scheduled: List[Dict[str, Any]] = field(default_factory=list)

    # Structured event log (the combat log). Each entry is a dict with at
    # least {type, round, turn, ...type-specific fields}. The engine
    # appends curated combat events at its chokepoints (death, action,
    # status, move, hp change, spawn/revive) when the matching
    # event_log_<type> rule is on; the log() formula primitive appends
    # custom entries. Rendering to text happens at read time (!log) via
    # the event_log_format rule's per-type templates. Capped by
    # event_log_retention. Serialized with the match. See
    # Match.log_event.
    event_log: List[Dict[str, Any]] = field(default_factory=list)

    # ---- host / access control ----
    # `owner` is the user id (Discord author id, or "cli" at the CLI) of
    # whoever created the match. The owner has full command privileges
    # and is the ONLY identity that can manage the host list. `cohosts`
    # are user ids the owner has appointed; they share the owner's
    # command privileges but cannot themselves appoint/remove hosts.
    # Together {owner} | cohosts are "the hosts" — see is_host(). A
    # command sent by a non-host against this match is held for host
    # approval (see pending_requests) rather than executed. Both persist
    # with the match; the owner is set at creation by MatchManager.
    owner: Optional[str] = None
    cohosts: List[str] = field(default_factory=list)

    # Per-match host overrides for the command access gate, keyed by
    # command name or "name sub" -> level ("all"/"host"/"host_only"/
    # "owner"). Set by the owner via `!host access`. Takes precedence over
    # the system-level command_access rule AND survives a system rule
    # refresh (it's match state, not a snapshotted rule), so a host can
    # lock down reads for a fog-of-war match without the next !system set
    # wiping it. Persisted with the match.
    access_overrides: Dict[str, str] = field(default_factory=dict)

    # Channels bound to this match. channel_key -> metadata dict (an
    # optional free-form {"label": ...} reserved for the upcoming
    # fog-of-war / per-team-view work; the engine attaches no meaning to
    # it yet). MULTIPLE channels can bind one match, uncapped — e.g. a
    # host channel plus a players channel, or one channel per team. The
    # manager's active_by_channel pointer is kept in sync (binding a
    # channel makes the match active there). Persisted with the match.
    bound_channels: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    # ---- runtime-only: pending approval queue ----
    # Requests from non-host users awaiting host approval, keyed by a
    # short per-match id ("r1", "r2", ...). Each value is a dict
    # {id, user, user_name, name, args, channel_key}. NOT serialized —
    # ephemeral, lives only as long as the in-memory match. Mutated via
    # add_pending_request / pop_pending_request.
    pending_requests: Dict[str, Dict[str, Any]] = field(
        default_factory=dict, repr=False)
    _request_seq: int = field(default=0, repr=False)

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

    # Deferred turn-order rebuild flag. Set true when a write to an
    # entity's turnorder_var or team_var happens under the 'deferred'
    # change policy; the actual rebuild runs at round end. Always
    # false under 'immediate' policy (the rebuild ran inline so
    # nothing was deferred). Cleared at every successful rebuild.
    _turn_order_dirty: bool = field(default=False, repr=False)

    # Runtime-only seeded RNG for formulas, lazily built by the formula
    # engine when the random_seed rule is non-empty. Not serialized: the
    # seed (a rule) is saved, but the live cursor position is not — a
    # reloaded match reseeds from scratch. _rng_seed tracks which seed
    # _rng was built with so a seed change triggers a rebuild.
    _rng: Any = field(default=None, repr=False, compare=False)
    _rng_seed: Any = field(default=None, repr=False, compare=False)

    # ---- action runtime state --------------------------------------------
    # Tracks how deep we are in a chain of action invocations. Bumped at
    # the start of an action body and decremented after; gates two
    # behaviors:
    #   1. recursion limit — `use_action()` past action_recursion_limit
    #      raises ActionFail without blowing Python's stack.
    #   2. snapshot suspension — when > 0, the command dispatcher
    #      skips its per-command pre/post snapshot; only the action's
    #      outer snapshot pair counts (the transactional unit the GM
    #      cares about). Mirrors the `dispatch_no_snapshot` path used
    #      by !batch / !run, but flag-driven so nested cmd() calls
    #      inside an action body don't have to re-route themselves.
    _action_depth: int = field(default=0, repr=False)
    # Shared output buffer for the in-flight top-level action. cmd()
    # dispatches into this (a _BufferCtx) instead of the real reply
    # context because the formula engine is synchronous and can't await
    # a real network send(); the top-level run_action flushes it through
    # the real context after the body completes. None when no action is
    # running. Not serialized — purely transient dispatch state.
    _runtime_buffer: Any = field(default=None, repr=False, compare=False)
    # Per-command summon counter, reset to 0 at the start of every
    # top-level command dispatch (see CommandRegistry.run). Incremented
    # by summon_entity; once it reaches summon_event_limit, further
    # summons in the same command raise. Not serialized — transient
    # safety state.
    _summon_count: int = field(default=0, repr=False, compare=False)
    # Re-entry guard for the death-condition chokepoint. The check runs
    # at the top of every write_var chain; if the condition write
    # itself causes further var writes (clamp, on_death passive,
    # corpse-tile mutation), we must NOT re-evaluate the condition
    # mid-collapse — set to >0 while processing a death to short-
    # circuit nested checks. Reset to 0 once the death pipeline
    # finishes. Not serialized.
    _death_processing: int = field(default=0, repr=False, compare=False)
    # Per-entity death-check suppression. Holds the ids whose automatic
    # death-condition re-check is currently deferred — used by
    # revive_corpse to shield ONLY the entity being respawned from its
    # transient corpse death-state (hp<=0) while spawn / revive-effects /
    # on_revive run, WITHOUT masking deaths the same writes might cause on
    # other entities (which the match-global _death_processing counter
    # would). Explicit kill() bypasses check_death and so is unaffected —
    # only the automatic condition check is deferred. Not serialized.
    _death_check_suppressed_ids: Set[str] = field(
        default_factory=set, repr=False, compare=False
    )

    # ---- global constraints / helpers (unchanged in spirit) ----
    def in_bounds(self, x: int, y: int) -> bool:
        return 1 <= x <= self.grid_width and 1 <= y <= self.grid_height

    # ---- tile data helpers --------------------------------------------
    # Tiles live in self.tiles keyed by (x, y) tuples. These methods are
    # the chokepoints for set / get / delete so that JSON serialization,
    # bounds checking, and intermediate-dict creation happen in one
    # place — both !tile commands and the formula-side tile_get /
    # tile_has functions reach for them.

    def tile_data(self, x: int, y: int) -> Dict[str, Any]:
        """Return the tile's data dict at (x, y), creating an empty
        entry if the tile has no data yet. Caller mutates in place.
        Out-of-bounds coordinates raise OutOfBounds — silently auto-
        creating off-grid tiles would mask GM typos."""
        if not self.in_bounds(x, y):
            raise OutOfBounds(
                f"({x},{y}) outside {self.grid_width}x{self.grid_height}"
            )
        return self.tiles.setdefault((x, y), {})

    def tile_get_path(self, x: int, y: int, path: str) -> Any:
        """Read tile_data[x,y][a][b][c] for a dotted path 'a.b.c'.
        Raises NotFound on any missing segment so the user gets a
        precise error like \"tile (5,5) has no feature 'flame'\"
        rather than a Python KeyError. Empty-path '' is rejected —
        the bare 'tile data dict' isn't a useful thing to expose."""
        if not self.in_bounds(x, y):
            raise OutOfBounds(
                f"({x},{y}) outside {self.grid_width}x{self.grid_height}"
            )
        if not path:
            raise VTTError("tile path cannot be empty.")
        d: Any = self.tiles.get((x, y), {})
        parts = path.split(".")
        for i, key in enumerate(parts):
            if not isinstance(d, dict) or key not in d:
                # Build a useful path-prefix for the error so the GM
                # knows where the chain broke.
                where = ".".join(parts[:i + 1])
                raise NotFound(
                    f"tile ({x},{y}) has no value at '{where}'."
                )
            d = d[key]
        return d

    def tile_has_path(self, x: int, y: int, path: str) -> bool:
        """Boolean version of tile_get_path. Out-of-bounds returns
        False (a missing tile is conceptually 'has no feature' rather
        than an error) so formulas can defensively guard against
        passing entity coords that walked off-grid for some reason."""
        if not self.in_bounds(x, y):
            return False
        if not path:
            return False
        d: Any = self.tiles.get((x, y), {})
        for key in path.split("."):
            if not isinstance(d, dict) or key not in d:
                return False
            d = d[key]
        return True

    def tile_set_path(self, x: int, y: int, path: str, value: Any) -> None:
        """Set tile_data[x,y][a][b][c] = value, creating intermediate
        dicts as needed (same write semantics as Entity.write_var,
        but on tile dicts). Out-of-bounds raises; empty path raises;
        path that collides with a non-dict value mid-traversal raises
        (we don't silently clobber a scalar with a dict)."""
        if not self.in_bounds(x, y):
            raise OutOfBounds(
                f"({x},{y}) outside {self.grid_width}x{self.grid_height}"
            )
        if not path:
            raise VTTError("tile path cannot be empty.")
        d = self.tiles.setdefault((x, y), {})
        parts = path.split(".")
        for i, key in enumerate(parts[:-1]):
            existing = d.get(key)
            if existing is not None and not isinstance(existing, dict):
                where = ".".join(parts[:i + 1])
                raise VTTError(
                    f"tile ({x},{y}) value at '{where}' is "
                    f"{type(existing).__name__}, not a dict — cannot "
                    f"set a nested key under it without clobbering."
                )
            if key not in d:
                d[key] = {}
            d = d[key]
        d[parts[-1]] = value

    def tile_del_path(self, x: int, y: int, path: Optional[str]) -> None:
        """Delete a dotted-path key from a tile's data, OR (when
        path is None / empty) delete the entire tile entry from
        self.tiles. After a path-delete, walk back up the chain and
        drop any parent dict that became empty, finishing with the
        (x,y) entry itself — every entry in self.tiles always has
        at least one populated feature so the serialized form
        stays sparse and !tile list doesn't show no-op coords."""
        if (x, y) not in self.tiles:
            return  # nothing to do — symmetric with remove_var_silent
        if not path:
            del self.tiles[(x, y)]
            return
        # Walk down, tracking each parent dict so we can prune empties
        # on the way back up.
        parts = path.split(".")
        chain: List[Dict[str, Any]] = [self.tiles[(x, y)]]
        for i, key in enumerate(parts[:-1]):
            d = chain[-1]
            if not isinstance(d, dict) or key not in d:
                where = ".".join(parts[:i + 1])
                raise NotFound(f"tile ({x},{y}) has no value at '{where}'.")
            chain.append(d[key])
        leaf_parent = chain[-1]
        leaf_key = parts[-1]
        if not isinstance(leaf_parent, dict) or leaf_key not in leaf_parent:
            raise NotFound(f"tile ({x},{y}) has no value at '{path}'.")
        del leaf_parent[leaf_key]
        # Prune empty parents from the leaf back to the root. Each
        # iteration drops the just-emptied dict from its grandparent
        # under the key we descended through.
        for i in range(len(chain) - 1, 0, -1):
            if chain[i]:
                break  # still has siblings — stop pruning
            parent = chain[i - 1]
            parent_key = parts[i - 1]
            del parent[parent_key]
        if not self.tiles[(x, y)]:
            del self.tiles[(x, y)]

    # ---- zones (named multi-cell regions) ----------------------------
    # A zone is a named SET of cells plus a free-form data dict, optional
    # hook formulas, and an optional map glyph (see the `zones` field and
    # ZONE_HOOK_NAMES). These methods are the chokepoints the !zone
    # command and the zone_* formula functions share. `cells` is held as
    # a set of (x,y) tuples in memory; serialization encodes it as a
    # sorted list of [x,y].
    @staticmethod
    def _zone_to_dict(z: Dict[str, Any]) -> Dict[str, Any]:
        cells = z.get("cells") or set()
        out: Dict[str, Any] = {
            "cells": [[x, y] for (x, y) in sorted(cells)],
            "data": z.get("data") or {},
            "hooks": z.get("hooks") or {},
        }
        g = z.get("glyph")
        if isinstance(g, str) and len(g) == 1:
            out["glyph"] = g
        return out

    def _zone_from_dict(self, zdef: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(zdef, dict):
            return None
        cells: Set[Tuple[int, int]] = set()
        raw_cells = zdef.get("cells")
        if isinstance(raw_cells, list):
            for pair in raw_cells:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    try:
                        x, y = int(pair[0]), int(pair[1])
                    except (TypeError, ValueError):
                        continue
                    if self.in_bounds(x, y):
                        cells.add((x, y))
        data = zdef.get("data")
        hooks = zdef.get("hooks")
        z: Dict[str, Any] = {
            "cells": cells,
            "data": copy.deepcopy(data) if isinstance(data, dict) else {},
            "hooks": {
                k: v for k, v in hooks.items()
                if isinstance(k, str) and isinstance(v, str)
            } if isinstance(hooks, dict) else {},
        }
        g = zdef.get("glyph")
        if isinstance(g, str) and len(g) == 1:
            z["glyph"] = g
        return z

    def has_zone(self, name: str) -> bool:
        return name in self.zones

    def _require_zone(self, name: str) -> Dict[str, Any]:
        z = self.zones.get(name)
        if z is None:
            raise NotFound(f"Zone '{name}' not found.")
        return z

    def create_zone(self, name: str, *, overwrite: bool = False) -> Dict[str, Any]:
        """Create an empty zone. Raises DuplicateId if it exists unless
        overwrite=True."""
        if not isinstance(name, str) or not name.strip():
            raise VTTError("Zone name must be a non-empty string.")
        if name in self.zones and not overwrite:
            raise DuplicateId(f"Zone '{name}' already exists.")
        self.zones[name] = {"cells": set(), "data": {}, "hooks": {}}
        return self.zones[name]

    def delete_zone(self, name: str) -> None:
        if name not in self.zones:
            raise NotFound(f"Zone '{name}' not found.")
        del self.zones[name]

    def zone_add_cell(self, name: str, x: int, y: int,
                      *, create: bool = True) -> bool:
        """Add cell (x,y) to a zone (creating the zone if missing and
        create=True). Returns True iff newly added. Off-grid cells are
        rejected."""
        if not self.in_bounds(x, y):
            raise OutOfBounds(
                f"({x},{y}) outside {self.grid_width}x{self.grid_height}"
            )
        z = self.zones.get(name)
        if z is None:
            if not create:
                raise NotFound(f"Zone '{name}' not found.")
            z = self.create_zone(name)
        if (x, y) in z["cells"]:
            return False
        z["cells"].add((x, y))
        return True

    def zone_remove_cell(self, name: str, x: int, y: int) -> bool:
        """Remove cell (x,y) from a zone. Returns True iff it was present.
        An emptied zone still exists (data/hooks preserved)."""
        z = self._require_zone(name)
        if (x, y) in z["cells"]:
            z["cells"].discard((x, y))
            return True
        return False

    def zone_fill_rect(self, name: str, x1: int, y1: int, x2: int, y2: int,
                       *, create: bool = True) -> int:
        """Add every in-bounds cell of the rectangle (x1,y1)-(x2,y2) to a
        zone (corners in any order). Returns the count of NEWLY added
        cells."""
        z = self.zones.get(name)
        if z is None:
            if not create:
                raise NotFound(f"Zone '{name}' not found.")
            z = self.create_zone(name)
        xa, xb = sorted((int(x1), int(x2)))
        ya, yb = sorted((int(y1), int(y2)))
        added = 0
        for xx in range(xa, xb + 1):
            for yy in range(ya, yb + 1):
                if self.in_bounds(xx, yy) and (xx, yy) not in z["cells"]:
                    z["cells"].add((xx, yy))
                    added += 1
        return added

    def zone_shift(self, name: str, dx: int, dy: int) -> int:
        """Translate every cell of the zone by (dx,dy). Cells that would
        leave the grid are DROPPED (a zone may drift off the edge and,
        with an opposite shift, partially return — only on-grid cells
        survive each shift). Returns the number of cells retained."""
        z = self._require_zone(name)
        dx, dy = int(dx), int(dy)
        new: Set[Tuple[int, int]] = set()
        for (x, y) in z["cells"]:
            nx, ny = x + dx, y + dy
            if self.in_bounds(nx, ny):
                new.add((nx, ny))
        z["cells"] = new
        return len(new)

    def zone_cell_list(self, name: str) -> List[List[int]]:
        z = self._require_zone(name)
        return [[x, y] for (x, y) in sorted(z["cells"])]

    def zone_size(self, name: str) -> int:
        """Cell count of a zone (0 if the zone doesn't exist)."""
        z = self.zones.get(name)
        return len(z["cells"]) if z is not None else 0

    def zones_at(self, x: int, y: int) -> Set[str]:
        """Names of every zone whose footprint includes (x,y)."""
        return {name for name, z in self.zones.items() if (x, y) in z["cells"]}

    def cell_in_zone(self, x: int, y: int, name: str) -> bool:
        z = self.zones.get(name)
        return z is not None and (x, y) in z["cells"]

    def entity_zones(self, eid: str) -> Set[str]:
        """Names of every zone the entity currently stands in."""
        e = self.entities.get(eid)
        if e is None:
            return set()
        return self.zones_at(e.x, e.y)

    def entities_in_zone(self, name: str) -> List[str]:
        """Ids of every alive entity standing on a cell of the zone, in
        insertion order. [] for a missing zone."""
        z = self.zones.get(name)
        if z is None:
            return []
        cells = z["cells"]
        return [
            eid for eid, e in self.entities.items()
            if getattr(e, "is_alive", True) and (e.x, e.y) in cells
        ]

    # ---- zone data accessors (dotted paths under zone["data"]) -------
    def zone_get_path(self, name: str, path: str) -> Any:
        z = self._require_zone(name)
        if not path:
            raise VTTError("zone path cannot be empty.")
        cur: Any = z["data"]
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                raise NotFound(f"zone '{name}' has no value at '{path}'.")
            cur = cur[key]
        return cur

    def zone_has_path(self, name: str, path: str) -> bool:
        z = self.zones.get(name)
        if z is None or not path:
            return False
        cur: Any = z["data"]
        for key in path.split("."):
            if not isinstance(cur, dict) or key not in cur:
                return False
            cur = cur[key]
        return True

    def zone_set_path(self, name: str, path: str, value: Any,
                      *, create: bool = True) -> None:
        z = self.zones.get(name)
        if z is None:
            if not create:
                raise NotFound(f"Zone '{name}' not found.")
            z = self.create_zone(name)
        if not path:
            raise VTTError("zone path cannot be empty.")
        d = z["data"]
        parts = path.split(".")
        for i, key in enumerate(parts[:-1]):
            existing = d.get(key)
            if existing is not None and not isinstance(existing, dict):
                where = ".".join(parts[:i + 1])
                raise VTTError(
                    f"zone '{name}' value at '{where}' is "
                    f"{type(existing).__name__}, not a dict — cannot set a "
                    f"nested key under it without clobbering."
                )
            if key not in d:
                d[key] = {}
            d = d[key]
        d[parts[-1]] = value

    def zone_del_path(self, name: str, path: str) -> None:
        """Delete a dotted key from a zone's data, pruning emptied
        parents. Raises NotFound if the path is absent."""
        z = self._require_zone(name)
        if not path:
            raise VTTError("zone path cannot be empty.")
        parts = path.split(".")
        chain: List[Dict[str, Any]] = [z["data"]]
        cur: Any = z["data"]
        for key in parts[:-1]:
            if not isinstance(cur, dict) or key not in cur:
                raise NotFound(f"zone '{name}' has no value at '{path}'.")
            cur = cur[key]
            chain.append(cur)
        leaf_parent = chain[-1]
        if not isinstance(leaf_parent, dict) or parts[-1] not in leaf_parent:
            raise NotFound(f"zone '{name}' has no value at '{path}'.")
        del leaf_parent[parts[-1]]
        for i in range(len(chain) - 1, 0, -1):
            if chain[i]:
                break
            del chain[i - 1][parts[i - 1]]

    def zone_clear_data(self, name: str) -> int:
        """Drop ALL data from a zone (keeps cells + hooks). Returns the
        number of top-level keys removed."""
        z = self._require_zone(name)
        n = len(z["data"])
        z["data"] = {}
        return n

    # ---- host / access control ---------------------------------------
    def is_owner(self, user: Optional[str]) -> bool:
        """True iff `user` is the match owner. An unset owner (legacy /
        CLI-less load) treats nobody as owner."""
        return user is not None and self.owner == user

    def is_host(self, user: Optional[str]) -> bool:
        """True iff `user` is the owner or an appointed co-host — i.e. has
        full command privileges on this match."""
        if user is None:
            return False
        return user == self.owner or user in self.cohosts

    def host_ids(self) -> List[str]:
        """Owner first, then co-hosts in appointment order. Empty if no
        owner is set."""
        out: List[str] = []
        if self.owner is not None:
            out.append(self.owner)
        out.extend(c for c in self.cohosts if c != self.owner)
        return out

    def add_cohost(self, user: str) -> bool:
        """Appoint a co-host. No-op (returns False) if they're already the
        owner or a co-host; raises on an empty id."""
        if not isinstance(user, str) or not user.strip():
            raise VTTError("co-host user id must be a non-empty string.")
        if user == self.owner or user in self.cohosts:
            return False
        self.cohosts.append(user)
        return True

    def remove_cohost(self, user: str) -> bool:
        """Remove a co-host. Returns False if they weren't one. The owner
        is never in cohosts and can't be removed here."""
        if user in self.cohosts:
            self.cohosts.remove(user)
            return True
        return False

    # ---- channel binding ---------------------------------------------
    def bind_channel(self, channel_key: str,
                     label: Optional[str] = None) -> bool:
        """Bind a channel to this match. Returns True if newly bound,
        False if it was already bound (label still updated). The caller
        (command layer) is responsible for keeping MatchManager's
        active_by_channel pointer in sync."""
        if not isinstance(channel_key, str) or not channel_key:
            raise VTTError("channel key must be a non-empty string.")
        newly = channel_key not in self.bound_channels
        meta = self.bound_channels.get(channel_key, {})
        if label is not None:
            meta["label"] = label
        self.bound_channels[channel_key] = meta
        return newly

    def unbind_channel(self, channel_key: str) -> bool:
        """Unbind a channel. Returns False if it wasn't bound."""
        if channel_key in self.bound_channels:
            del self.bound_channels[channel_key]
            return True
        return False

    # ---- pending approval queue --------------------------------------
    def add_pending_request(self, *, user: Optional[str], user_name: str,
                            name: str, args: List[str],
                            channel_key: str) -> Dict[str, Any]:
        """Queue a non-host command for host approval. Returns the stored
        request dict (its `id` is a short per-match token)."""
        self._request_seq += 1
        rid = f"r{self._request_seq}"
        req = {
            "id": rid,
            "user": user,
            "user_name": user_name,
            "name": name,
            "args": list(args),
            "channel_key": channel_key,
        }
        self.pending_requests[rid] = req
        return req

    def pop_pending_request(self, rid: str) -> Optional[Dict[str, Any]]:
        """Remove and return a pending request by id, or None if absent."""
        return self.pending_requests.pop(rid, None)

    # ---- tile-template registry --------------------------------------
    # Templates are the "kind" / "class" definitions; placed tiles in
    # self.tiles are the "instances". A placed tile may carry a reserved
    # `_template` field naming its source template — see place_template
    # below for the placement semantics (data is deep-copied at place
    # time, hooks are looked up live at fire time).

    def define_tile_template(
        self, name: str, *,
        data: Optional[Dict[str, Any]] = None,
        hooks: Optional[Dict[str, str]] = None,
        overwrite: bool = False,
    ) -> SpecialTileTemplate:
        """Create or replace a tile template. `overwrite=False` raises
        DuplicateId if the name is already taken — keeps the GM from
        clobbering a template by accident. Returns the stored template."""
        if not isinstance(name, str) or not name:
            raise VTTError("tile template name must be a non-empty string.")
        if not overwrite and name in self.tile_templates:
            raise DuplicateId(f"tile template '{name}' already exists.")
        # Reject reserved-key collisions in template data: a template
        # whose data dict carries `_template` or `hooks` would corrupt
        # the placed tile's structure (the placement merge would
        # silently overwrite the instance's tracking fields). Catching
        # at define time is louder than catching at place time.
        data_dict = dict(data or {})
        bad = TILE_RESERVED_KEYS & data_dict.keys()
        if bad:
            raise VTTError(
                f"template data may not use reserved keys: "
                f"{', '.join(sorted(bad))}."
            )
        # Hooks must be strings (formula source). Validation of the
        # formula syntax is the caller's job (vtt_commands runs
        # validate_formula at !tile def hook time) — this method just
        # stores; tests and internal callers shouldn't have to round
        # through the command layer.
        hooks_dict: Dict[str, str] = {}
        for w, src in (hooks or {}).items():
            if not isinstance(src, str):
                raise VTTError(
                    f"template '{name}' hook '{w}' source must be a string."
                )
            hooks_dict[str(w)] = src
        tpl = SpecialTileTemplate(
            name=name,
            data=copy.deepcopy(data_dict),
            hooks=hooks_dict,
        )
        self.tile_templates[name] = tpl
        return tpl

    def undefine_tile_template(self, name: str) -> int:
        """Remove a template. Returns the number of placed instances
        that referenced it (now orphaned — they still exist as tiles
        with a `_template` field pointing at a missing definition;
        fire_tile_hook degrades gracefully by emitting a warning and
        no-opping on hook firings for those tiles)."""
        if name not in self.tile_templates:
            raise NotFound(f"tile template '{name}' not found.")
        del self.tile_templates[name]
        orphan_count = 0
        for tile in self.tiles.values():
            if isinstance(tile, dict) and tile.get("_template") == name:
                orphan_count += 1
        return orphan_count

    def place_tile_template(
        self, name: str, x: int, y: int,
        *, overrides: Optional[Dict[str, Any]] = None,
        replace: bool = True,
    ) -> Dict[str, Any]:
        """Instantiate template `name` at (x, y), returning the resulting
        tile dict. Default `replace=True` discards any existing tile data
        at (x, y) — the GM is explicitly placing a new instance, not
        merging onto an existing tile. Set `replace=False` to error
        instead when the cell already carries tile data.

        The placement copies template.data deeply onto a fresh dict,
        applies any `overrides` on top (single-level merge — pass a
        deep override structure pre-merged if you need nested control),
        and tags the result with `_template`. The template's hooks are
        NOT copied — they're looked up live at fire time so editing
        the template's hooks affects all placed instances. The instance
        can shadow individual hooks via the existing !tile hook add
        surface (or by direct ['hooks'][when] mutation).
        """
        if name not in self.tile_templates:
            raise NotFound(f"tile template '{name}' not found.")
        if not self.in_bounds(x, y):
            raise OutOfBounds(
                f"({x},{y}) outside {self.grid_width}x{self.grid_height}"
            )
        if not replace and (x, y) in self.tiles:
            raise DuplicateId(
                f"tile ({x},{y}) already has data; pass replace=True "
                f"or delete first."
            )
        tpl = self.tile_templates[name]
        tile: Dict[str, Any] = copy.deepcopy(tpl.data)
        if overrides:
            bad = TILE_RESERVED_KEYS & overrides.keys()
            if bad:
                raise VTTError(
                    f"placement overrides may not use reserved keys: "
                    f"{', '.join(sorted(bad))}."
                )
            tile.update(copy.deepcopy(overrides))
        tile["_template"] = name
        self.tiles[(x, y)] = tile
        return tile

    def tile_template_for(self, x: int, y: int) -> Optional[SpecialTileTemplate]:
        """Return the SpecialTileTemplate this tile was placed from, or
        None if the tile is ad-hoc / off-grid / has a dangling
        `_template` reference (template was deleted after placement).
        The dangling case is intentional: fire_tile_hook returns a
        warning for it rather than silently swallowing the miss."""
        if not self.in_bounds(x, y):
            return None
        tile = self.tiles.get((x, y))
        if not isinstance(tile, dict):
            return None
        tpl_name = tile.get("_template")
        if not isinstance(tpl_name, str):
            return None
        return self.tile_templates.get(tpl_name)

    # ---- formula-function registry -----------------------------------
    # User-defined formula functions live in self.formula_functions and
    # are callable by name from any formula on this match. The command
    # layer (!func def) validates the name / params / body before
    # calling define_formula_function; this method enforces the
    # structural invariants (valid identifiers, unique params) but
    # does NOT compile the body — that happens lazily in the formula
    # engine on first call.

    def define_formula_function(
        self, name: str, params: List[str], body: str,
        *, overwrite: bool = True,
    ) -> "FormulaFunction":
        """Create or replace a formula function. Validates that the name
        and every parameter is a legal identifier, parameters are
        unique, and the name doesn't collide with a built-in formula
        function. Does NOT validate the body here (the command layer
        does that via validate_formula so it can pass the right
        known-function / known-param sets); a bad body would simply
        fail at call time. Returns the stored FormulaFunction.

        overwrite defaults True (redefining is the common iterate-on-a-
        formula workflow); pass overwrite=False to raise DuplicateId on
        an existing name instead.
        """
        if not isinstance(name, str) or not _FUNC_NAME_RE.match(name):
            raise VTTError(
                f"formula function name '{name}' must be a valid "
                f"identifier (letters, digits, underscore; not starting "
                f"with a digit)."
            )
        # Lazy import to avoid a logic <-> formula import cycle at module
        # load. We only need the reserved-name set.
        from formula import _ALLOWED_FUNCS, _MATCH_FUNC_NAMES
        if name in _ALLOWED_FUNCS or name in _MATCH_FUNC_NAMES:
            raise VTTError(
                f"'{name}' is a built-in formula function and cannot be "
                f"redefined."
            )
        if not overwrite and name in self.formula_functions:
            raise DuplicateId(f"formula function '{name}' already exists.")
        seen = set()
        for p in params:
            if not isinstance(p, str) or not _FUNC_NAME_RE.match(p):
                raise VTTError(
                    f"parameter name '{p}' must be a valid identifier."
                )
            if p in seen:
                raise VTTError(f"duplicate parameter name '{p}'.")
            seen.add(p)
            if p in _ALLOWED_FUNCS or p in _MATCH_FUNC_NAMES:
                raise VTTError(
                    f"parameter name '{p}' collides with a built-in "
                    f"formula function; pick a different name."
                )
            # `entity` is the magic accessor keyword and __-prefixed
            # names are transformer-injected internals; a parameter
            # using either would either shadow the accessor or never be
            # reachable. Reject both up front.
            if p == "entity" or p.startswith("__"):
                raise VTTError(
                    f"parameter name '{p}' is reserved; pick a different "
                    f"name."
                )
        fn = FormulaFunction(name=name, params=list(params), body=body)
        self.formula_functions[name] = fn
        return fn

    def undefine_formula_function(self, name: str) -> None:
        """Remove a formula function. Raises NotFound if absent. Note:
        other functions or passives that referenced this one will fail
        at their next call (the name simply won't resolve) — we don't
        scan for dependents here, matching how tile-template deletion
        leaves orphaned instances to surface at fire time."""
        if name not in self.formula_functions:
            raise NotFound(f"formula function '{name}' not found.")
        del self.formula_functions[name]

    def is_occupied(self, x: int, y: int, ignore_entity_id: Optional[str] = None) -> bool:
        """True when the cell (x, y) holds an entity that BLOCKS other
        entities from entering — i.e. an alive non-stackable entity.
        Stackable entities (vars `__cell_stackable` truthy) don't count
        as occupying their cell; a tile holding only stackable residents
        still answers False here. Pair this with the mover-side check
        in Entity.tp / move_dirs / Match.summon_entity: those skip the
        is_occupied call entirely when the MOVER itself is stackable,
        so a stackable entity can also enter a cell that has a
        non-stackable resident."""
        for eid, e in self.entities.items():
            if ignore_entity_id and eid == ignore_entity_id:
                continue
            if e.x == x and e.y == y and e.is_alive and not e.is_cell_stackable:
                return True
        return False

    def push_entity(self, eid: str, direction: str, n: int = 1) -> Tuple[int, List[str]]:
        """Forced movement: step `eid` up to `n` cells in `direction`,
        stopping at the first blocked cell (off-grid, or — for a non-
        stackable mover — an occupied cell). Returns
        (cells_actually_moved, hook_log). Honors allow_diagonal_movement.
        The committed steps go through Entity.move_dirs, so per-step tile
        on_enter/on_exit and the final on_stop / on_entity_moved fire the
        same way a stepwise `!ent move` would — a pushed entity walks
        through fire tiles cell by cell. Raises VTTError on an unknown
        entity, a non-string / unknown direction, a disallowed diagonal,
        or a non-int `n`.

        This is the shared implementation behind both the push_entity()
        formula primitive and the `!ent push` command."""
        e = self.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found.")
        if not isinstance(direction, str):
            raise VTTError("push: direction must be a string.")
        canon = normalize_direction(direction)
        if canon is None:
            raise VTTError(f"push: unknown direction '{direction}'.")
        allow_diag = bool(self.rules.get("allow_diagonal_movement", False))
        if canon in DIAGONAL_DIRECTIONS and not allow_diag:
            raise VTTError(
                f"push: diagonal direction '{direction}' requires "
                f"allow_diagonal_movement=True."
            )
        if not isinstance(n, int) or isinstance(n, bool):
            raise VTTError("push: n must be an integer.")
        if n <= 0:
            return 0, []
        dx, dy = DIRECTION_VECTORS[canon]
        # Walk forward to find the longest legal prefix. The push stops
        # at the cell BEFORE the first blocker. Intermediate occupancy
        # matters — shoving an entity THROUGH another isn't allowed
        # (matches the "knockback hits the wall behind them" intuition).
        # A stackable pushee ignores occupancy; only bounds stop it.
        x, y = e.x, e.y
        steps = 0
        stackable = e.is_cell_stackable
        for _ in range(n):
            nx, ny = x + dx, y + dy
            if not self.in_bounds(nx, ny):
                break
            if not stackable and self.is_occupied(nx, ny, ignore_entity_id=e.id):
                break
            x, y = nx, ny
            steps += 1
        if steps == 0:
            return 0, []
        # Delegate the actual commit to move_dirs (we've already proven
        # the path legal) so we inherit per-step hook firing for free.
        log = e.move_dirs([(canon, steps)])
        return steps, log

    def pull_entity(self, eid: str, tx: int, ty: int, n: int = 1) -> Tuple[int, List[str]]:
        """Forced movement TOWARD a point: step `eid` up to `n` cells
        toward (tx, ty), stopping at the first blocked cell (off-grid,
        or — for a non-stackable mover — an occupied cell) or once the
        entity reaches the target cell. Returns (cells_actually_moved,
        hook_log).

        The point-directed twin of push_entity: where push walks a FIXED
        direction away, pull recomputes a heading toward the target each
        step, so the path curves toward (tx, ty). Each step reduces the
        Chebyshev gap by moving one cell on whichever axes still differ.
        When allow_diagonal_movement is off, a step that would be diagonal
        collapses to the dominant axis (the larger remaining delta; ties
        favor the horizontal axis), matching the cardinal-only feel of
        !ent move. The committed path goes through Entity.move_dirs, so
        per-step tile on_enter/on_exit and the final on_stop /
        on_entity_moved fire exactly as a stepwise move would — a pulled
        entity is dragged through fire tiles cell by cell.

        Raises VTTError on an unknown entity, non-int target coords, or a
        non-int `n`. (tx, ty) need NOT be in bounds — pulling toward an
        off-grid point just drags the entity to the board edge.

        Shared implementation behind the pull_entity() formula primitive
        and the `!ent pull` command."""
        e = self.entities.get(eid)
        if e is None:
            raise NotFound(f"Entity '{eid}' not found.")
        for label, val in (("tx", tx), ("ty", ty), ("n", n)):
            if not isinstance(val, int) or isinstance(val, bool):
                raise VTTError(f"pull: {label} must be an integer.")
        if n <= 0:
            return 0, []
        allow_diag = bool(self.rules.get("allow_diagonal_movement", False))
        stackable = e.is_cell_stackable
        # Walk forward to find the longest legal prefix, recomputing the
        # heading toward (tx, ty) at each cell. Intermediate occupancy
        # stops the drag (you can't be pulled THROUGH a body), mirroring
        # push_entity's "stops at the first blocker" rule.
        x, y = e.x, e.y
        moves: List[Tuple[str, int]] = []
        for _ in range(n):
            if (x, y) == (tx, ty):
                break
            sx = (tx > x) - (tx < x)   # sign of remaining x delta
            sy = (ty > y) - (ty < y)
            if sx != 0 and sy != 0 and not allow_diag:
                # Collapse the diagonal to the dominant axis; ties favor
                # horizontal so the behavior is deterministic.
                if abs(tx - x) >= abs(ty - y):
                    sy = 0
                else:
                    sx = 0
            nx, ny = x + sx, y + sy
            if not self.in_bounds(nx, ny):
                break
            if not stackable and self.is_occupied(nx, ny, ignore_entity_id=e.id):
                break
            moves.append((_VECTOR_TO_DIRECTION[(sx, sy)], 1))
            x, y = nx, ny
        if not moves:
            return 0, []
        log = e.move_dirs(moves)
        return len(moves), log

    def swap_entities(self, a: str, b: str) -> Tuple[bool, List[str]]:
        """Atomically exchange the positions of entities `a` and `b`.
        Both must exist; swapping an entity with itself (or two entities
        that already share a cell) is a no-op returning (False, []).
        Bypasses the usual occupancy check — the two target cells ARE
        occupied, by each other — but the move is atomic. Fires on_exit
        on both old cells, on_enter + on_stop on both new cells, and
        on_entity_moved for both. Returns (swapped, hook_log).

        Shared implementation behind the swap_entities() formula
        primitive and the `!ent swap` command."""
        ea = self.entities.get(a)
        eb = self.entities.get(b)
        if ea is None:
            raise NotFound(f"Entity '{a}' not found.")
        if eb is None:
            raise NotFound(f"Entity '{b}' not found.")
        if a == b:
            return False, []
        ax, ay = ea.x, ea.y
        bx, by = eb.x, eb.y
        # Degenerate co-located case (shouldn't happen for two non-
        # stackable entities, but defended): nothing moves, no hooks.
        if (ax, ay) == (bx, by):
            return False, []
        log: List[str] = []
        # Fire on_exit at CURRENT positions before any state changes, so
        # passives observe each entity still standing on its old cell.
        log += self.fire_tile_hook("on_exit", a, ax, ay)
        log += self.fire_zone_exit_hooks(a, ax, ay, bx, by)
        log += self.fire_tile_hook("on_exit", b, bx, by)
        log += self.fire_zone_exit_hooks(b, bx, by, ax, ay)
        # Atomic swap via direct move_to (bypasses is_occupied — required,
        # since A and B occupy each other's target cells until done).
        ea.move_to(bx, by)
        eb.move_to(ax, ay)
        log += self.fire_tile_hook("on_enter", a, bx, by)
        log += self.fire_tile_hook("on_stop", a, bx, by)
        # Each swap partner's move is a single, final step — enter + stop.
        log += self.fire_zone_enter_hooks(a, ax, ay, bx, by, True)
        log += self.fire_tile_hook("on_enter", b, ax, ay)
        log += self.fire_tile_hook("on_stop", b, ax, ay)
        log += self.fire_zone_enter_hooks(b, bx, by, ax, ay, True)
        log += self.fire_entity_moved(a, ax, ay, bx, by)
        log += self.fire_entity_moved(b, bx, by, ax, ay)
        return True, log

    # ------------- turns -------------

    def _rebuild_turn_order(self):
        """Sort the alive, initiative-bearing entities into `self.turn_order`
        according to the active rules. Preserves the currently-acting
        entity's identity: if they're still eligible, active_index points
        at their new position. Clears the deferred-rebuild flag.

        Five rules drive this:
          - turnorder_direction       (highest_first | lowest_first)
          - turnorder_team_grouping   (none | highest_per_team | average_per_team)
          - turnorder_teamless_position (interleaved | first | last)
          - turnorder_tiebreaker      (name | id | insertion_order | random_stable)
          - turnorder_change_policy   handled OUTSIDE this method by
                                      _mark_turn_order_dirty; once we
                                      get here we just rebuild.

        Structural rebuilds (spawn / remove / death / revive) call this
        directly because membership changes can't usefully be deferred —
        a dead entity sitting in turn_order would otherwise still take
        turns. Value-driven rebuilds (init/team writes from formulas or
        the !ent initiative command) route through _mark_turn_order_dirty
        so they respect the policy rule.
        """
        prev_current_id = None
        if 0 <= self.active_index < len(self.turn_order):
            prev_current_id = self.turn_order[self.active_index]

        candidates = [
            e for e in self.entities.values()
            if e.initiative is not None and e.is_alive
        ]

        direction = self.rules.get("turnorder_direction", "highest_first")
        grouping = self.rules.get("turnorder_team_grouping", "none")
        teamless_pos = self.rules.get("turnorder_teamless_position", "interleaved")
        tiebreaker = self.rules.get("turnorder_tiebreaker", "name")

        # Direction multiplier — Python's sort is ascending, so we negate
        # the initiative for highest_first and leave it positive for
        # lowest_first. Group scores follow the same convention.
        init_dir = -1 if direction == "highest_first" else 1

        # Snapshot insertion order once per rebuild — list(entities.keys())
        # is O(n) and we'd otherwise call it inside every tie_key.
        insertion_index = {eid: i for i, eid in enumerate(self.entities.keys())}

        def tie_key(e: "Entity") -> Tuple:
            if tiebreaker == "id":
                return (e.id,)
            if tiebreaker == "insertion_order":
                return (insertion_index.get(e.id, 1_000_000),)
            if tiebreaker == "random_stable":
                # Seeded by (match.id, round_number) so the same set of
                # ties resolves identically every call within a round
                # but reshuffles when the round bumps. Using a hash of
                # the seed + entity id keeps it deterministic without
                # needing a stored permutation.
                import hashlib
                seed = f"{self.id}:{self.round_number}:{e.id}"
                return (hashlib.sha256(seed.encode()).hexdigest(),)
            # default 'name'
            return (e.name.lower(), e.id)

        def member_key(e: "Entity") -> Tuple:
            # Members are always sorted by direction-aware individual
            # initiative first, then the configured tiebreaker.
            return (init_dir * e.initiative,) + tie_key(e)

        if grouping == "none":
            ordered = sorted(candidates, key=member_key)
            self.turn_order = [e.id for e in ordered]
        else:
            # Partition into teamed groups and teamless. team_var on
            # Entity already returns None for "no team" (absent or
            # empty value), so the partition is just a None check.
            teamed_groups: Dict[str, List["Entity"]] = {}
            teamless: List["Entity"] = []
            for e in candidates:
                team = e.team
                if team is None or team == "":
                    teamless.append(e)
                else:
                    teamed_groups.setdefault(team, []).append(e)

            def group_score(members: List["Entity"]) -> float:
                inits = [e.initiative for e in members]
                if grouping == "average_per_team":
                    return sum(inits) / len(inits)
                # highest_per_team — but "highest" depends on direction.
                # In lowest_first systems, the team's slot is its LOWEST
                # initiative (the most-eager member); the rule name's
                # spirit is "best representative", not "max value".
                return min(inits) if direction == "lowest_first" else max(inits)

            def team_group_key(team_name: str, members: List["Entity"]) -> Tuple:
                # Group ties resolve by the tiebreaker applied to the
                # team's NAME (rather than to a representative member's
                # name) — teams are first-class buckets here. For
                # tiebreaker='id' / 'insertion_order' / 'random_stable'
                # we fall back to team_name as the secondary key since
                # those tiebreakers are entity-specific.
                if tiebreaker == "id":
                    return (team_name,)
                if tiebreaker == "insertion_order":
                    earliest = min(
                        insertion_index.get(m.id, 1_000_000) for m in members
                    )
                    return (earliest, team_name)
                if tiebreaker == "random_stable":
                    import hashlib
                    seed = f"{self.id}:{self.round_number}:team:{team_name}"
                    return (hashlib.sha256(seed.encode()).hexdigest(),)
                return (team_name.lower(), team_name)

            def sort_group(members):
                return sorted(members, key=member_key)

            teamed_entries: List[Tuple[float, Tuple, List["Entity"], str]] = []
            for tname, members in teamed_groups.items():
                score = group_score(members)
                teamed_entries.append((
                    init_dir * score, team_group_key(tname, members),
                    sort_group(members), tname,
                ))

            # Sort teams by (score, tiebreak). Stable Python sort plus
            # an explicit secondary key makes ordering deterministic
                # even on score ties.
            teamed_entries.sort(key=lambda t: (t[0], t[1]))
            teamed_flat: List["Entity"] = [
                m for _, _, members, _ in teamed_entries for m in members
            ]
            teamless_sorted = sort_group(teamless)

            if teamless_pos == "first":
                ordered = teamless_sorted + teamed_flat
            elif teamless_pos == "last":
                ordered = teamed_flat + teamless_sorted
            else:
                # 'interleaved' — treat each teamless entity as a
                # one-person team. Merge them into teamed_entries with
                # a synthetic team name so the same sort produces the
                # interleaved layout in one pass.
                interleaved_entries = list(teamed_entries)
                for e in teamless:
                    interleaved_entries.append((
                        init_dir * e.initiative,
                        # Synthetic team key — use the entity's own
                        # tie_key so a teamless entity with init==team
                        # score sorts where its own tiebreaker would
                        # place it, not where its arbitrary id would.
                        tie_key(e),
                        [e],
                        f"__teamless_{e.id}",
                    ))
                interleaved_entries.sort(key=lambda t: (t[0], t[1]))
                ordered = [m for _, _, members, _ in interleaved_entries for m in members]
            self.turn_order = [e.id for e in ordered]

        if prev_current_id and prev_current_id in self.turn_order:
            self.active_index = self.turn_order.index(prev_current_id)
        else:
            self.active_index = 0

        # Whatever triggered this rebuild, we've now applied it — the
        # deferred flag is no longer meaningful.
        self._turn_order_dirty = False

    def _mark_turn_order_dirty(self, reason: str) -> None:
        """Called when a value change (turnorder_var or team_var) wants
        a rebuild. Under the 'immediate' policy (the default) this
        rebuilds inline. Under 'deferred' it just sets the dirty flag
        and queues a warning on _var_event_warnings so the GM sees
        WHICH change is being held until round end.

        Membership changes (spawn / remove / death / revive) DO NOT go
        through this — they call _rebuild_turn_order directly because
        a dead entity in the turn list would otherwise still get
        turns under the deferred policy."""
        policy = self.rules.get("turnorder_change_policy", "immediate")
        if policy == "deferred":
            if not self._turn_order_dirty:
                # First defer this round — explain the policy once so
                # the GM doesn't think the change was dropped.
                self._var_event_warnings.append(
                    f"⏳ turn order change deferred ({reason}). "
                    f"New order takes effect at round end."
                )
            else:
                # Subsequent changes during the same deferred window —
                # just note the reason; the policy was already
                # surfaced by the first warning.
                self._var_event_warnings.append(
                    f"⏳ turn order change deferred: {reason}."
                )
            self._turn_order_dirty = True
            return
        # 'immediate' (default).
        self._rebuild_turn_order()

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
            log.extend(self.fire_status_tick("round_start"))
            log.extend(self.fire_tile_time_hooks("on_round_start"))
            log.extend(self.fire_zone_time_hooks("on_round_start"))
            log.extend(self.fire_scheduled_round())
            # Autosave: round start happens after on_round_start hooks
            # so that restoring the snapshot gives the players the
            # state they would have seen as the round began (with any
            # hook side-effects already applied).
            self.history.record_round(self)
            # The opening entity may itself be skippable (e.g. starts
            # stunned) — skip forward to the first eligible one.
            eligible = self._skip_to_eligible(log)
            cur = self.turn_order[self.active_index]
            if eligible:
                log.extend(self.fire_hook("on_turn_start", target_ids=[cur]))
                log.extend(self.fire_status_tick("turn_start"))
                log.extend(self.fire_tile_time_hooks("on_turn_start"))
                log.extend(self.fire_zone_time_hooks("on_turn_start"))
                log.extend(self.fire_scheduled_turn(cur))
            else:
                log.append(
                    "⏭️ every entity is skippable; the round passes "
                    "without anyone acting."
                )
            self.history.record_turn(self)
            return (cur, log)

        # Normal transition.
        cur = self.turn_order[self.active_index]
        log.extend(self.fire_status_tick("turn_end"))
        log.extend(self.fire_tile_time_hooks("on_turn_end"))
        log.extend(self.fire_zone_time_hooks("on_turn_end"))
        log.extend(self.fire_hook("on_turn_end", target_ids=[cur]))
        self._advance_index(log)
        # Skip over any entity carrying a skip-status flag.
        eligible = self._skip_to_eligible(log)
        new_cur = self.turn_order[self.active_index]
        if eligible:
            log.extend(self.fire_hook("on_turn_start", target_ids=[new_cur]))
            log.extend(self.fire_status_tick("turn_start"))
            log.extend(self.fire_tile_time_hooks("on_turn_start"))
            log.extend(self.fire_zone_time_hooks("on_turn_start"))
            log.extend(self.fire_scheduled_turn(new_cur))
        else:
            log.append(
                "⏭️ every entity is skippable; the round passes "
                "without anyone acting."
            )
        self.history.record_turn(self)
        return (new_cur, log)

    def _advance_index(self, log: List[str]) -> None:
        """Advance active_index by one, handling round wrap: fire
        on_round_end + status_tick(round_end), flush any deferred
        turn-order rebuild, bump the round number, fire on_round_start
        + status_tick(round_start), and autosave the round. Factored
        out of next_turn so the skip-status loop can reuse the exact
        same wrap bookkeeping for each cell it steps over."""
        new_index = (self.active_index + 1) % len(self.turn_order)
        wrapped = (new_index == 0)
        if wrapped:
            log.extend(self.fire_hook("on_round_end"))
            log.extend(self.fire_status_tick("round_end"))
            log.extend(self.fire_tile_time_hooks("on_round_end"))
            log.extend(self.fire_zone_time_hooks("on_round_end"))
            # Flush any deferred turn-order rebuild between on_round_end
            # (ran against the OLD order) and on_round_start (sees the
            # NEW order). Round-wrap naturally restarts at the top.
            if self._turn_order_dirty:
                self._rebuild_turn_order()
                log.append(
                    "🔄 deferred turn-order rebuild applied at round end."
                )
                new_index = 0
        self.active_index = new_index
        if wrapped:
            self.round_number += 1
            log.extend(self.fire_hook("on_round_start"))
            log.extend(self.fire_status_tick("round_start"))
            log.extend(self.fire_tile_time_hooks("on_round_start"))
            log.extend(self.fire_zone_time_hooks("on_round_start"))
            log.extend(self.fire_scheduled_round())
            self.history.record_round(self)

    def _skipping_statuses(self, e: "Entity") -> List[str]:
        """Names of `e`'s statuses whose data dict carries
        `skips_turn: True`. Returns [] when no status causes a skip.
        Each status defines its own behavior — there is no global
        skip-list rule. The boolean key is `skips_turn`."""
        out = []
        for name, data in e.status.items():
            if isinstance(data, dict) and bool(data.get("skips_turn", False)):
                out.append(name)
        return out

    def _skip_to_eligible(self, log: List[str]) -> bool:
        """Starting from the current active_index, advance past any
        entity carrying a status whose data has `skips_turn=True`.
        Returns True if an eligible (non-skippable) entity is now
        current, or False if a full cycle found every entity skippable
        (the caller then passes the round without firing on_turn_start).
        Bounded to one full turn-order cycle so an all-skippable table
        can't loop forever."""
        n = len(self.turn_order)
        checked = 0
        while checked < n:
            cur = self.turn_order[self.active_index]
            e = self.entities.get(cur)
            if e is None:
                return True
            skipping = self._skipping_statuses(e)
            if not skipping:
                return True
            matched = ", ".join(sorted(skipping))
            log.append(f"⏭️ `{cur}`'s turn skipped ({matched}).")
            self._advance_index(log)
            checked += 1
        # Full cycle without finding an eligible entity.
        return False

    def _effective_clamp(self, entity: "Entity", path: str) -> Optional["ClampSpec"]:
        """Return the clamp that applies to `path` on this entity, or None.

        Entity-level clamps WHOLLY override system-level clamps on the same
        path (replace, not field-by-field merge). See ClampSpec docstring.
        Returns None if neither level defines a clamp for this path.
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

    def fire_tile_hook(self, when: str, entity_id: Optional[str], x: int, y: int) -> List[str]:
        """Fire the tile hook at (x, y) of the given `when`.

        Returns log lines: one info line per successful fire, one warning
        line per formula failure. Empty list when no hook is registered
        for this (tile, when) — the common case, since most tiles carry
        no hooks at all.

        Context bindings:
          - self          = the bound entity (entity_id arg). For
                            movement hooks this is the entering /
                            exiting / stopping entity. For time hooks
                            (on_round_*, on_turn_*) it's the
                            currently-acting entity if there is one,
                            or unbound (entity_id=None) otherwise — in
                            which case a formula referencing `self`
                            errors with the standard "self is unbound"
                            message.
          - this          = current_entity_id()    (often `self` but not
                            always — e.g. a future push effect would move
                            a non-current entity)
          - tile_x, tile_y = (x, y) of the firing tile
          - hook_name      = `when`

        No-ops gracefully for:
          - out-of-bounds (x, y): GM-typo guard, returns []
          - tile has no data dict at (x, y)
          - tile data exists but has no "hooks" subdict
          - "hooks" subdict has no entry for `when`
        Each layer is checked independently so a tile can carry pure
        data with no hooks and never pay the hook-evaluation cost.
        """
        if when not in TILE_HOOK_NAMES:
            return []
        if not self.in_bounds(x, y):
            return []
        data = self.tiles.get((x, y))
        if not data:
            return []
        # Resolution order: ad-hoc instance hook wins, falling back to
        # the template's hook for the same `when`. This lets the GM
        # place a template-backed tile and then locally override a
        # single hook via !tile hook add without redefining the
        # template, while leaving the other hook timings inherited.
        formula_src: Optional[str] = None
        instance_hooks = data.get("hooks")
        if isinstance(instance_hooks, dict):
            cand = instance_hooks.get(when)
            if isinstance(cand, str) and cand:
                formula_src = cand
        if formula_src is None:
            tpl_name = data.get("_template")
            if isinstance(tpl_name, str):
                tpl = self.tile_templates.get(tpl_name)
                # Dangling template references (template deleted after
                # placement) silently no-op the lookup. The deletion
                # path already warned the GM at confirm-time about the
                # orphan count; warning again on every fire would spam
                # the move-result message — one !ent move step over
                # three orphaned tiles produces six fire_tile_hook
                # calls (three on_enter + three on_exit), each of
                # which would emit a "missing template" line.
                # !tile info on the tile still shows the dangling
                # `_template` field so the GM can audit them later.
                if tpl is not None:
                    cand = tpl.hooks.get(when)
                    if isinstance(cand, str) and cand:
                        formula_src = cand
        if formula_src is None:
            return []
        # Lazy import to avoid logic <-> formula import cycle (same
        # pattern as fire_hook below).
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        extras = {
            "tile_x": x,
            "tile_y": y,
            "hook_name": when,
        }
        ctx = EvalCtx(
            this=self.current_entity_id(),
            target=entity_id or None,  # "" -> None for unbound `self`
            extras=extras,
        )
        # Log-line tag: show "for `eid`" when there's an acting entity,
        # otherwise "(no acting entity)" for time hooks during a wrap.
        tag = f"for `{entity_id}`" if entity_id else "(no acting entity)"
        try:
            engine.eval_program(formula_src, ctx)
        except FormulaError as ex:
            return [f"⚠️ tile ({x},{y}) hook `{when}` {tag} FAILED: {ex}"]
        except Exception as ex:
            # Defensive: any non-FormulaError exception is a bug, but
            # don't bring down the move on it.
            return [
                f"⚠️ tile ({x},{y}) hook `{when}` {tag} "
                f"CRASHED: {type(ex).__name__}: {ex}"
            ]
        return [f"⚙️ tile ({x},{y}) hook `{when}` fired {tag}"]

    # ---- zone hooks --------------------------------------------------
    # Zones carry two movement-hook families plus the time hooks (see
    # ZONE_HOOK_NAMES). fire_zone_hook fires ONE named zone's hook of a
    # given `when`; fire_zone_exit_hooks / fire_zone_enter_hooks do the
    # boundary-vs-per-cell dispatch around a single step (called pre- and
    # post-move respectively at every movement site, paralleling the tile
    # on_exit / on_enter+on_stop calls); fire_zone_time_hooks fires the
    # round/turn hooks across all zones.
    def fire_zone_hook(self, when: str, name: str,
                       entity_id: Optional[str], x: int, y: int) -> List[str]:
        """Fire zone `name`'s hook of `when`. Bindings: self = the bound
        entity (or None -> unbound), this = current entity, zone_name =
        `name`, tile_x/tile_y = (x,y) (the crossed/stepped cell), and
        hook_name = `when`. No-ops (returns []) for an unknown `when`, a
        missing zone, or a zone with no hook of that name."""
        if when not in ZONE_HOOK_NAMES:
            return []
        z = self.zones.get(name)
        if not z:
            return []
        hooks = z.get("hooks")
        if not isinstance(hooks, dict):
            return []
        formula_src = hooks.get(when)
        if not (isinstance(formula_src, str) and formula_src):
            return []
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        extras = {
            "zone_name": name,
            "tile_x": x,
            "tile_y": y,
            "hook_name": when,
        }
        ctx = EvalCtx(
            this=self.current_entity_id(),
            target=entity_id or None,
            extras=extras,
        )
        tag = f"for `{entity_id}`" if entity_id else "(no acting entity)"
        try:
            engine.eval_program(formula_src, ctx)
        except FormulaError as ex:
            return [f"⚠️ zone `{name}` hook `{when}` {tag} FAILED: {ex}"]
        except Exception as ex:
            return [
                f"⚠️ zone `{name}` hook `{when}` {tag} "
                f"CRASHED: {type(ex).__name__}: {ex}"
            ]
        return [f"⚙️ zone `{name}` hook `{when}` fired {tag}"]

    def fire_zone_exit_hooks(self, entity_id: Optional[str],
                             fx: int, fy: int, tx: int, ty: int) -> List[str]:
        """PRE-move zone firing for a step (fx,fy) -> (tx,ty). Fires the
        per-cell on_cell_exit for every zone containing the FROM cell, and
        the boundary on_exit for every zone the entity is LEAVING (FROM
        cell in the zone, TO cell not). Moving within a zone fires only
        on_cell_exit, not on_exit. Zone-name sets are snapshotted before
        firing so a hook that reshapes zones can't disturb the iteration."""
        from_zones = self.zones_at(fx, fy)
        if not from_zones:
            return []
        to_zones = self.zones_at(tx, ty)
        log: List[str] = []
        for name in sorted(from_zones):
            # Boundary exit first (you cross the edge), then per-cell.
            if name not in to_zones:
                log.extend(self.fire_zone_hook("on_exit", name, entity_id, fx, fy))
            log.extend(self.fire_zone_hook("on_cell_exit", name, entity_id, fx, fy))
        return log

    def fire_zone_enter_hooks(self, entity_id: Optional[str],
                              fx: int, fy: int, tx: int, ty: int,
                              is_final: bool) -> List[str]:
        """POST-move zone firing for a step (fx,fy) -> (tx,ty). Fires the
        boundary on_enter for every zone the entity is ENTERING (TO cell
        in the zone, FROM cell not) and the per-cell on_cell_enter for
        every zone containing the TO cell. When is_final (the step that
        ends the whole move), also fires the boundary on_stop and per-cell
        on_cell_stop for every zone containing the final cell."""
        to_zones = self.zones_at(tx, ty)
        if not to_zones:
            return []
        from_zones = self.zones_at(fx, fy)
        log: List[str] = []
        for name in sorted(to_zones):
            if name not in from_zones:
                log.extend(self.fire_zone_hook("on_enter", name, entity_id, tx, ty))
            log.extend(self.fire_zone_hook("on_cell_enter", name, entity_id, tx, ty))
        if is_final:
            log.extend(self.fire_zone_stop_hooks(entity_id, tx, ty))
        return log

    def fire_zone_stop_hooks(self, entity_id: Optional[str],
                             x: int, y: int) -> List[str]:
        """Fire boundary on_stop + per-cell on_cell_stop for every zone
        containing (x,y). Split out from fire_zone_enter_hooks so a
        stepwise mover can fire the stop ONCE at its final cell (after the
        per-step enter loop), mirroring how tile on_stop fires once after
        the move_dirs loop."""
        log: List[str] = []
        for name in sorted(self.zones_at(x, y)):
            log.extend(self.fire_zone_hook("on_stop", name, entity_id, x, y))
            log.extend(self.fire_zone_hook("on_cell_stop", name, entity_id, x, y))
        return log

    def fire_zone_time_hooks(self, when: str) -> List[str]:
        """Fire zone time hooks (on_round_start/end, on_turn_start/end) on
        every zone carrying a hook of that name. Iterates a snapshot of
        zone names sorted for determinism, skipping zones deleted
        mid-iteration. self binds to the currently-acting entity (if any);
        zone_name to the zone; tile_x/tile_y are unbound (None) for time
        hooks — there's no single cell, so a time hook that needs cells
        should loop zone_cells(zone_name)."""
        if when not in ZONE_HOOK_NAMES:
            return []
        cur = self.current_entity_id()
        log: List[str] = []
        for name in sorted(self.zones.keys()):
            if name not in self.zones:
                continue
            # tile_x/tile_y default to None for time hooks (no single
            # firing cell); fire_zone_hook still binds zone_name.
            z = self.zones.get(name)
            hooks = z.get("hooks") if z else None
            if not (isinstance(hooks, dict) and hooks.get(when)):
                continue
            log.extend(self._fire_zone_time_hook(when, name, cur))
        return log

    def _fire_zone_time_hook(self, when: str, name: str,
                             entity_id: Optional[str]) -> List[str]:
        """fire_zone_hook variant for time hooks: binds tile_x/tile_y to
        None (no single firing cell) rather than a real coordinate."""
        from formula import FormulaEngine, EvalCtx, FormulaError
        z = self.zones.get(name)
        if not z:
            return []
        formula_src = (z.get("hooks") or {}).get(when)
        if not (isinstance(formula_src, str) and formula_src):
            return []
        engine = FormulaEngine(self)
        ctx = EvalCtx(
            this=self.current_entity_id(),
            target=entity_id or None,
            extras={"zone_name": name, "tile_x": None, "tile_y": None,
                    "hook_name": when},
        )
        tag = f"for `{entity_id}`" if entity_id else "(no acting entity)"
        try:
            engine.eval_program(formula_src, ctx)
        except FormulaError as ex:
            return [f"⚠️ zone `{name}` hook `{when}` {tag} FAILED: {ex}"]
        except Exception as ex:
            return [
                f"⚠️ zone `{name}` hook `{when}` {tag} "
                f"CRASHED: {type(ex).__name__}: {ex}"
            ]
        return [f"⚙️ zone `{name}` hook `{when}` fired {tag}"]

    def fire_status_tick(self, when: str) -> List[str]:
        """Run status_tick_formula once for every status on every
        target entity, when the active status_tick_when rule equals
        `when`. Returns accumulated log lines (one per fire, one
        ⚠️ per formula failure). No-op when the rule disabled or the
        formula is empty.

        `when` should be one of "turn_start", "turn_end", "round_start",
        "round_end" — match what next_turn calls with.

        Targeting:
          turn_start / turn_end : the entity whose turn is starting /
                                  ending (active_index at call time)
          round_start / round_end: every entity in turn_order

        Per (entity, status) the formula runs with:
          self        = the bearing entity
          this        = current_entity_id()
          status_name = the status (a string)

        The per-entity status name list is SNAPSHOT before iterating, so
        a status_remove call inside the formula safely removes the
        current status (or any other) without breaking iteration. A
        formula error on one status logs a ⚠️ line and the next status
        still ticks.
        """
        configured = self.rules.get("status_tick_when", "never")
        if configured != when:
            return []
        src = self.rules.get("status_tick_formula", "")
        if not isinstance(src, str) or not src.strip():
            return []
        # Pick targets to iterate.
        if when in ("turn_start", "turn_end"):
            if not self.turn_order:
                return []
            cur = self.turn_order[self.active_index] if (
                0 <= self.active_index < len(self.turn_order)
            ) else None
            if cur is None:
                return []
            targets = [cur]
        else:  # round_start / round_end
            targets = list(self.turn_order)

        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        log: List[str] = []
        for eid in targets:
            e = self.entities.get(eid)
            if e is None:
                continue
            # Snapshot status names so status_remove during the tick
            # doesn't break iteration.
            for sname in list(e.status.keys()):
                # Re-check existence in case a previous tick on this
                # entity removed `sname`.
                if sname not in e.status:
                    continue
                ctx = EvalCtx(
                    this=this_id, target=eid,
                    extras={"status_name": sname},
                )
                try:
                    engine.eval_program(src, ctx)
                except FormulaError as ex:
                    log.append(
                        f"⚠️ status_tick on `{eid}.{sname}` "
                        f"({when}) FAILED: {ex}"
                    )
                except Exception as ex:
                    log.append(
                        f"⚠️ status_tick on `{eid}.{sname}` "
                        f"({when}) CRASHED: "
                        f"{type(ex).__name__}: {ex}"
                    )
        return log

    def fire_status_event(
        self, when: str, entity_id: str, status_name: str,
        *, old_value: Optional[Dict[str, Any]] = None,
        new_value: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Fire passives matching `when` for a status lifecycle event.

        `when` is one of "on_status_added" / "on_status_removed" /
        "on_status_changed". Context bindings: status_name + old_value
        / new_value as documented in HOOK_NAMES. Routes through the
        same fire_hook pipeline (global passives first, then the
        entity's own), so target/scope filtering works the same way
        var hooks do.

        old_value / new_value semantics:
          on_status_added   — old None, new = the data dict (usually
                              {} since add starts with empty data)
          on_status_removed — old = the prior dict, new None
          on_status_changed — both = the dict (whole status subtree)
                              before and after the field change
        Snapshots are shallow copies so a passive that mutates
        new_value doesn't bleed into the live status data.
        """
        if when not in HOOK_NAMES:
            return []
        if when not in ("on_status_added", "on_status_removed",
                        "on_status_changed"):
            return []
        e = self.entities.get(entity_id)
        if e is None:
            return []
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        extras = {
            "status_name": status_name,
            "old_value": dict(old_value) if isinstance(old_value, dict) else old_value,
            "new_value": dict(new_value) if isinstance(new_value, dict) else new_value,
            "hook_name": when,
        }
        ctx = EvalCtx(this=this_id, target=entity_id, extras=extras)
        log: List[str] = []
        for p in self.global_passives.values():
            if p.when == when:
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == when:
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=False,
                ))
        return log

    def _emit_status_diff(
        self, entity_id: str, status_name: str,
        before: Any, after: Any,
    ) -> List[str]:
        """Compare a status's data before vs after a mutation and fire
        the right lifecycle event (or none if data is unchanged).

        before / after are the data dicts at status[name] (None if the
        status wasn't / isn't present). Used by every mutation site
        (both the !ent status command and the status_* formula
        functions) so event-firing is consistent and the call sites
        don't repeat the dispatch logic.

        before  after  -> event
          None  {...}      on_status_added
          {...} None       on_status_removed
          {...} {...}      on_status_changed iff before != after
        """
        log: List[str] = []
        if before is None and after is not None:
            self.log_event("status_added", entity=entity_id,
                           name=self._entity_name(entity_id),
                           status=status_name)
            log = self.fire_status_event(
                "on_status_added", entity_id, status_name,
                old_value=None, new_value=after,
            )
        elif before is not None and after is None:
            self.log_event("status_removed", entity=entity_id,
                           name=self._entity_name(entity_id),
                           status=status_name)
            log = self.fire_status_event(
                "on_status_removed", entity_id, status_name,
                old_value=before, new_value=None,
            )
        elif before != after:
            log = self.fire_status_event(
                "on_status_changed", entity_id, status_name,
                old_value=before, new_value=after,
            )
        # Death-check chokepoint for status mutations. Death conditions
        # can reference statuses (status_has / status_get) — without
        # this, adding a 'doomed' status wouldn't trigger the death
        # condition the way an hp write would. Gated by
        # _death_processing (set by _process_death) to prevent
        # re-entry from on_death passives that themselves mutate
        # status. Covers add / remove / changed via the single
        # chokepoint here.
        if entity_id in self.entities:
            log.extend(self.check_death(entity_id))
        return log

    def fire_entity_moved(
        self, entity_id: str,
        from_x: int, from_y: int, to_x: int, to_y: int,
    ) -> List[str]:
        """Fire on_entity_moved passives for an entity that just changed
        position. Context bindings: from_x / from_y / to_x / to_y. The
        entity-side complement of tile on_enter / on_exit hooks — a
        passive can react to "any movement" without caring which tile."""
        e = self.entities.get(entity_id)
        if e is None:
            return []
        from formula import FormulaEngine, EvalCtx
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        extras = {
            "from_x": from_x, "from_y": from_y,
            "to_x": to_x, "to_y": to_y,
            "hook_name": "on_entity_moved",
        }
        ctx = EvalCtx(this=this_id, target=entity_id, extras=extras)
        self.log_event("move", entity=entity_id, name=e.name,
                       from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y)
        log: List[str] = []
        for p in self.global_passives.values():
            if p.when == "on_entity_moved":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == "on_entity_moved":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=False,
                ))
        return log

    def fire_entity_step(
        self, entity_id: str,
        from_x: int, from_y: int, to_x: int, to_y: int,
    ) -> List[str]:
        """Fire on_entity_step passives for the entity that just stepped
        from (from_x, from_y) to (to_x, to_y) by ONE cell. Called per
        step by Entity.move_dirs, AFTER the tile on_enter for that step
        — so passives observe the post-step entity position AND see
        whatever the tile's on_enter already applied. Same binding
        shape as fire_entity_moved (from_x/from_y/to_x/to_y), making
        the two hooks drop-in interchangeable in a passive body."""
        e = self.entities.get(entity_id)
        if e is None:
            return []
        from formula import FormulaEngine, EvalCtx
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        extras = {
            "from_x": from_x, "from_y": from_y,
            "to_x": to_x, "to_y": to_y,
            "hook_name": "on_entity_step",
        }
        ctx = EvalCtx(this=this_id, target=entity_id, extras=extras)
        log: List[str] = []
        for p in self.global_passives.values():
            if p.when == "on_entity_step":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == "on_entity_step":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=entity_id, is_global=False,
                ))
        return log

    def fire_action_used(
        self, *,
        actor_id: str,
        action_name: str,
        action_path: str,
        target: Any,
        args: Dict[str, Any],
    ) -> List[str]:
        """Fire on_action_used passives after an action's body has
        completed successfully. Bindings (per the hook contract in
        HOOK_NAMES): action_name, action_path, target, args. `self`
        binds to the ACTOR — the entity that used the action.

        Failed/rolled-back actions do NOT call this — the action runner
        only invokes fire_action_used on the success path."""
        e = self.entities.get(actor_id)
        if e is None:
            return []
        from formula import FormulaEngine, EvalCtx
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        extras = {
            "action_name": action_name,
            "action_path": action_path,
            "target": target,
            "args": args,
            "actor": actor_id,
            "hook_name": "on_action_used",
        }
        ctx = EvalCtx(this=this_id, target=actor_id, extras=extras)
        log: List[str] = []
        for p in self.global_passives.values():
            if p.when == "on_action_used":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=actor_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == "on_action_used":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=actor_id, is_global=False,
                ))
        return log

    def fire_action_used_on_target(
        self, *,
        target_id: str,
        actor_id: str,
        action_name: str,
        action_path: str,
        args: Dict[str, Any],
    ) -> List[str]:
        """Fire on_action_used_on_target passives on a SPECIFIC target
        entity. Called once per entity by the action runner (for
        target=entity, once total; for target=entity_list, once per
        eid). `self` binds to the TARGET; `actor` to the entity that
        used the action; `target` shadows to the same eid as self
        (it's the resolved-for-this-fire target, not the original
        list). action_name / action_path / args match on_action_used."""
        e = self.entities.get(target_id)
        if e is None:
            return []
        from formula import FormulaEngine, EvalCtx
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        extras = {
            "action_name": action_name,
            "action_path": action_path,
            # Shadow `target` to the per-fire eid so a passive reading
            # `entity[target].hp` (or comparing `target == self`) sees
            # this specific defender, not the whole entity_list.
            "target": target_id,
            "args": args,
            "actor": actor_id,
            "hook_name": "on_action_used_on_target",
        }
        ctx = EvalCtx(this=this_id, target=target_id, extras=extras)
        log: List[str] = []
        for p in self.global_passives.values():
            if p.when == "on_action_used_on_target":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=target_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == "on_action_used_on_target":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=target_id, is_global=False,
                ))
        return log

    def fire_action_failed(
        self, *,
        actor_id: str,
        action_name: str,
        action_path: str,
        target: Any,
        args: Dict[str, Any],
        fail_reason: str,
        fail_message: str,
    ) -> List[str]:
        """Fire on_action_failed passives on the ACTOR after a clean
        fail() abort. The runner has already rolled back to
        pre-state by the time this fires, so passives see the
        unchanged world (not the partial in-flight state). Engine
        faults (recursion limit, internal invariants) deliberately
        skip this hook — those are bugs, not GM-controlled outcomes.

        Bindings: action_name, action_path, target, args, actor,
        fail_reason (GM-supplied tag or ""), fail_message."""
        e = self.entities.get(actor_id)
        if e is None:
            return []
        from formula import FormulaEngine, EvalCtx
        engine = FormulaEngine(self)
        this_id = self.current_entity_id()
        extras = {
            "action_name": action_name,
            "action_path": action_path,
            "target": target,
            "args": args,
            "actor": actor_id,
            "fail_reason": fail_reason,
            "fail_message": fail_message,
            "hook_name": "on_action_failed",
        }
        ctx = EvalCtx(this=this_id, target=actor_id, extras=extras)
        log: List[str] = []
        for p in self.global_passives.values():
            if p.when == "on_action_failed":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=actor_id, is_global=True,
                ))
        for p in e.passives.values():
            if p.when == "on_action_failed":
                log.append(_run_passive_safely(
                    engine, p, ctx, target_id=actor_id, is_global=False,
                ))
        return log

    # ---- summon / entity-template helpers --------------------------------
    # A "template" is just an entity-shaped dict (Entity.to_dict output,
    # optionally with id/x/y omitted). It lives in ordinary storage —
    # an entity's vars, a tile's data, wherever the GM put it. There is
    # NO first-class template type; summon_entity turns any such dict
    # into a live entity. This is the "what X does is stored in X"
    # philosophy: a summoner stores its minion as data, and the summon
    # primitive is the only engine-side machinery.

    @staticmethod
    def entity_template_dict(e: "Entity") -> Dict[str, Any]:
        """Snapshot a live entity into a summon-ready template dict.
        Strips id and position — those are assigned at summon time, so
        a template never carries a stale id (which would collide) or a
        fixed cell. Everything else (name, vars, status, passives,
        clamps, facing) carries over."""
        d = e.to_dict()
        d.pop("id", None)
        d.pop("x", None)
        d.pop("y", None)
        return d

    def _taken_entity_ids(self) -> "set[str]":
        """Return the set of entity ids that should be considered
        already-in-use for collision purposes. Always includes live
        entity ids; ALSO includes corpse ids when the
        `corpse_id_uniqueness` rule is true (default). This is the
        single source of truth for "is this id taken?" — used by
        mint_entity_id and Entity.spawn so the two stay consistent."""
        taken: set = set(self.entities.keys())
        if bool(self.rules.get("corpse_id_uniqueness", True)):
            for (_x, _y, eid, _c) in self.all_corpses():
                taken.add(eid)
        return taken

    def mint_entity_id(self, prefix: str) -> str:
        """Return an id not currently in use, derived from `prefix`.
        `prefix` itself if free, else prefix2, prefix3, ... Honors the
        `corpse_id_uniqueness` rule: when true (default) the scan
        considers corpse ids too, so a summoned skeleton becomes
        `skeleton2` if a `skeleton` corpse is already lying around.
        Prefix is sanitized to a reasonable id token (whitespace/dots
        stripped) and lower-cased; falls back to 'summon' if empty."""
        base = "".join(
            ch for ch in str(prefix).strip().lower().replace(" ", "_")
            if ch.isalnum() or ch in ("_", "-")
        ) or "summon"
        taken = self._taken_entity_ids()
        if base not in taken:
            return base
        n = 2
        while f"{base}{n}" in taken:
            n += 1
        return f"{base}{n}"

    def summon_entity(
        self, template: Any, x: int, y: int, *,
        id_prefix: Optional[str] = None,
        near_radius: Optional[int] = None,
    ) -> Tuple[str, List[str]]:
        """Instantiate a live entity from a template dict and place it.

        template     an entity-shaped dict (Entity.to_dict output, with
                     id/x/y optional — they're overridden here).
        x, y         the desired cell.
        id_prefix    base for the minted id; defaults to the template's
                     `name`, then its `id`, then "summon".
        near_radius  None = STRICT placement (raises OutOfBounds /
                     Occupied if (x,y) isn't free). A non-negative int
                     = ring-search outward (Chebyshev) up to that
                     radius for the first free in-bounds cell, raising
                     only if none is found.

        Returns (new_entity_id, log_lines) — log_lines carries any
        on_entity_spawned passive output, same as Entity.spawn. The
        summoned entity joins turn order by its initiative.

        Guarded by the summon_event_limit rule: the per-command summon
        counter (_summon_count) is checked and incremented here, so a
        runaway summon chain raises instead of hanging."""
        if not isinstance(template, dict):
            raise VTTError(
                f"summon: template must be an entity dict, got "
                f"{type(template).__name__}."
            )
        limit = int(self.rules.get("summon_event_limit", 50))
        if self._summon_count >= limit:
            raise VTTError(
                f"summon_event_limit reached ({limit}) — too many "
                f"entities summoned in one command. Likely a runaway "
                f"summon loop; raise the rule if this is intentional."
            )

        # Resolve placement.
        if not isinstance(x, int) or isinstance(x, bool) \
                or not isinstance(y, int) or isinstance(y, bool):
            raise VTTError("summon: x and y must be integers.")
        place_x, place_y = x, y
        # If the template marks the summoned entity as cell-stackable,
        # placement bypasses occupancy entirely — symmetric with how
        # Entity.tp / move_dirs handle stackable movers. We probe the
        # template's vars directly rather than constructing the Entity
        # twice; the eventual Entity.spawn below will validate again
        # (and also bypass for stackable, via the same flag).
        tpl_vars = template.get("vars") if isinstance(template.get("vars"), dict) else {}
        stackable_template = bool(tpl_vars.get("__cell_stackable", False))
        if near_radius is not None:
            place = self._find_free_cell_near(x, y, int(near_radius))
            if place is None:
                raise Occupied(
                    f"summon_near: no free in-bounds cell within "
                    f"radius {near_radius} of ({x},{y})."
                )
            place_x, place_y = place
        else:
            if not self.in_bounds(place_x, place_y):
                raise OutOfBounds(
                    f"summon: ({place_x},{place_y}) is off-grid "
                    f"({self.grid_width}x{self.grid_height})."
                )
            if not stackable_template and self.is_occupied(place_x, place_y):
                raise Occupied(
                    f"summon: cell ({place_x},{place_y}) is occupied. "
                    f"Use summon_near to search for a free cell."
                )

        # Build the concrete entity dict: copy the template, then
        # override id/x/y. We deep-copy so the summoned entity doesn't
        # alias the stored template's nested dicts (mutating the minion
        # later must not edit the stored blueprint).
        prefix = id_prefix or template.get("name") or template.get("id") or "summon"
        new_id = self.mint_entity_id(prefix)
        d = copy.deepcopy(template)
        d["id"] = new_id
        d["x"] = place_x
        d["y"] = place_y
        e = Entity.from_dict(d)
        # Entity.spawn validates bounds/occupancy again (cheap) and
        # fires on_entity_spawned. We pre-validated for a clean error
        # message; spawn's checks are the authoritative gate.
        self._summon_count += 1
        _, log = e.spawn(self, place_x, place_y)
        return new_id, log

    def _find_free_cell_near(
        self, x: int, y: int, radius: int,
    ) -> Optional[Tuple[int, int]]:
        """Ring-search outward from (x,y) for the first free in-bounds
        cell, distance 0..radius (Chebyshev). Returns (cx,cy) or None.
        Deterministic order: rings nearest-first, and within a ring by
        (dy, dx) so summon_near placement is reproducible."""
        for r in range(0, max(0, radius) + 1):
            if r == 0:
                candidates = [(x, y)]
            else:
                candidates = []
                for dy in range(-r, r + 1):
                    for dx in range(-r, r + 1):
                        # Only the ring shell at exactly Chebyshev r.
                        if max(abs(dx), abs(dy)) == r:
                            candidates.append((x + dx, y + dy))
            for cx, cy in candidates:
                if self.in_bounds(cx, cy) and not self.is_occupied(cx, cy):
                    return (cx, cy)
        return None

    # ---- death / corpse machinery ----------------------------------------
    # A "corpse" is an entry under `tile[(x,y)].corpses.<eid>` carrying
    # the dead entity's full Entity.to_dict (so revive can spawn it back
    # exactly). The corpse's authoritative position is the TILE
    # coordinate, never the embedded entity dict's `x`/`y` (those are
    # snapshotted at death and not read by engine code afterward — this
    # is the desync prevention the user called out). Multi-corpse-per-
    # cell is supported via the id-keyed map; a second death on the same
    # cell adds a new key, not an overwrite.

    def _effective_death_condition(self, e: "Entity") -> Tuple[str, str]:
        """Resolve the (condition_formula, mode) pair to evaluate for
        entity `e`. Per-entity vars `__death_condition` / `__death_mode`
        override the system rules; the mode (`additive` / `replace`)
        decides whether the per-entity condition combines with or
        wholly replaces the system one."""
        sys_cond = str(self.rules.get("death_condition", "entity[self].hp <= 0"))
        sys_mode = str(self.rules.get("death_condition_mode", "additive"))
        ent_cond_raw = e.vars.get("__death_condition")
        ent_mode_raw = e.vars.get("__death_mode")
        ent_cond = str(ent_cond_raw) if isinstance(ent_cond_raw, str) and ent_cond_raw.strip() else None
        ent_mode = str(ent_mode_raw) if isinstance(ent_mode_raw, str) else None
        mode = ent_mode if ent_mode in ("additive", "replace") else sys_mode
        if ent_cond is None:
            return sys_cond, mode
        if mode == "replace":
            return ent_cond, mode
        # additive: OR the two together with parens so operator
        # precedence stays predictable regardless of either body.
        return f"({sys_cond}) or ({ent_cond})", mode

    def _effective_death_result(self, e: "Entity") -> str:
        """Resolve `corpse` vs `delete` for entity `e`. Per-entity var
        `__death_result` overrides the system rule; falls back to the
        rule's default on any malformed value."""
        ent = e.vars.get("__death_result")
        if isinstance(ent, str) and ent in ("corpse", "delete"):
            return ent
        sys_res = str(self.rules.get("death_result", "corpse"))
        return sys_res if sys_res in ("corpse", "delete") else "corpse"

    def check_death(self, entity_id: str) -> List[str]:
        """Evaluate the effective death condition on `entity_id`. If it
        returns truthy, run the death pipeline (fire on_death, then
        store-corpse-or-delete, remove from turn order). Returns log
        lines produced by passives that fired.

        Re-entry guarded via _death_processing — a death-firing passive
        that itself writes vars must not retrigger the check on the
        already-dying entity. Returns empty list on guard hit. A specific
        entity can also be shielded via _death_check_suppressed_ids (used
        by revive to defer the revived entity's check until its post-
        revive state settles)."""
        if self._death_processing > 0:
            return []
        if entity_id in self._death_check_suppressed_ids:
            return []
        e = self.entities.get(entity_id)
        if e is None:
            return []
        condition, _mode = self._effective_death_condition(e)
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        ctx = EvalCtx(this=self.current_entity_id(), target=entity_id)
        try:
            verdict = engine.eval_expression(condition, ctx)
        except FormulaError:
            # A malformed death condition is the GM's bug, not the
            # entity's; surfacing it as "the entity instantly dies"
            # would be terrible UX. Silently treat as "not dying" so
            # gameplay continues; the typo will surface elsewhere.
            return []
        if not verdict:
            return []
        return self._process_death(entity_id)

    def _process_death(self, entity_id: str) -> List[str]:
        """Run the death pipeline for `entity_id`: fire on_death (entity
        still in match), apply the death_result (corpse-store or
        delete), remove from turn order. Returns log lines."""
        e = self.entities.get(entity_id)
        if e is None:
            return []
        self._death_processing += 1
        # Capture identity/position now — after e.remove() the entity is
        # detached, so the log event must read these up front.
        dead_name, dead_x, dead_y = e.name, e.x, e.y
        try:
            log = self.fire_hook("on_death", target_ids=[entity_id])
            # Re-check entity still exists — a paranoid on_death passive
            # could have already removed the entity, in which case the
            # corpse/delete step has nothing to do.
            if entity_id not in self.entities:
                self.log_event("death", entity=entity_id, name=dead_name,
                               x=dead_x, y=dead_y)
                return log
            result = self._effective_death_result(e)
            if result == "corpse":
                self._store_corpse(e)
            # Remove from match (entity.remove handles turn-order
            # bookkeeping + group scrubbing) regardless of result.
            e.remove()
            self.log_event("death", entity=entity_id, name=dead_name,
                           x=dead_x, y=dead_y)
        finally:
            self._death_processing = max(0, self._death_processing - 1)
        return log

    def kill_entity(self, eid: str) -> Tuple[bool, List[str]]:
        """Unconditionally kill `eid`, bypassing the death CONDITION so
        it works no matter what the GM configured. Two-step pipeline:
          1. Run the `default_kill_function_effects` formula on the
             target (default `entity[self].hp = 0`) so the corpse reads
             naturally and any GM-configured pre-death effects fire.
             Skipped when the rule is empty.
          2. Run _process_death (fires on_death, stores the corpse or
             deletes per death_result, rebuilds turn order).
        Returns (killed, log). `killed` is True iff the entity is gone
        afterward; False if there was no such entity to begin with.

        Shared implementation behind the kill() formula primitive and
        the `!ent kill` command."""
        if eid not in self.entities:
            return False, []
        effects = str(self.rules.get("default_kill_function_effects", "")).strip()
        if effects:
            from formula import FormulaEngine, EvalCtx, FormulaError
            engine = FormulaEngine(self)
            k_ctx = EvalCtx(this=self.current_entity_id(), target=eid)
            try:
                engine.eval_program(effects, k_ctx)
            except FormulaError:
                # A malformed effects formula is the GM's bug; the
                # unconditional death below still fires, so kill stays
                # useful. Swallow rather than turn every kill into a
                # hard failure across the whole match.
                pass
        # The effects formula (or a side-effect passive) may have already
        # tripped natural death and removed the entity; only run the
        # pipeline if it's still present.
        log: List[str] = []
        if eid in self.entities:
            log = self._process_death(eid)
        return eid not in self.entities, log

    def _store_corpse(self, e: "Entity") -> None:
        """Snapshot `e` into a corpse entry under tile (e.x, e.y) at
        `corpses.<eid>`. Stores the full Entity.to_dict (including id,
        x, y) so revive can spawn it back exactly. Multi-corpse-per-
        cell: existing corpses on the cell aren't disturbed."""
        cell = self.tile_data(e.x, e.y)
        corpses = cell.setdefault("corpses", {})
        if not isinstance(corpses, dict):
            # Tile data shape was hand-written into something else;
            # bulldoze with a fresh dict (corpses is engine-owned).
            corpses = {}
            cell["corpses"] = corpses
        corpses[e.id] = {
            "entity": e.to_dict(),
            "died_round": self.round_number,
        }

    def find_corpse(self, eid: str) -> Optional[Tuple[int, int, Dict[str, Any]]]:
        """Locate a corpse by its dead-entity id. Returns
        (x, y, corpse_dict) or None. The tile coords are the
        AUTHORITATIVE position; the embedded entity dict's x/y are
        the snapshot at death and may differ if a (rare) GM hack
        moved corpses around manually — engine code never reads them
        for placement, this method always returns the tile's coords."""
        for (x, y), cell in self.tiles.items():
            corpses = cell.get("corpses") if isinstance(cell, dict) else None
            if isinstance(corpses, dict) and eid in corpses:
                return x, y, corpses[eid]
        return None

    def all_corpses(self) -> List[Tuple[int, int, str, Dict[str, Any]]]:
        """List every corpse in the match as
        (x, y, eid, corpse_dict). Sorted by (x, y, eid) for stable
        iteration; the order matches what `!list`'s Dead: section
        renders."""
        out: List[Tuple[int, int, str, Dict[str, Any]]] = []
        for (x, y) in sorted(self.tiles.keys()):
            cell = self.tiles[(x, y)]
            corpses = cell.get("corpses") if isinstance(cell, dict) else None
            if isinstance(corpses, dict):
                for eid in sorted(corpses.keys()):
                    out.append((x, y, eid, corpses[eid]))
        return out

    def revive_corpse(self, eid: str) -> Tuple[str, List[str]]:
        """Resurrect the corpse keyed by `eid`: spawn an entity from
        the stored dict at the corpse's TILE position, remove the
        corpse, run the `default_revive_function_effects` formula on
        the freshly-spawned entity (default: hp -> max_hp; entirely
        configurable via the gamerule), then fire on_revive.

        Returns (entity_id, log_lines). Raises NotFound / Occupied /
        OutOfBounds on failure (the caller decides whether to wrap
        as fail() in an action, etc.).

        The hp restoration policy lives in the
        default_revive_function_effects rule, NOT here — empty rule
        means "revive at whatever the corpse stored" (typically the
        negative death hp), and a GM can swap in any formula
        (`entity[self].hp = 1` for the "revive at 1 hp" rule,
        `status_add(self, 'weakened')` for a tax on resurrection,
        etc.). Per-revive customization beyond the rule belongs in
        an on_revive passive."""
        loc = self.find_corpse(eid)
        if loc is None:
            raise NotFound(f"No corpse with id '{eid}'.")
        x, y, corpse = loc
        entity_dict = copy.deepcopy(corpse.get("entity") or {})
        if not entity_dict:
            raise VTTError(f"Corpse '{eid}' has no entity data to revive.")
        # Stamp authoritative coords from the tile location, not the
        # embedded snapshot — desync defense.
        entity_dict["x"] = x
        entity_dict["y"] = y
        e = Entity.from_dict(entity_dict)
        # Remove corpse FIRST so spawn doesn't see its own cell as
        # already-corpse-occupied for any future invariants and so the
        # tile's `corpses` key is gone if it becomes empty.
        cell = self.tiles.get((x, y), {})
        corpses = cell.get("corpses") if isinstance(cell, dict) else None
        if isinstance(corpses, dict) and eid in corpses:
            del corpses[eid]
            if not corpses:
                cell.pop("corpses", None)
            # Drop an empty tile dict entirely so tile listings stay clean.
            if isinstance(cell, dict) and not cell:
                self.tiles.pop((x, y), None)
        # Suppress death checks FOR THIS ENTITY across the spawn +
        # revive-effects window. The corpse snapshot carries the entity's
        # DEATH-state vars (typically hp<=0), so the entity momentarily
        # satisfies its own death condition while being respawned.
        # Without this guard any var write during spawn — an
        # on_entity_spawned passive, an injected default passive, the
        # max_hp auto-fill — would trip the death check and re-kill the
        # entity before the revive policy (default_revive_function_effects,
        # e.g. hp -> max_hp) runs. We scope the shield to e.id (NOT the
        # match-global _death_processing counter) so writes in this window
        # that legitimately kill OTHER entities still register. The
        # condition is re-evaluated against e's settled state right after
        # the window (below).
        self._death_check_suppressed_ids.add(e.id)
        try:
            _, spawn_log = e.spawn(self, x, y)
            # Run the revive-effects formula on the freshly-spawned entity
            # BEFORE on_revive so on_revive observers see the post-effect
            # state (matching on_death's "see settled state" contract).
            # Skipped silently when the rule is empty.
            effects = str(self.rules.get("default_revive_function_effects", "")).strip()
            if effects:
                from formula import FormulaEngine, EvalCtx, FormulaError
                engine = FormulaEngine(self)
                r_ctx = EvalCtx(this=self.current_entity_id(), target=e.id)
                try:
                    engine.eval_program(effects, r_ctx)
                except FormulaError:
                    # GM bug; don't make every revive into a hard error.
                    pass
        finally:
            self._death_check_suppressed_ids.discard(e.id)
        # on_revive fires with death checks active again — by now the
        # revive policy has applied, so a healthy entity won't re-die.
        revive_log = self.fire_hook("on_revive", target_ids=[e.id])
        # Evaluate the death condition ONCE against the fully-settled
        # post-revive state. The suppression above only deferred the
        # check past spawn + revive-effects + on_revive so a transient
        # death-state (corpse hp, an on_entity_spawned write) couldn't
        # re-kill the entity before the revive policy and on_revive had
        # their say. The invariant still holds afterward: if the entity
        # STILL meets its death condition here — e.g.
        # default_revive_function_effects is empty so the corpse's hp<=0
        # carried over, and no on_revive passive healed it — it dies
        # again, exactly as it did pre-suppression (and as the
        # default_revive_function_effects docs describe). A revived,
        # healed entity passes this check as a no-op.
        if e.id in self.entities:
            self.log_event("revive", entity=e.id, name=e.name)
        death_log: List[str] = []
        if e.id in self.entities:
            death_log = self.check_death(e.id)
        return e.id, spawn_log + revive_log + death_log

    def fire_tile_time_hooks(self, when: str) -> List[str]:
        """Fire tile time hooks (on_round_start/end, on_turn_start/end)
        on every tile that has a hook of that name. Iterates a snapshot
        of tile coords so a hook that calls tile_clear/tile_del during
        firing doesn't break the loop. Sorted by (x, y) for deterministic
        order across rounds. Each fire has self bound to whatever entity
        is currently acting (if any); tile_x/tile_y bind to the tile's
        coords as usual.

        Tile time hooks are an alternative to entity-attached passives
        for terrain effects: 'fire spreads at round end', 'trap rearms
        at round start', a status-like-effect that's tied to the tile
        rather than to a creature.
        """
        if when not in TILE_HOOK_NAMES:
            return []
        cur = self.current_entity_id()
        coords = sorted(self.tiles.keys())
        log: List[str] = []
        for (x, y) in coords:
            # Skip tiles cleared mid-iteration (e.g. by a previous tile's
            # hook calling tile_clear on this one). Also skip if no hook
            # of this kind is registered — fire_tile_hook handles all
            # the per-tile lookup so we just delegate.
            if (x, y) not in self.tiles:
                continue
            log.extend(self.fire_tile_hook(when, cur, x, y))
        return log

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

    def _apply_default_passives(self, entity: "Entity") -> None:
        """Copy the system's `default_entity_passives` onto `entity`,
        skipping any id it already carries. Called from Entity.spawn for
        every entity-creation path. Each entity gets its OWN Passive
        instance (built from the spec dict) so later per-entity edits
        don't mutate the shared rule. A malformed spec is skipped
        silently (same convention as the from_dict loaders) — one bad
        default shouldn't make every entity un-spawnable; the `!defpassive
        add` command validates specs before they reach the rule, so a bad
        entry here means hand-edited config."""
        specs = self.rules.get("default_entity_passives") or []
        if not isinstance(specs, list):
            return
        for spec in specs:
            if not isinstance(spec, dict):
                continue
            pid = spec.get("id")
            if not isinstance(pid, str) or pid in entity.passives:
                continue
            try:
                entity.add_passive(Passive.from_dict(spec))
            except (VTTError, KeyError, TypeError):
                continue

    # ---- scheduled / delayed effects ----
    # A scheduled effect is a formula body queued to run at a future
    # round (match-level) or after a future number of one entity's turns
    # (entity-attached). Both live in self.scheduled; the firing points
    # are wired into next_turn / _advance_index alongside the status-tick
    # boundaries. See the `scheduled` field comment for the entry shape.
    def _validate_schedule_body(self, body: Any) -> str:
        """Normalize + validate a scheduled-effect body. Raises VTTError
        on a non-string or a body that fails formula validation, so the
        error surfaces at schedule time, not silently at fire time."""
        from formula import normalize_body_source, validate_program, FormulaError
        if not isinstance(body, str):
            raise VTTError("schedule: body must be a formula string.")
        src = normalize_body_source(body).strip()
        if not src:
            raise VTTError("schedule: body cannot be empty.")
        try:
            validate_program(
                src, known_funcs=frozenset(self.formula_functions.keys())
            )
        except FormulaError as ex:
            raise VTTError(f"schedule: invalid body: {ex}")
        return src

    @staticmethod
    def _mint_schedule_name() -> str:
        return f"sched_{uuid.uuid4().hex[:8]}"

    def add_scheduled(self, delay: Any, body: Any,
                      name: Optional[str] = None) -> str:
        """Queue a MATCH-level effect to fire at on_round_start once
        round_number has advanced by `delay` rounds (delay>=1; delay=1
        fires at the start of the next round). The body runs with no
        `self` bound (use explicit ids or `this`). Returns the schedule
        name (a generated one if `name` is falsy). Raises VTTError on a
        bad delay or body."""
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 1:
            raise VTTError("schedule: delay must be a positive integer (rounds).")
        src = self._validate_schedule_body(body)
        nm = name if (isinstance(name, str) and name) else self._mint_schedule_name()
        self.scheduled.append({
            "name": nm, "kind": "round", "body": src,
            "eid": None, "fire_round": self.round_number + delay,
        })
        return nm

    def add_scheduled_on(self, eid: str, delay: Any, body: Any,
                         name: Optional[str] = None) -> str:
        """Queue an ENTITY-attached effect to fire at `eid`'s
        on_turn_start once `delay` of its turns have started (delay>=1;
        delay=1 fires at its next turn). The body runs with `self`=eid.
        Auto-dropped if the entity is removed (death/despawn) before it
        fires. Returns the schedule name. Raises VTTError/NotFound on a
        bad delay, body, or unknown entity."""
        if eid not in self.entities:
            raise NotFound(f"Entity '{eid}' not found.")
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 1:
            raise VTTError("schedule_on: delay must be a positive integer (turns).")
        src = self._validate_schedule_body(body)
        nm = name if (isinstance(name, str) and name) else self._mint_schedule_name()
        self.scheduled.append({
            "name": nm, "kind": "turn", "body": src,
            "eid": eid, "turns_left": delay,
        })
        return nm

    def cancel_scheduled(self, name: str) -> int:
        """Remove every pending schedule whose name == `name`. Returns
        the number removed (0 if none matched)."""
        before = len(self.scheduled)
        self.scheduled = [s for s in self.scheduled if s.get("name") != name]
        return before - len(self.scheduled)

    def _prune_entity_scheduled(self, eid: str) -> None:
        """Drop every entity-attached schedule bound to `eid`. Called
        from Entity.remove so a dead/despawned entity's pending effects
        don't fire (the 'dropped if the entity dies' contract) and don't
        dangle to a future same-id entity."""
        self.scheduled = [
            s for s in self.scheduled if s.get("eid") != eid
        ]

    def _run_scheduled_body(self, entry: Dict[str, Any],
                            eid: Optional[str]) -> List[str]:
        """Run one scheduled effect's body. `self` binds to `eid` (None
        for match-level). Errors are logged with a ⚠️ marker rather than
        crashing the turn transition — a sibling schedule keeps firing."""
        from formula import FormulaEngine, EvalCtx, FormulaError
        engine = FormulaEngine(self)
        ctx = EvalCtx(this=self.current_entity_id(), target=eid)
        name = entry.get("name", "?")
        try:
            engine.eval_program(entry.get("body", ""), ctx)
        except FormulaError as ex:
            return [f"⚠️ scheduled effect `{name}` failed: {ex}"]
        return [f"⏰ scheduled effect `{name}` fired."]

    def fire_scheduled_round(self) -> List[str]:
        """Fire (and remove) every due MATCH-level round schedule —
        those with fire_round <= the current round_number. Due entries
        are removed BEFORE their bodies run so a body that re-schedules
        itself for a later round doesn't re-fire this boundary."""
        due = [
            s for s in self.scheduled
            if s.get("kind") == "round"
            and int(s.get("fire_round", 1)) <= self.round_number
        ]
        if not due:
            return []
        due_ids = {id(s) for s in due}
        self.scheduled = [s for s in self.scheduled if id(s) not in due_ids]
        log: List[str] = []
        for s in due:
            log.extend(self._run_scheduled_body(s, eid=None))
        return log

    def fire_scheduled_turn(self, eid: str) -> List[str]:
        """Decrement every ENTITY-attached schedule bound to `eid` and
        fire (and remove) those that reach 0. Called at eid's
        on_turn_start."""
        fire: List[Dict[str, Any]] = []
        for s in self.scheduled:
            if s.get("kind") == "turn" and s.get("eid") == eid:
                s["turns_left"] = int(s.get("turns_left", 1)) - 1
                if s["turns_left"] <= 0:
                    fire.append(s)
        if not fire:
            return []
        fire_ids = {id(s) for s in fire}
        self.scheduled = [s for s in self.scheduled if id(s) not in fire_ids]
        log: List[str] = []
        for s in fire:
            log.extend(self._run_scheduled_body(s, eid=eid))
        return log

    # ---- event log (combat log) ----
    # The engine appends structured event records here at its chokepoints
    # (see the call sites in write_var / _process_death / revive_corpse /
    # Entity.spawn / fire_entity_moved / fire_status_event and action.py's
    # run_action). Records are rendered to text only at read time by the
    # !log command, keeping the stored data un-opinionated.
    def _event_type_enabled(self, event_type: str) -> bool:
        """Whether an event of this type should be recorded right now.
        Gated by the event_log_enabled master switch; AUTO types are
        additionally gated by membership in the event_log_events list.
        The `custom` type (log() primitive) bypasses the list."""
        if not self.rules.get("event_log_enabled", True):
            return False
        if event_type == "custom":
            return True
        raw = self.rules.get("event_log_events", "")
        enabled = {t.strip() for t in str(raw).split(",") if t.strip()}
        return event_type in enabled

    def log_event(self, event_type: str, **fields: Any) -> bool:
        """Append a structured event to event_log if its type is enabled.
        Stamps round + turn (active_index) automatically and trims to
        event_log_retention. Returns True iff an entry was actually kept
        (False when logging is off, the type is disabled, or retention is
        0 — 'keep nothing'). A cheap no-op in the False cases, so
        chokepoints can call it unconditionally."""
        if not self._event_type_enabled(event_type):
            return False
        try:
            cap = int(self.rules.get("event_log_retention", 200))
        except (TypeError, ValueError):
            cap = 200
        if cap == 0:
            return False  # 'keep nothing' — don't even append
        entry: Dict[str, Any] = {
            "type": event_type,
            "round": self.round_number,
            "turn": self.active_index,
        }
        entry.update(fields)
        self.event_log.append(entry)
        self._trim_event_log()
        return True

    def _trim_event_log(self) -> None:
        """Enforce the event_log_retention cap (drop oldest). -1 =
        unlimited; 0 = keep nothing."""
        try:
            cap = int(self.rules.get("event_log_retention", 200))
        except (TypeError, ValueError):
            cap = 200
        if cap < 0:
            return
        if cap == 0:
            self.event_log.clear()
            return
        excess = len(self.event_log) - cap
        if excess > 0:
            del self.event_log[:excess]

    def _entity_name(self, eid: str) -> str:
        """Display name for an event field; falls back to the id when the
        entity is already gone (e.g. logging a death after removal)."""
        e = self.entities.get(eid)
        return e.name if e is not None else eid

    # ---- persistence ----
    def to_dict(self, include_history: bool = False) -> Dict[str, Any]:
        """Serialize the match for save files and snapshots.

        `include_history` defaults to False because the autosave history
        can balloon save files (every round-start state for the entire
        match). MatchHistory itself calls this with include_history=False
        when snapshotting — bundling history inside snapshots would
        produce infinite nesting. Pass include_history=True from `!store
        save <path> include_history=yes` for a long-term campaign backup
        that includes every undoable autosave.
        """
        d: Dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "grid_width": self.grid_width,
            "grid_height": self.grid_height,
            "entities": {eid: e.to_dict() for eid, e in self.entities.items()},
            "turn_order": self.turn_order,
            "active_index": self.active_index,
            "system_name": self.system_name,
            "rules": self.rules,
            "round_number": self.round_number,
            "round_started": self.round_started,
            "global_passives": {pid: p.to_dict() for pid, p in self.global_passives.items()},
            "groups": {name: list(members) for name, members in self.groups.items()},
            # Tile keys are tuples internally; JSON object keys must be
            # strings, so encode as "x,y". Order doesn't matter (the
            # match doesn't iterate tiles by position), but sorting the
            # serialized output keeps save-file diffs readable when a
            # GM edits tiles by hand.
            "tiles": {
                f"{x},{y}": dat for (x, y), dat in sorted(self.tiles.items())
            },
            "zones": {
                name: self._zone_to_dict(z)
                for name, z in sorted(self.zones.items())
            },
            "tile_templates": {
                name: tpl.to_dict()
                for name, tpl in sorted(self.tile_templates.items())
            },
            "formula_functions": {
                name: fn.to_dict()
                for name, fn in sorted(self.formula_functions.items())
            },
            "aliases": dict(self.aliases),
            "vars": copy.deepcopy(self.vars),
            "scheduled": copy.deepcopy(self.scheduled),
            "event_log": copy.deepcopy(self.event_log),
            "owner": self.owner,
            "cohosts": list(self.cohosts),
            "access_overrides": dict(self.access_overrides),
            "bound_channels": copy.deepcopy(self.bound_channels),
        }
        if include_history:
            d["history"] = self.history.to_dict()
        return d

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
        m.round_number = int(d.get("round_number", 1))
        m.round_started = bool(d.get("round_started", False))
        m.global_passives = {
            pid: Passive.from_dict(pd)
            for pid, pd in (d.get("global_passives", {}) or {}).items()
        }
        # Filter group membership to currently-existing entities so a
        # stale reference (entity deleted between save and load) doesn't
        # survive the load — group_members would otherwise return a
        # mix of live and dangling ids.
        raw_groups = d.get("groups", {}) or {}
        m.groups = {
            name: [eid for eid in members if eid in m.entities]
            for name, members in raw_groups.items()
        }
        # Filter tiles to in-bounds coords so a grid-resize between save
        # and load doesn't leave orphaned data. Match's bounds are
        # authoritative — an out-of-bounds tile would never be reachable
        # from any command or formula anyway.
        raw_tiles = d.get("tiles", {}) or {}
        m.tiles = {}
        for key, val in raw_tiles.items():
            try:
                xs, ys = key.split(",", 1)
                x, y = int(xs), int(ys)
            except (ValueError, AttributeError):
                continue
            if m.in_bounds(x, y) and isinstance(val, dict) and val:
                m.tiles[(x, y)] = val
        raw_zones = d.get("zones", {}) or {}
        m.zones = {}
        for zname, zdef in raw_zones.items():
            if not isinstance(zname, str) or not isinstance(zdef, dict):
                continue
            z = m._zone_from_dict(zdef)
            # Keep only in-bounds cells (a grid resize between save and
            # load shouldn't leave dangling cells). An emptied zone still
            # exists — its data/hooks are preserved so a shift could bring
            # it back on-grid; consistent with keeping empty groups.
            if z is not None:
                m.zones[zname] = z
        raw_templates = d.get("tile_templates", {}) or {}
        m.tile_templates = {}
        for tname, tdef in raw_templates.items():
            if not isinstance(tname, str) or not isinstance(tdef, dict):
                continue
            try:
                m.tile_templates[tname] = SpecialTileTemplate.from_dict(tdef)
            except (KeyError, TypeError, VTTError):
                # Malformed entry in the save — skip rather than fail
                # the whole load. The instances that referenced this
                # template will surface the missing-template warning
                # at their next hook fire.
                continue
        raw_funcs = d.get("formula_functions", {}) or {}
        m.formula_functions = {}
        for fname, fdef in raw_funcs.items():
            if not isinstance(fname, str) or not isinstance(fdef, dict):
                continue
            try:
                m.formula_functions[fname] = FormulaFunction.from_dict(fdef)
            except (KeyError, TypeError, VTTError):
                # Malformed entry — skip. A formula that called the
                # missing function will fail at its next evaluation.
                continue
        raw_aliases = d.get("aliases", {}) or {}
        m.aliases = {
            str(k): str(v) for k, v in raw_aliases.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        raw_vars = d.get("vars", {})
        m.vars = copy.deepcopy(raw_vars) if isinstance(raw_vars, dict) else {}
        raw_sched = d.get("scheduled", [])
        m.scheduled = (
            [e for e in copy.deepcopy(raw_sched) if isinstance(e, dict)]
            if isinstance(raw_sched, list) else []
        )
        raw_evlog = d.get("event_log", [])
        m.event_log = (
            [e for e in copy.deepcopy(raw_evlog) if isinstance(e, dict)]
            if isinstance(raw_evlog, list) else []
        )
        # Host / access control + channel bindings.
        owner = d.get("owner")
        m.owner = owner if isinstance(owner, str) else None
        raw_cohosts = d.get("cohosts", [])
        m.cohosts = (
            [c for c in raw_cohosts if isinstance(c, str)]
            if isinstance(raw_cohosts, list) else []
        )
        raw_acc = d.get("access_overrides", {})
        m.access_overrides = {
            k: str(v) for k, v in raw_acc.items()
            if isinstance(k, str) and isinstance(v, str)
        } if isinstance(raw_acc, dict) else {}
        raw_bound = d.get("bound_channels", {})
        m.bound_channels = {
            k: (dict(v) if isinstance(v, dict) else {})
            for k, v in raw_bound.items() if isinstance(k, str)
        } if isinstance(raw_bound, dict) else {}
        # History is optional in saved dicts. It's only present when the
        # original save was made with include_history=True. A snapshot's
        # state.dict deliberately omits history (snapshots-within-
        # snapshots would explode), so loading a snapshot starts with a
        # fresh empty history — the in-memory MatchHistory keeps living
        # on the OLD Match object, which the caller transfers across
        # via Match.history = ... after construction.
        if "history" in d:
            m.history = MatchHistory.from_dict(d["history"])
        return m

    # ------------- simple ASCII render for quick debugging -------------
    def render_ascii(self) -> str:
        # Build grid with an unused 0th row/col so coordinates can be 1-based
        grid = [
            ["." for _ in range(self.grid_width + 1)]
            for _ in range(self.grid_height + 1)
        ]

        # Zone glyphs are the lowest layer — a zone with a single-char
        # "glyph" paints all its cells (overlapping zones: later-iterated
        # wins). Tiles, then entities, render on top, so a zone glyph
        # shows only where no tile glyph or entity sits. Lets a gas-cloud
        # zone be visible on the map without per-cell tile setup.
        for z in self.zones.values():
            g = z.get("glyph")
            if not (isinstance(g, str) and len(g) == 1):
                continue
            for (zx, zy) in z.get("cells", ()):
                if self.in_bounds(zx, zy):
                    grid[zy][zx] = g

        # Lay down tile glyphs next — any tile with a single-character
        # "glyph" key shows that character instead of the default ".".
        # Validation is strict (must be exactly one character) so a GM
        # who accidentally sets glyph="fire" gets nothing on the map
        # rather than a column-misaligning multi-character cell.
        for (tx, ty), data in self.tiles.items():
            if not self.in_bounds(tx, ty):
                continue
            # Instance glyph wins; if the instance has no glyph but
            # comes from a template that does, use the template's.
            # This lets a "fire" template define glyph='F' once and
            # every placed instance picks it up without per-cell
            # repetition. Same single-character constraint applies
            # to both layers.
            glyph = data.get("glyph")
            if not (isinstance(glyph, str) and len(glyph) == 1):
                tpl_name = data.get("_template")
                if isinstance(tpl_name, str):
                    tpl = self.tile_templates.get(tpl_name)
                    if tpl is not None:
                        cand = tpl.data.get("glyph")
                        if isinstance(cand, str) and len(cand) == 1:
                            glyph = cand
            if isinstance(glyph, str) and len(glyph) == 1:
                grid[ty][tx] = glyph

        # Entities take precedence over tile glyphs: the standard
        # "who is where" question is more useful than tile feature
        # visualization. A GM who wants tile visibility through an
        # entity should toggle the entity off or use !tile info.
        for e in self.entities.values():
            # Keep old semantics: skip dead entities
            if not getattr(e, "is_alive", True):
                continue
            if self.in_bounds(e.x, e.y):
                sym = DIRECTION_ARROWS.get(getattr(e, "facing", ""), "@")
                grid[e.y][e.x] = sym

        # Skip the 0th row entirely to preserve 1-based coordinates
        lines = [" ".join(row[1:]) for row in grid[1:]]
        return "\n".join(lines)

    def _spawn_facing(self, x: int, y: int) -> Direction:
        eight_way = bool(self.rules.get("allow_diagonal_facing", False))
        if self.rules.get("spawn_face_toward_center", True):
            return _default_facing_for(
                x, y, self.grid_width, self.grid_height, eight_way=eight_way,
            )
        d = self.rules.get("spawn_default_facing", "up")
        if d not in ALLOWED_DIRECTIONS:
            return "up"
        # If the system disallows diagonal facing but spawn_default_facing
        # is set to one, fall back rather than silently producing a state
        # the system says shouldn't exist.
        if d in DIAGONAL_DIRECTIONS and not eight_way:
            return "up"
        return d

    # ---- group management ----
    # Groups are an organizational layer: an entity can belong to any
    # number of named groups, and various !ent subcommands accept a
    # `group:NAME` target to apply the action to every member at once.
    # Mass movement (move_group_dirs below) treats fellow group members
    # as transparent to each other so a marching group doesn't trip on
    # its own footprint. Group names are arbitrary strings — they share
    # no namespace with entity ids, so a group named "hero" and an
    # entity id "hero" can coexist without conflict.

    def has_group(self, name: str) -> bool:
        return name in self.groups

    def group_members(self, name: str) -> List[str]:
        """Return the member id list (a copy). Raises NotFound if the
        group doesn't exist. Filters out any ids that are no longer in
        self.entities, but that should never happen in practice because
        Entity.remove() scrubs group membership on its way out."""
        if name not in self.groups:
            raise NotFound(f"Group '{name}' not found in match '{self.id}'.")
        return [eid for eid in self.groups[name] if eid in self.entities]

    def groups_for(self, entity_id: str) -> List[str]:
        """Return the names of all groups containing this entity, in
        the order they were defined on the match."""
        return [name for name, members in self.groups.items()
                if entity_id in members]

    def add_to_group(self, name: str, entity_id: str) -> bool:
        """Add entity_id to group `name`. Creates the group implicitly
        if it doesn't yet exist. Returns True if newly added, False if
        the entity was already a member (idempotent). Raises NotFound
        if the entity id doesn't refer to an entity in this match."""
        if entity_id not in self.entities:
            raise NotFound(f"Entity '{entity_id}' not found in match '{self.id}'.")
        members = self.groups.setdefault(name, [])
        if entity_id in members:
            return False
        members.append(entity_id)
        return True

    def remove_from_group(self, name: str, entity_id: str) -> bool:
        """Remove entity_id from group `name`. Returns True if removed,
        False if the entity wasn't a member. Raises NotFound if the
        group doesn't exist. Empty groups are kept (they're explicit
        and might be populated later) — use delete_group to drop one."""
        if name not in self.groups:
            raise NotFound(f"Group '{name}' not found in match '{self.id}'.")
        if entity_id not in self.groups[name]:
            return False
        self.groups[name].remove(entity_id)
        return True

    def delete_group(self, name: str) -> None:
        """Drop the group entirely. Members are unaffected (the group
        is just a label — deleting it doesn't remove or alter the
        entities). Raises NotFound if the group doesn't exist."""
        if name not in self.groups:
            raise NotFound(f"Group '{name}' not found in match '{self.id}'.")
        del self.groups[name]

    def _scrub_entity_from_groups(self, entity_id: str) -> None:
        """Remove `entity_id` from every group it's in. Called by
        Entity.remove() so a deleted entity doesn't leave dangling
        references in group membership lists."""
        for members in self.groups.values():
            while entity_id in members:
                members.remove(entity_id)

    # ---- atomic group movement ----
    # Apply the same move sequence to every member of a group at once.
    # The two key properties:
    #   1. Fellow group members don't block each other — during path
    #      validation we treat them as transparent. This makes sense
    #      because every member is shifting by the same (dx,dy) vector,
    #      so geometrically no two members can collide internally as
    #      long as they started at distinct cells.
    #   2. Either the whole group moves, or nothing moves. We validate
    #      every member's path first; if any one hits an out-of-bounds
    #      cell or a non-member obstacle, we raise without mutating
    #      anything. Callers see the same exception types they'd see
    #      for a single-entity move (OutOfBounds / Occupied).
    # Designed to play nicely with a future per-step collision system:
    # the validation walks each entity step-by-step so per-step hooks
    # could be added here later without restructuring.
    def move_group_dirs(
        self, group_name: str, moves: List[Tuple[str, int]],
        *, fire_hooks: bool = True,
    ) -> Tuple[int, int, List[str]]:
        """Move every member of `group_name` through `moves` atomically.

        Returns (member_count, total_steps_per_member, log_lines).
        Raises NotFound if the group is unknown or empty. Raises
        OutOfBounds / Occupied (annotated with the offending member's
        id) if any member can't complete the move; in that case NO
        entity is moved and no facing is changed and no hooks fire.

        Tile-hook firing semantics: hooks run per MEMBER, not per group.
        Each member fires on_exit at every cell of its path, on_enter
        at every cell it arrives in, and on_stop once at its final
        cell — exactly like calling Entity.move_dirs on the same
        sequence of moves, but with the group-level all-or-nothing
        validation guarantee that no member starts moving until every
        member's path has been proven legal.
        """
        members = self.group_members(group_name)
        if not members:
            raise NotFound(f"Group '{group_name}' has no members to move.")
        allow_diag_move = bool(self.rules.get("allow_diagonal_movement", False))
        allow_diag_face = bool(self.rules.get("allow_diagonal_facing", False))
        member_set = set(members)

        # Phase 1: per-member simulation. We don't mutate anything until
        # every member has been validated; if a later member fails, the
        # earlier ones must not have moved. plans[eid] is the full
        # per-step path so phase 2 can fire hooks cell by cell.
        plans: Dict[str, List[Tuple[int, int, str]]] = {}
        for eid in members:
            e = self.entities[eid]
            x, y = e.x, e.y
            path: List[Tuple[int, int, str]] = []
            for direction, count in moves:
                canon = normalize_direction(direction)
                if canon is None:
                    raise VTTError(f"Unknown direction '{direction}'")
                if canon in DIAGONAL_DIRECTIONS and not allow_diag_move:
                    raise VTTError(
                        f"Diagonal direction '{direction}' is not allowed by "
                        f"the active game system. Enable rule "
                        f"'allow_diagonal_movement' to permit it."
                    )
                dx, dy = DIRECTION_VECTORS[canon]
                step_facing = canon
                if canon in DIAGONAL_DIRECTIONS and not allow_diag_face:
                    step_facing = "up" if dy < 0 else "down"
                for _ in range(max(1, int(count))):
                    nx, ny = x + dx, y + dy
                    if not self.in_bounds(nx, ny):
                        raise OutOfBounds(
                            f"Group '{group_name}' move aborted: member "
                            f"'{eid}' would leave the grid at ({nx},{ny})."
                        )
                    path.append((nx, ny, step_facing))
                    x, y = nx, ny
            # Final-cell occupancy: ignore the entity itself AND every
            # other group member (they're moving too — treat as
            # transparent). We hand-roll the loop here because
            # Match.is_occupied only takes a single ignore_entity_id.
            for other_eid, other_e in self.entities.items():
                if other_eid in member_set:
                    continue
                if other_e.x == x and other_e.y == y and other_e.is_alive:
                    raise Occupied(
                        f"Group '{group_name}' move aborted: member "
                        f"'{eid}' would land on ({x},{y}), occupied by "
                        f"'{other_eid}'."
                    )
            plans[eid] = path

        # Phase 2: commit per member, firing tile hooks step by step.
        # Geometric invariant (same shift vector for all members)
        # guarantees no intra-group collision; we still walk each
        # member's path one cell at a time so on_enter / on_exit fire
        # per intermediate tile (same as single-entity move_dirs).
        log: List[str] = []
        for eid, path in plans.items():
            e = self.entities[eid]
            origin_x, origin_y = e.x, e.y
            for nx, ny, facing in path:
                sx, sy = e.x, e.y
                if fire_hooks:
                    log.extend(self.fire_tile_hook("on_exit", eid, sx, sy))
                    log.extend(self.fire_zone_exit_hooks(eid, sx, sy, nx, ny))
                e.facing = facing
                e.move_to(nx, ny)
                if fire_hooks:
                    log.extend(self.fire_tile_hook("on_enter", eid, nx, ny))
                    log.extend(self.fire_zone_enter_hooks(eid, sx, sy, nx, ny, False))
            if fire_hooks and path:
                log.extend(self.fire_tile_hook("on_stop", eid, e.x, e.y))
                log.extend(self.fire_zone_stop_hooks(eid, e.x, e.y))
                # on_entity_moved per group member, once per member
                # (mirrors single-entity move_dirs semantics).
                log.extend(self.fire_entity_moved(
                    eid, origin_x, origin_y, e.x, e.y,
                ))
        total_steps = sum(max(1, int(n)) for _, n in moves)
        return len(members), total_steps, log

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

    def delete_system(self, name: str) -> None:
        """Remove a GameSystem. Guards against leaving the manager in a
        broken state:
          - the system must exist
          - at least one system must remain afterward (there's always a
            global default to fall back to)
          - it must not be the global default (reassign that first)
          - no live match may still be bound to it (rebind or delete
            those matches first)
        Per-server / per-channel default pointers AT this system are
        scrubbed (they fall back to the global default automatically via
        effective_system_name)."""
        if name not in self.systems:
            raise NotFound(f"GameSystem '{name}' not found.")
        if len(self.systems) <= 1:
            raise VTTError(
                "Cannot delete the only remaining GameSystem — at least "
                "one must exist."
            )
        if name == self.default_system_name:
            raise VTTError(
                f"'{name}' is the global default GameSystem. Set a "
                f"different global default first "
                f"(!system default global <other>)."
            )
        bound = [mid for mid, m in self.matches.items() if m.system_name == name]
        if bound:
            shown = ", ".join(sorted(bound)[:5])
            more = "" if len(bound) <= 5 else f" (+{len(bound) - 5} more)"
            raise VTTError(
                f"GameSystem '{name}' is still used by {len(bound)} "
                f"match(es): {shown}{more}. Rebind or delete those "
                f"matches first."
            )
        del self.systems[name]
        # Scrub server/channel default pointers that referenced it; they
        # fall back to the global default via effective_system_name.
        self.default_system_per_server = {
            k: v for k, v in self.default_system_per_server.items() if v != name
        }
        self.default_system_per_channel = {
            k: v for k, v in self.default_system_per_channel.items() if v != name
        }

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

    def create_match(self, match_id: str, name: str, width: int, height: int, channel_key: Optional[str] = None, system_name: Optional[str] = None, owner: Optional[str] = None) -> str:
        if match_id in self.matches:
            raise DuplicateId(f"Match id '{match_id}' already exists")
        sysobj = self.get_system(system_name) if system_name else (
            self.effective_system(channel_key or "CLI")
        )
        rules = self._build_rules_dict(sysobj)
        m = Match(id=match_id, name=name, grid_width=width, grid_height=height,
                  system_name=sysobj.name, rules=rules)
        # The creator becomes the match owner (full privileges + sole host
        # manager). The creating channel isn't bound here — binding tracks
        # channels that ACTIVATE the match, which happens on `match use` /
        # `match bind` (the creator's `match new` is followed by a `use`).
        m.owner = owner
        # Seed match-level templates from the system's defaults. We copy
        # so subsequent match-side define / undefine doesn't reach back
        # into GameSystem.tile_templates — the system's library is the
        # canonical source for NEW matches but not a live link to
        # existing ones (mirrors how `rules` is snapshotted, not linked).
        for tname, tdef in (sysobj.tile_templates or {}).items():
            try:
                m.tile_templates[tname] = SpecialTileTemplate.from_dict(tdef)
            except (KeyError, TypeError, VTTError):
                continue
        # Seed match-level formula functions from the system's library,
        # same snapshot-not-link semantics as tile templates above.
        for fname, fdef in (sysobj.formula_functions or {}).items():
            try:
                m.formula_functions[fname] = FormulaFunction.from_dict(fdef)
            except (KeyError, TypeError, VTTError):
                continue
        # Seed match-level aliases from the system's library, same
        # snapshot-not-link semantics. Plain string copy — match-side
        # !alias def/del afterwards doesn't reach back into the system.
        for aname, expansion in (sysobj.aliases or {}).items():
            if isinstance(aname, str) and isinstance(expansion, str):
                m.aliases[aname] = expansion
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
    def save(self, path: str, include_history: bool = False):
        """Persist all matches & bindings to JSON.

        `include_history` is forwarded to Match.to_dict — if True, each
        match's autosave history is bundled into the save file. Off by
        default to keep save files small; opt in via
        `!store save <path> include_history=yes` for a complete backup.
        """
        data = {
            "matches": {
                mid: m.to_dict(include_history=include_history)
                for mid, m in self.matches.items()
            },
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
            self.systems = {
                name: GameSystem.from_dict(sd)
                for name, sd in data["systems"].items()
            }
            self.default_system_name = data.get("default_system_name", "default")
            self.default_system_per_server = data.get("default_system_per_server", {})
            self.default_system_per_channel = data.get("default_system_per_channel", {})
            # Re-snapshot each match's rules dict from its bound system so
            # mid-write rule edits since the save took effect when reloaded.
            for m in self.matches.values():
                if m.system_name not in self.systems:
                    m.system_name = self.default_system_name
                m.rules = self._build_rules_dict(self.systems[m.system_name])
                for e in m.entities.values():
                    e.bind(m, set_spawn_facing=False)
        except Exception as e:
            # Defensive: any schema mismatch should be surfaced as a friendly VTTError
            raise VTTError(f"Invalid save file format in '{path}': {e}")