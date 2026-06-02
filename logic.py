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

# Tile hooks live in tiles[(x, y)]["hooks"][<name>] as formula strings.
# They fire when an entity transits a tile via Entity.tp / Entity.move_dirs
# (and Match.move_group_dirs for group-shift moves) — see Match.fire_tile_hook
# for the actual firing path. These names are kept separate from
# HOOK_NAMES because entity / global passives don't subscribe to them
# (only stored tile-side formulas do).
TILE_HOOK_NAMES: Set[str] = {
    "on_enter",   # entity arrived on this tile (fired post-position-change)
    "on_exit",    # entity is about to leave this tile (fired PRE-position-change
                  # so entity[self].x / .y still refer to the exit tile)
    "on_stop",    # entity stopped here: either a tp landed here, or this
                  # was the last cell of a stepwise move. Fires after on_enter
                  # at the same coord.
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
        return GameSystem(
            name=d["name"], settings=settings,
            tile_templates=templates, formula_functions=funcs,
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
        if m.is_occupied(x, y, ignore_entity_id=self.id):
            raise Occupied(f"Cell ({x},{y}) already occupied")
        log: List[str] = []
        old_x, old_y = self.x, self.y
        if fire_hooks and (old_x, old_y) != (x, y):
            # Skip on_exit when teleporting to the current cell (no
            # actual move). Matches the intuition that "tp to where I
            # am" is a no-op rather than a fire-and-reverse cycle.
            log.extend(m.fire_tile_hook("on_exit", self.id, old_x, old_y))
        self.move_to(x, y)
        if fire_hooks:
            log.extend(m.fire_tile_hook("on_enter", self.id, x, y))
            log.extend(m.fire_tile_hook("on_stop", self.id, x, y))
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
        if m.is_occupied(x, y, ignore_entity_id=self.id):
            raise Occupied(f"Cell ({x},{y}) already occupied")

        # Phase 2: commit each step, firing hooks. No more validation —
        # phase 1 already proved the whole path is legal.
        log: List[str] = []
        for nx, ny, step_facing in step_path:
            if fire_hooks:
                log.extend(m.fire_tile_hook("on_exit", self.id, self.x, self.y))
            self.facing = step_facing
            self.move_to(nx, ny)
            if fire_hooks:
                log.extend(m.fire_tile_hook("on_enter", self.id, nx, ny))
        if fire_hooks and step_path:
            # on_stop fires once at the final cell, even if no actual
            # movement happened (zero-step move_dirs) — empty step_path
            # means no transit and therefore no stop.
            log.extend(m.fire_tile_hook("on_stop", self.id, self.x, self.y))
        return log

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
        for eid, e in self.entities.items():
            if ignore_entity_id and eid == ignore_entity_id:
                continue
            if e.x == x and e.y == y and e.is_alive:
                return True
        return False

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
        log.extend(self.fire_hook("on_turn_end", target_ids=[cur]))
        self._advance_index(log)
        # Skip over any entity carrying a skip-status flag.
        eligible = self._skip_to_eligible(log)
        new_cur = self.turn_order[self.active_index]
        if eligible:
            log.extend(self.fire_hook("on_turn_start", target_ids=[new_cur]))
            log.extend(self.fire_status_tick("turn_start"))
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

    def fire_tile_hook(self, when: str, entity_id: str, x: int, y: int) -> List[str]:
        """Fire the tile hook at (x, y) of the given `when`, with the
        moving entity bound as `self`.

        Returns log lines: one info line per successful fire, one warning
        line per formula failure. Empty list when no hook is registered
        for this (tile, when) — the common case, since most tiles carry
        no hooks at all.

        Context bindings:
          - self          = the entering/exiting/stopping entity
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
            target=entity_id,
            extras=extras,
        )
        try:
            engine.eval_program(formula_src, ctx)
        except FormulaError as ex:
            return [
                f"⚠️ tile ({x},{y}) hook `{when}` for `{entity_id}` "
                f"FAILED: {ex}"
            ]
        except Exception as ex:
            # Defensive: any non-FormulaError exception is a bug, but
            # don't bring down the move on it.
            return [
                f"⚠️ tile ({x},{y}) hook `{when}` for `{entity_id}` "
                f"CRASHED: {type(ex).__name__}: {ex}"
            ]
        return [
            f"⚙️ tile ({x},{y}) hook `{when}` fired for `{entity_id}`"
        ]

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
            "tile_templates": {
                name: tpl.to_dict()
                for name, tpl in sorted(self.tile_templates.items())
            },
            "formula_functions": {
                name: fn.to_dict()
                for name, fn in sorted(self.formula_functions.items())
            },
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

        # Lay down tile glyphs first — any tile with a single-character
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
            for nx, ny, facing in path:
                if fire_hooks:
                    log.extend(self.fire_tile_hook("on_exit", eid, e.x, e.y))
                e.facing = facing
                e.move_to(nx, ny)
                if fire_hooks:
                    log.extend(self.fire_tile_hook("on_enter", eid, nx, ny))
            if fire_hooks and path:
                log.extend(self.fire_tile_hook("on_stop", eid, e.x, e.y))
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

    def create_match(self, match_id: str, name: str, width: int, height: int, channel_key: Optional[str] = None, system_name: Optional[str] = None) -> str:
        if match_id in self.matches:
            raise DuplicateId(f"Match id '{match_id}' already exists")
        sysobj = self.get_system(system_name) if system_name else (
            self.effective_system(channel_key or "CLI")
        )
        rules = self._build_rules_dict(sysobj)
        m = Match(id=match_id, name=name, grid_width=width, grid_height=height,
                  system_name=sysobj.name, rules=rules)
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